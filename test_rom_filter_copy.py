"""Tests for rom_filter_copy."""

import math
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import rom_filter_copy
from rom_filter_copy import (
    build_media_index,
    copy_system,
    format_size,
    parse_rating,
    parse_rom_path,
    preview_system,
    should_include,
)


# ---------------------------------------------------------------------------
# format_size
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (0,            "0.0 B"),
    (1,            "1.0 B"),
    (1023,         "1023.0 B"),
    (1024,         "1.0 KB"),
    (1536,         "1.5 KB"),
    (1024 ** 2,    "1.0 MB"),
    (1024 ** 3,    "1.0 GB"),
    (1024 ** 4,    "1.0 TB"),
    (1024 ** 5,    "1.0 PB"),
    (1024 ** 6,    "1024.0 PB"),
])
def test_format_size(value, expected):
    assert format_size(value) == expected


# ---------------------------------------------------------------------------
# parse_rating
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    (None,    None),
    ("",      None),
    ("   ",   None),
    ("abc",   None),
    ("0",     0.0),
    ("0.0",   0.0),
    ("0.85",  0.85),
    ("1.0",   1.0),
    ("-0.1", -0.1),
    (" 0.5 ", 0.5),
    # Document current behavior: anything float() accepts passes through.
    ("+0.5",  0.5),
    ("1e2",   100.0),
    ("inf",   float("inf")),
])
def test_parse_rating(text, expected):
    assert parse_rating(text) == expected


def test_parse_rating_nan_documents_current_behavior():
    """'nan' is accepted by float() and parse_rating returns it. NaN compares
    False against any min_rating, so a NaN-rated game is silently dropped — not
    an outright bug, but worth pinning so the behavior is intentional."""
    result = parse_rating("nan")
    assert result is not None
    assert math.isnan(result)


# ---------------------------------------------------------------------------
# parse_rom_path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    (None,                None),
    ("",                  None),
    ("   ",               None),
    ("./",                None),
    ("./Game.zip",        Path("Game.zip")),
    ("Game.zip",          Path("Game.zip")),
    ("subdir/Game.iso",   Path("subdir/Game.iso")),
    ("./subdir/Game.iso", Path("subdir/Game.iso")),
    ("  ./Game.zip  ",    Path("Game.zip")),
    ("Pokemon (U) [!].gbc", Path("Pokemon (U) [!].gbc")),
    # Regression for the old lstrip("./") bug — these cases would have had
    # leading dots stripped by character-set semantics, but removeprefix("./")
    # leaves them intact.
    (".dotfile.zip",      Path(".dotfile.zip")),
    ("...Game.zip",       Path("...Game.zip")),
    # Defensive guard: absolute paths and traversal segments would escape the
    # ROMs dir when joined (Path / abs == abs; "../" walks out of the system
    # dir). Both are rejected.
    ("/etc/passwd",       None),
    ("../escape.zip",     None),
    ("foo/../bar.zip",    None),
    # Backslashes are not path separators on Linux/WSL — the whole string is
    # treated as a single filename. ES-DE writes forward slashes, so this is
    # unlikely in practice, but pinned so a future change is noticed.
    (".\\Game.zip",       Path(".\\Game.zip")),
])
def test_parse_rom_path(text, expected):
    assert parse_rom_path(text) == expected


# ---------------------------------------------------------------------------
# should_include
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rating, min_rating, include_unrated, copy_all, expected", [
    # copy_all wins over everything
    (None, 0.7, False, True,  True),
    (0.0,  0.7, False, True,  True),
    (0.3,  0.7, False, True,  True),

    # unrated handling
    (None, 0.7, False, False, False),
    (None, 0.7, True,  False, True),

    # rating threshold boundary (inclusive on the threshold)
    (0.70, 0.7, False, False, True),
    (0.69, 0.7, False, False, False),
    (0.71, 0.7, False, False, True),

    # edges of the 0..1 scale
    (0.0, 0.0, False, False, True),
    (1.0, 1.0, False, False, True),
])
def test_should_include(rating, min_rating, include_unrated, copy_all, expected):
    assert should_include(rating, min_rating, include_unrated, copy_all) is expected


# ---------------------------------------------------------------------------
# build_media_index (touches the filesystem via tmp_path)
# ---------------------------------------------------------------------------

def _make_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


def test_build_media_index_missing_system_dir(tmp_path):
    assert build_media_index("snes", tmp_path) == {}


def test_build_media_index_finds_across_subdirs(tmp_path):
    media = tmp_path
    cover = _make_file(media / "snes" / "covers" / "Game.png")
    shot  = _make_file(media / "snes" / "screenshots" / "Game.jpg")
    other = _make_file(media / "snes" / "covers" / "Other.png")

    index = build_media_index("snes", media)
    # _make_file writes b"x" → 1 byte
    assert sorted(index["Game"]) == sorted([(cover, 1), (shot, 1)])
    assert index["Other"]        == [(other, 1)]


def test_build_media_index_bracket_stem_regression(tmp_path):
    """Regression: glob would have mis-matched stems with [, ?, *."""
    media = tmp_path
    stem  = "Final Fantasy IV (USA) [!]"
    match = _make_file(media / "snes" / "covers" / f"{stem}.png")
    decoy = _make_file(media / "snes" / "covers" / "Final Fantasy IV (USA) X.png")

    index = build_media_index("snes", media)
    assert index[stem]                              == [(match, 1)]
    assert index["Final Fantasy IV (USA) X"]        == [(decoy, 1)]


def test_build_media_index_skips_non_dir_entries(tmp_path):
    media = tmp_path
    _make_file(media / "snes" / "stray.txt")        # stray file at system root
    cover = _make_file(media / "snes" / "covers" / "Game.png")

    index = build_media_index("snes", media)
    assert index == {"Game": [(cover, 1)]}


def test_build_media_index_carries_real_file_size(tmp_path):
    """Size in the index must come from disk, not be hard-coded."""
    media = tmp_path
    cover = media / "snes" / "covers" / "Game.png"
    cover.parent.mkdir(parents=True)
    cover.write_bytes(b"a" * 4096)

    index = build_media_index("snes", media)
    assert index["Game"] == [(cover, 4096)]


def test_build_media_index_empty_when_no_files(tmp_path):
    media = tmp_path
    (media / "snes" / "covers").mkdir(parents=True)  # empty subdir
    assert build_media_index("snes", media) == {}


# ---------------------------------------------------------------------------
# preview_system (end-to-end against a fake gamelist + rom/media tree)
# ---------------------------------------------------------------------------

def _write_gamelist(path: Path, games: list[dict]) -> None:
    """Write a minimal gamelist.xml. Each game dict supports keys: path, rating."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ['<?xml version="1.0"?>', "<gameList>"]
    for g in games:
        lines.append("  <game>")
        if "path" in g:
            lines.append(f"    <path>{g['path']}</path>")
        if "rating" in g:
            lines.append(f"    <rating>{g['rating']}</rating>")
        lines.append("  </game>")
    lines.append("</gameList>")
    path.write_text("\n".join(lines), encoding="utf-8")


@pytest.fixture
def tree(tmp_path):
    """Build the standard fake tree and return its key paths."""
    roms_dir  = tmp_path / "roms"
    media_dir = tmp_path / "media"
    gl_path   = tmp_path / "gamelists" / "snes" / "gamelist.xml"
    return {
        "roms_dir":  roms_dir,
        "media_dir": media_dir,
        "gl_path":   gl_path,
        "system":    "snes",
    }


def test_preview_empty_gamelist(tree):
    _write_gamelist(tree["gl_path"], [])
    games, skipped, missing, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, False, False,
        tree["roms_dir"], tree["media_dir"],
    )
    assert (games, skipped, missing) == ([], 0, 0)


def test_preview_includes_above_threshold(tree):
    _write_gamelist(tree["gl_path"], [{"path": "./Good.zip", "rating": "0.85"}])
    rom_src   = _make_file(tree["roms_dir"] / "snes" / "Good.zip")
    cover_src = _make_file(tree["media_dir"] / "snes" / "covers" / "Good.png")

    games, skipped, missing, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, False, False,
        tree["roms_dir"], tree["media_dir"],
    )
    assert len(games) == 1
    assert skipped == 0
    assert missing == 0
    entry = games[0]
    assert entry["rom_filename"] == Path("Good.zip")
    assert entry["src_rom"]      == rom_src
    assert entry["media_files"]  == [cover_src]
    assert entry["bytes"]        == rom_src.stat().st_size + cover_src.stat().st_size
    # The dict carries the *right* <game> element, not just any element.
    assert entry["game"].find("path").text == "./Good.zip"


def test_preview_skips_below_threshold(tree):
    _write_gamelist(tree["gl_path"], [{"path": "./Bad.zip", "rating": "0.5"}])
    _make_file(tree["roms_dir"] / "snes" / "Bad.zip")

    games, skipped, missing, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, False, False,
        tree["roms_dir"], tree["media_dir"],
    )
    assert (games, skipped, missing) == ([], 1, 0)


def test_preview_unrated_excluded_by_default(tree):
    _write_gamelist(tree["gl_path"], [{"path": "./Unknown.zip"}])
    _make_file(tree["roms_dir"] / "snes" / "Unknown.zip")

    games, skipped, missing, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, False, False,
        tree["roms_dir"], tree["media_dir"],
    )
    assert (len(games), skipped, missing) == (0, 1, 0)


def test_preview_unrated_included_with_flag(tree):
    _write_gamelist(tree["gl_path"], [{"path": "./Unknown.zip"}])
    _make_file(tree["roms_dir"] / "snes" / "Unknown.zip")

    games, skipped, missing, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, True, False,
        tree["roms_dir"], tree["media_dir"],
    )
    assert (len(games), skipped, missing) == (1, 0, 0)


def test_preview_missing_rom_counted_separately(tree):
    _write_gamelist(tree["gl_path"], [{"path": "./Ghost.zip", "rating": "0.9"}])
    # No ROM file on disk.

    games, skipped, missing, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, False, False,
        tree["roms_dir"], tree["media_dir"],
    )
    assert (len(games), skipped, missing) == (0, 0, 1)


def test_preview_copy_all_overrides_rating(tree):
    _write_gamelist(tree["gl_path"], [
        {"path": "./Low.zip", "rating": "0.1"},
        {"path": "./None.zip"},
    ])
    _make_file(tree["roms_dir"] / "snes" / "Low.zip")
    _make_file(tree["roms_dir"] / "snes" / "None.zip")

    games, skipped, missing, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, False, True,  # copy_all=True
        tree["roms_dir"], tree["media_dir"],
    )
    assert (len(games), skipped, missing) == (2, 0, 0)
    # Pin both entries are present (not duplicates of one).
    rom_names = sorted(g["rom_filename"].name for g in games)
    assert rom_names == ["Low.zip", "None.zip"]


def test_preview_bracket_stem_finds_media(tree):
    """End-to-end regression for the glob bug — bracket-tagged ROM matches its art."""
    name = "Final Fantasy IV (USA) [!]"
    _write_gamelist(tree["gl_path"], [{"path": f"./{name}.sfc", "rating": "0.9"}])
    _make_file(tree["roms_dir"] / "snes" / f"{name}.sfc")
    cover = _make_file(tree["media_dir"] / "snes" / "covers" / f"{name}.png")

    games, _, _, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, False, False,
        tree["roms_dir"], tree["media_dir"],
    )
    assert len(games) == 1
    assert games[0]["media_files"] == [cover]


def test_preview_empty_path_element_ignored(tree):
    _write_gamelist(tree["gl_path"], [
        {"path": "", "rating": "0.9"},
        {"path": "./Real.zip", "rating": "0.9"},
    ])
    _make_file(tree["roms_dir"] / "snes" / "Real.zip")

    games, skipped, missing, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, False, False,
        tree["roms_dir"], tree["media_dir"],
    )
    # Empty path entry is dropped silently (not counted as skipped or missing).
    assert (len(games), skipped, missing) == (1, 0, 0)


def test_preview_malformed_rating_treated_as_unrated(tree):
    _write_gamelist(tree["gl_path"], [{"path": "./Game.zip", "rating": "not-a-number"}])
    _make_file(tree["roms_dir"] / "snes" / "Game.zip")

    # Without --include-unrated: skipped.
    games, skipped, _, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, False, False,
        tree["roms_dir"], tree["media_dir"],
    )
    assert (len(games), skipped) == (0, 1)

    # With --include-unrated: included.
    games, skipped, _, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, True, False,
        tree["roms_dir"], tree["media_dir"],
    )
    assert (len(games), skipped) == (1, 0)


def test_preview_counts_all_skipped_games(tree):
    _write_gamelist(tree["gl_path"], [
        {"path": "./Bad1.zip", "rating": "0.3"},
        {"path": "./Bad2.zip", "rating": "0.4"},
    ])
    _make_file(tree["roms_dir"] / "snes" / "Bad1.zip")
    _make_file(tree["roms_dir"] / "snes" / "Bad2.zip")

    games, skipped, missing, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, False, False,
        tree["roms_dir"], tree["media_dir"],
    )
    assert (len(games), skipped, missing) == (0, 2, 0)


def test_preview_continues_after_skipped_game(tree):
    """Skipped game does not stop iteration — subsequent qualifying games are included."""
    _write_gamelist(tree["gl_path"], [
        {"path": "./Bad.zip",  "rating": "0.3"},
        {"path": "./Good.zip", "rating": "0.9"},
    ])
    _make_file(tree["roms_dir"] / "snes" / "Bad.zip")
    _make_file(tree["roms_dir"] / "snes" / "Good.zip")

    games, skipped, missing, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, False, False,
        tree["roms_dir"], tree["media_dir"],
    )
    assert len(games) == 1
    assert skipped == 1
    assert games[0]["rom_filename"] == Path("Good.zip")


def test_preview_counts_all_missing_roms(tree):
    _write_gamelist(tree["gl_path"], [
        {"path": "./Ghost1.zip", "rating": "0.9"},
        {"path": "./Ghost2.zip", "rating": "0.9"},
    ])
    # Neither ROM exists on disk.

    games, skipped, missing, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, False, False,
        tree["roms_dir"], tree["media_dir"],
    )
    assert (len(games), skipped, missing) == (0, 0, 2)


def test_preview_continues_after_missing_rom(tree):
    """Missing ROM does not stop iteration — subsequent present ROMs are included."""
    _write_gamelist(tree["gl_path"], [
        {"path": "./Ghost.zip", "rating": "0.9"},
        {"path": "./Good.zip",  "rating": "0.9"},
    ])
    # Ghost.zip absent; Good.zip present.
    _make_file(tree["roms_dir"] / "snes" / "Good.zip")

    games, skipped, missing, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, False, False,
        tree["roms_dir"], tree["media_dir"],
    )
    assert len(games) == 1
    assert missing == 1
    assert games[0]["rom_filename"] == Path("Good.zip")


def test_preview_copy_bytes_equal_full_size_when_no_targets(tree):
    """Without skip-existing targets, copy_rom_bytes and copy_media_bytes equal
    the full source sizes (no subtraction for already-present files)."""
    _write_gamelist(tree["gl_path"], [{"path": "./Good.zip", "rating": "0.9"}])
    _make_file(tree["roms_dir"]  / "snes" / "Good.zip")
    _make_file(tree["media_dir"] / "snes" / "covers" / "Good.png")

    games, _, _, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, False, False,
        tree["roms_dir"], tree["media_dir"],
    )
    assert games[0]["copy_rom_bytes"]   == games[0]["rom_bytes"]
    assert games[0]["copy_media_bytes"] == games[0]["media_bytes"]


# ---------------------------------------------------------------------------
# preview_system with skip-existing (overwrite=False)
# ---------------------------------------------------------------------------

def test_preview_skip_existing_zeros_rom_bytes_when_target_matches(tree, tmp_path):
    """Same-size ROM on target → copy_rom_bytes drops to 0 (won't re-copy).
    Full rom_bytes is still tracked so the size display stays meaningful."""
    t_roms = tmp_path / "t-roms"
    t_esde = tmp_path / "t-esde"
    _write_gamelist(tree["gl_path"], [{"path": "./Good.zip", "rating": "0.9"}])
    _make_file(tree["roms_dir"] / "snes" / "Good.zip")  # 1 byte
    pre = t_roms / "snes" / "Good.zip"
    pre.parent.mkdir(parents=True)
    pre.write_bytes(b"Y")  # 1 byte, matches src size

    games, _, _, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, False, False,
        tree["roms_dir"], tree["media_dir"],
        target_roms_dir=t_roms, target_esde_data_dir=t_esde, overwrite=False,
    )
    assert games[0]["rom_bytes"]      == 1
    assert games[0]["copy_rom_bytes"] == 0


def test_preview_skip_existing_subtracts_only_matched_media(tree, tmp_path):
    """One of two media files already on target → only the unmatched one
    contributes to copy_media_bytes."""
    t_roms = tmp_path / "t-roms"
    t_esde = tmp_path / "t-esde"
    _write_gamelist(tree["gl_path"], [{"path": "./Good.zip", "rating": "0.9"}])
    _make_file(tree["roms_dir"]  / "snes" / "Good.zip")
    _make_file(tree["media_dir"] / "snes" / "covers"      / "Good.png")
    _make_file(tree["media_dir"] / "snes" / "screenshots" / "Good.jpg")
    pre = t_esde / "downloaded_media" / "snes" / "covers" / "Good.png"
    pre.parent.mkdir(parents=True)
    pre.write_bytes(b"Y")  # 1 byte matches src

    games, _, _, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, False, False,
        tree["roms_dir"], tree["media_dir"],
        target_roms_dir=t_roms, target_esde_data_dir=t_esde, overwrite=False,
    )
    assert games[0]["media_bytes"]      == 2  # both src files at 1 byte
    assert games[0]["copy_media_bytes"] == 1  # only the screenshot remains to copy


def test_preview_skip_existing_re_copies_when_target_size_differs(tree, tmp_path):
    """Same-named target file but different size → not a match, full ROM
    still needs copying. Guards against the truncated-previous-copy case."""
    t_roms = tmp_path / "t-roms"
    t_esde = tmp_path / "t-esde"
    _write_gamelist(tree["gl_path"], [{"path": "./Good.zip", "rating": "0.9"}])
    _make_file(tree["roms_dir"] / "snes" / "Good.zip")  # 1 byte
    pre = t_roms / "snes" / "Good.zip"
    pre.parent.mkdir(parents=True)
    pre.write_bytes(b"OLDLONGER")  # 9 bytes — mismatch

    games, _, _, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, False, False,
        tree["roms_dir"], tree["media_dir"],
        target_roms_dir=t_roms, target_esde_data_dir=t_esde, overwrite=False,
    )
    assert games[0]["copy_rom_bytes"] == 1


def test_preview_skip_existing_accumulates_all_unmatched_media(tree, tmp_path):
    """All media absent from target: copy_media_bytes sums every file, not just the last."""
    t_roms = tmp_path / "t-roms"
    t_esde = tmp_path / "t-esde"
    _write_gamelist(tree["gl_path"], [{"path": "./Good.zip", "rating": "0.9"}])
    _make_file(tree["roms_dir"] / "snes" / "Good.zip")
    cover = tree["media_dir"] / "snes" / "covers"      / "Good.png"
    shot  = tree["media_dir"] / "snes" / "screenshots" / "Good.jpg"
    cover.parent.mkdir(parents=True, exist_ok=True)
    cover.write_bytes(b"A" * 2)   # 2 bytes
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(b"B" * 3)    # 3 bytes — distinct size so mutation is detectable

    games, _, _, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, False, False,
        tree["roms_dir"], tree["media_dir"],
        target_roms_dir=t_roms, target_esde_data_dir=t_esde, overwrite=False,
    )
    assert games[0]["media_bytes"]      == 5
    assert games[0]["copy_media_bytes"] == 5  # both files absent from target


# ---------------------------------------------------------------------------
# copy_system (full source-to-target round-trip)
# ---------------------------------------------------------------------------

def _read_target_gamelist_paths(target_esde: Path, system: str) -> list[str]:
    gl = target_esde / "gamelists" / system / "gamelist.xml"
    out: list[str] = []
    for g in ET.parse(gl).getroot().findall("game"):
        el = g.find("path")
        assert el is not None and el.text is not None
        out.append(el.text)
    return out


def _targets(tmp_path: Path) -> tuple[Path, Path]:
    """Two independent target roots — they don't share a parent."""
    return tmp_path / "roms-out", tmp_path / "esde-out"


def test_copy_system_writes_rom_media_and_gamelist(tree, tmp_path):
    t_roms, t_esde = _targets(tmp_path)
    _write_gamelist(tree["gl_path"], [{"path": "./Good.zip", "rating": "0.9"}])
    _make_file(tree["roms_dir"]  / "snes" / "Good.zip")
    _make_file(tree["media_dir"] / "snes" / "covers"      / "Good.png")
    _make_file(tree["media_dir"] / "snes" / "screenshots" / "Good.jpg")

    games, _, _, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, False, False,
        tree["roms_dir"], tree["media_dir"],
    )
    copy_system(tree["system"], games, t_roms, t_esde)

    assert (t_roms / "snes" / "Good.zip").is_file()
    assert (t_esde / "downloaded_media" / "snes" / "covers"      / "Good.png").is_file()
    assert (t_esde / "downloaded_media" / "snes" / "screenshots" / "Good.jpg").is_file()
    assert _read_target_gamelist_paths(t_esde, "snes") == ["./Good.zip"]


def test_copy_system_excludes_ghost_entries_from_target_gamelist(tree, tmp_path):
    """Bug #3 at the output layer: missing-ROM entries must not appear in target xml."""
    t_roms, t_esde = _targets(tmp_path)
    _write_gamelist(tree["gl_path"], [
        {"path": "./Real.zip",  "rating": "0.9"},
        {"path": "./Ghost.zip", "rating": "0.9"},
    ])
    _make_file(tree["roms_dir"] / "snes" / "Real.zip")
    # Ghost.zip intentionally absent on disk.

    games, _, missing, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, False, False,
        tree["roms_dir"], tree["media_dir"],
    )
    assert missing == 1
    copy_system(tree["system"], games, t_roms, t_esde)

    assert (t_roms / "snes" / "Real.zip").is_file()
    assert not (t_roms / "snes" / "Ghost.zip").exists()
    assert _read_target_gamelist_paths(t_esde, "snes") == ["./Real.zip"]


def test_copy_system_survives_rom_vanishing_after_preview(tree, tmp_path):
    """The exists() guard at the top of copy_system handles a race where the
    ROM was present at preview time but deleted before the copy phase."""
    t_roms, t_esde = _targets(tmp_path)
    _write_gamelist(tree["gl_path"], [{"path": "./Vanish.zip", "rating": "0.9"}])
    rom = _make_file(tree["roms_dir"] / "snes" / "Vanish.zip")

    games, _, _, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, False, False,
        tree["roms_dir"], tree["media_dir"],
    )
    rom.unlink()  # disappears between preview and copy
    copy_system(tree["system"], games, t_roms, t_esde)

    # No crash; rom didn't land at target; gamelist still records the entry.
    assert not (t_roms / "snes" / "Vanish.zip").exists()
    assert _read_target_gamelist_paths(t_esde, "snes") == ["./Vanish.zip"]


def test_copy_system_targets_can_live_on_separate_roots(tree, tmp_path):
    """ROMs target and ES-DE target are independent — no shared parent inferred."""
    t_roms = tmp_path / "drive-A" / "ROMs"
    t_esde = tmp_path / "drive-B" / "esde-data"
    _write_gamelist(tree["gl_path"], [{"path": "./Good.zip", "rating": "0.9"}])
    _make_file(tree["roms_dir"]  / "snes" / "Good.zip")
    _make_file(tree["media_dir"] / "snes" / "covers" / "Good.png")

    games, _, _, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, False, False,
        tree["roms_dir"], tree["media_dir"],
    )
    copy_system(tree["system"], games, t_roms, t_esde)

    assert (t_roms / "snes" / "Good.zip").is_file()
    assert (t_esde / "gamelists" / "snes" / "gamelist.xml").is_file()
    assert (t_esde / "downloaded_media" / "snes" / "covers" / "Good.png").is_file()
    # ROMs target must NOT contain ES-DE data and vice versa.
    assert not (t_roms / "gamelists").exists()
    assert not (t_esde / "snes").exists()


def test_copy_system_skips_when_target_size_matches(tree, tmp_path):
    """overwrite=False: a same-size dst is left untouched (distinguishable
    content preserved). This is the speedup that justifies skip-existing."""
    t_roms, t_esde = _targets(tmp_path)
    _write_gamelist(tree["gl_path"], [{"path": "./Good.zip", "rating": "0.9"}])
    _make_file(tree["roms_dir"]  / "snes" / "Good.zip")  # src: b"x"
    _make_file(tree["media_dir"] / "snes" / "covers" / "Good.png")

    pre_rom = t_roms / "snes" / "Good.zip"
    pre_rom.parent.mkdir(parents=True)
    pre_rom.write_bytes(b"R")  # same size (1B), different content
    pre_cover = t_esde / "downloaded_media" / "snes" / "covers" / "Good.png"
    pre_cover.parent.mkdir(parents=True)
    pre_cover.write_bytes(b"C")

    games, _, _, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, False, False,
        tree["roms_dir"], tree["media_dir"],
    )
    copy_system(tree["system"], games, t_roms, t_esde, overwrite=False)

    assert pre_rom.read_bytes()   == b"R"
    assert pre_cover.read_bytes() == b"C"


def test_copy_system_overwrites_when_overwrite_true(tree, tmp_path):
    """overwrite=True: even same-size dsts are clobbered with src content."""
    t_roms, t_esde = _targets(tmp_path)
    _write_gamelist(tree["gl_path"], [{"path": "./Good.zip", "rating": "0.9"}])
    _make_file(tree["roms_dir"] / "snes" / "Good.zip")  # src: b"x"

    pre_rom = t_roms / "snes" / "Good.zip"
    pre_rom.parent.mkdir(parents=True)
    pre_rom.write_bytes(b"R")  # same size, different content

    games, _, _, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, False, False,
        tree["roms_dir"], tree["media_dir"],
    )
    copy_system(tree["system"], games, t_roms, t_esde, overwrite=True)

    assert pre_rom.read_bytes() == b"x"


def test_copy_system_overwrites_media_when_overwrite_true(tree, tmp_path):
    """overwrite=True must replace same-size media — not skip it the way skip-existing would."""
    t_roms, t_esde = _targets(tmp_path)
    _write_gamelist(tree["gl_path"], [{"path": "./Good.zip", "rating": "0.9"}])
    _make_file(tree["roms_dir"] / "snes" / "Good.zip")
    cover_src = tree["media_dir"] / "snes" / "covers" / "Good.png"
    cover_src.parent.mkdir(parents=True, exist_ok=True)
    cover_src.write_bytes(b"x")  # src: 1 byte

    pre_cover = t_esde / "downloaded_media" / "snes" / "covers" / "Good.png"
    pre_cover.parent.mkdir(parents=True)
    pre_cover.write_bytes(b"R")  # 1 byte, different content

    games, _, _, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, False, False,
        tree["roms_dir"], tree["media_dir"],
    )
    copy_system(tree["system"], games, t_roms, t_esde, overwrite=True)

    assert pre_cover.read_bytes() == b"x"


def test_copy_system_succeeds_on_second_run_to_same_target(tree, tmp_path):
    """Running copy_system twice must not raise FileExistsError — every mkdir
    must use exist_ok=True."""
    t_roms, t_esde = _targets(tmp_path)
    _write_gamelist(tree["gl_path"], [{"path": "./Good.zip", "rating": "0.9"}])
    _make_file(tree["roms_dir"]  / "snes" / "Good.zip")
    _make_file(tree["media_dir"] / "snes" / "covers" / "Good.png")

    games, _, _, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, False, False,
        tree["roms_dir"], tree["media_dir"],
    )
    copy_system(tree["system"], games, t_roms, t_esde)
    copy_system(tree["system"], games, t_roms, t_esde)  # must not raise


def test_copy_system_copies_when_target_size_differs(tree, tmp_path):
    """overwrite=False still copies when dst size doesn't match src — the
    skip is path+size, not path-only, so a truncated previous copy gets fixed."""
    t_roms, t_esde = _targets(tmp_path)
    _write_gamelist(tree["gl_path"], [{"path": "./Good.zip", "rating": "0.9"}])
    _make_file(tree["roms_dir"] / "snes" / "Good.zip")  # src: 1 byte

    pre_rom = t_roms / "snes" / "Good.zip"
    pre_rom.parent.mkdir(parents=True)
    pre_rom.write_bytes(b"OLDLONGER")  # 9 bytes, mismatch

    games, _, _, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, False, False,
        tree["roms_dir"], tree["media_dir"],
    )
    copy_system(tree["system"], games, t_roms, t_esde, overwrite=False)

    assert pre_rom.read_bytes() == b"x"


def test_copy_system_always_rewrites_gamelist_even_when_skipping(tree, tmp_path):
    """Gamelist.xml is the canonical metadata index — must be replaced wholesale
    even with skip-existing on, so re-scraped ratings/desc/etc. propagate."""
    t_roms, t_esde = _targets(tmp_path)
    _write_gamelist(tree["gl_path"], [{"path": "./Good.zip", "rating": "0.9"}])
    _make_file(tree["roms_dir"] / "snes" / "Good.zip")

    target_gl = t_esde / "gamelists" / "snes" / "gamelist.xml"
    target_gl.parent.mkdir(parents=True)
    target_gl.write_text(
        '<?xml version="1.0"?><gameList><game><path>./Stale.zip</path></game></gameList>',
        encoding="utf-8",
    )

    games, _, _, _ = preview_system(
        tree["system"], tree["gl_path"], 0.7, False, False,
        tree["roms_dir"], tree["media_dir"],
    )
    copy_system(tree["system"], games, t_roms, t_esde, overwrite=False)

    assert _read_target_gamelist_paths(t_esde, "snes") == ["./Good.zip"]


# ---------------------------------------------------------------------------
# main / CLI plumbing
# ---------------------------------------------------------------------------

@pytest.fixture
def main_env(tmp_path, monkeypatch):
    """Set up the minimum world for main() to run end-to-end:
    empty ESDE tree, no config file, auto-decline the copy prompt."""
    esde            = tmp_path / "esde"
    roms            = tmp_path / "roms"
    target_roms     = tmp_path / "out-roms"
    target_esde     = tmp_path / "out-esde"
    (esde / "gamelists").mkdir(parents=True)
    (esde / "downloaded_media").mkdir()
    roms.mkdir()

    monkeypatch.setattr(rom_filter_copy, "load_config", lambda _path: {})
    monkeypatch.setattr("builtins.input", lambda _: "y")
    return {
        "esde":        esde,
        "roms":        roms,
        "target_roms": target_roms,
        "target_esde": target_esde,
        "monkeypatch": monkeypatch,
    }


def _make_system_dir(esde: Path, system: str) -> None:
    sys_dir = esde / "gamelists" / system
    sys_dir.mkdir(parents=True, exist_ok=True)
    (sys_dir / "gamelist.xml").write_text(
        '<?xml version="1.0"?><gameList></gameList>', encoding="utf-8"
    )


def test_main_divides_cli_rating_by_ten(main_env):
    """CLI takes 0-10, internal threshold is 0-1. Pin the conversion."""
    _make_system_dir(main_env["esde"], "snes")

    captured = {}
    def fake_preview(system, gl_path, min_rating, include_unrated, copy_all, roms_dir, media_dir, **_kw):
        captured["min_rating"] = min_rating
        return [], 0, 0, []
    main_env["monkeypatch"].setattr(rom_filter_copy, "preview_system", fake_preview)
    main_env["monkeypatch"].setattr("sys.argv", [
        "rom_filter_copy.py",
        "--target-roms-dir",      str(main_env["target_roms"]),
        "--target-esde-data-dir", str(main_env["target_esde"]),
        "--roms-dir",             str(main_env["roms"]),
        "--esde-data-dir",        str(main_env["esde"]),
        "--rating",               "7.5",
    ])

    rom_filter_copy.main()
    assert captured["min_rating"] == pytest.approx(0.75)


def test_main_systems_flag_restricts_processing(main_env):
    """--systems should narrow the iteration to only the named systems."""
    for sysname in ("snes", "psx", "gba"):
        _make_system_dir(main_env["esde"], sysname)

    called_with: list[str] = []
    def fake_preview(system, *args, **kwargs):
        called_with.append(system)
        return [], 0, 0, []
    main_env["monkeypatch"].setattr(rom_filter_copy, "preview_system", fake_preview)
    main_env["monkeypatch"].setattr("sys.argv", [
        "rom_filter_copy.py",
        "--target-roms-dir",      str(main_env["target_roms"]),
        "--target-esde-data-dir", str(main_env["target_esde"]),
        "--roms-dir",             str(main_env["roms"]),
        "--esde-data-dir",        str(main_env["esde"]),
        "--systems",              "snes", "gba",
    ])

    rom_filter_copy.main()
    assert sorted(called_with) == ["gba", "snes"]


def test_main_errors_when_target_args_missing(main_env):
    """Neither --target-roms-dir nor --target-esde-data-dir set → SystemExit."""
    main_env["monkeypatch"].setattr("sys.argv", [
        "rom_filter_copy.py",
        "--roms-dir",      str(main_env["roms"]),
        "--esde-data-dir", str(main_env["esde"]),
    ])
    with pytest.raises(SystemExit):
        rom_filter_copy.main()


def test_main_errors_when_only_one_target_arg_set(main_env):
    """Setting only --target-roms-dir (or only --target-esde-data-dir) must
    still fail — neither implies the other."""
    main_env["monkeypatch"].setattr("sys.argv", [
        "rom_filter_copy.py",
        "--target-roms-dir", str(main_env["target_roms"]),
        "--roms-dir",        str(main_env["roms"]),
        "--esde-data-dir",   str(main_env["esde"]),
    ])
    with pytest.raises(SystemExit):
        rom_filter_copy.main()


def _disk_check_argv(main_env):
    return [
        "rom_filter_copy.py",
        "--target-roms-dir",      str(main_env["target_roms"]),
        "--target-esde-data-dir", str(main_env["target_esde"]),
        "--roms-dir",             str(main_env["roms"]),
        "--esde-data-dir",        str(main_env["esde"]),
    ]


def _install_fake_preview(main_env, rom_bytes: int, media_bytes: int,
                          copy_rom_bytes: int | None = None,
                          copy_media_bytes: int | None = None):
    """One included game with the requested per-destination byte split.
    copy_* defaults to the full size (treat as a fresh target, no skips)."""
    crb = rom_bytes if copy_rom_bytes is None else copy_rom_bytes
    cmb = media_bytes if copy_media_bytes is None else copy_media_bytes
    def fake_preview(system, *args, **kwargs):
        return [{
            "game":             ET.Element("game"),
            "rom_filename":     Path("Fake.zip"),
            "src_rom":          Path("/nonexistent/Fake.zip"),
            "media_files":      [],
            "rom_bytes":        rom_bytes,
            "media_bytes":      media_bytes,
            "bytes":            rom_bytes + media_bytes,
            "copy_rom_bytes":   crb,
            "copy_media_bytes": cmb,
        }], 0, 0, []
    main_env["monkeypatch"].setattr(rom_filter_copy, "preview_system", fake_preview)


def test_main_errors_on_insufficient_disk_space_roms(main_env):
    """ROMs target is too full → exit before the prompt with a clear message."""
    _make_system_dir(main_env["esde"], "snes")
    _install_fake_preview(main_env, rom_bytes=10_000_000, media_bytes=1)

    def fake_free(path: Path) -> int:
        return 1 if path == main_env["target_roms"] else 10**12
    main_env["monkeypatch"].setattr(rom_filter_copy, "_free_space", fake_free)
    main_env["monkeypatch"].setattr("sys.argv", _disk_check_argv(main_env))

    with pytest.raises(SystemExit) as exc:
        rom_filter_copy.main()
    msg = str(exc.value)
    assert "Not enough free space" in msg
    assert "--target-roms-dir" in msg


def test_main_errors_on_insufficient_disk_space_esde(main_env):
    """ES-DE target is too full → exit before the prompt, identifies the right target."""
    _make_system_dir(main_env["esde"], "snes")
    _install_fake_preview(main_env, rom_bytes=1, media_bytes=10_000_000)

    def fake_free(path: Path) -> int:
        return 1 if path == main_env["target_esde"] else 10**12
    main_env["monkeypatch"].setattr(rom_filter_copy, "_free_space", fake_free)
    main_env["monkeypatch"].setattr("sys.argv", _disk_check_argv(main_env))

    with pytest.raises(SystemExit) as exc:
        rom_filter_copy.main()
    msg = str(exc.value)
    assert "Not enough free space" in msg
    assert "--target-esde-data-dir" in msg


def test_main_passes_disk_check_when_enough_space(main_env):
    """Both targets have plenty of room → no exit; auto-declined prompt
    returns normally."""
    _make_system_dir(main_env["esde"], "snes")
    _install_fake_preview(main_env, rom_bytes=1_000, media_bytes=1_000)
    main_env["monkeypatch"].setattr(rom_filter_copy, "_free_space", lambda _p: 10**12)
    main_env["monkeypatch"].setattr("sys.argv", _disk_check_argv(main_env))

    rom_filter_copy.main()


def test_main_skips_system_dir_with_no_gamelist_xml(main_env):
    """A system folder under gamelists/ but with no gamelist.xml inside is
    silently skipped — preview_system is never invoked for it."""
    # 'snes' has a gamelist.xml (normal); 'psx' is an empty dir.
    _make_system_dir(main_env["esde"], "snes")
    (main_env["esde"] / "gamelists" / "psx").mkdir()

    called_with: list[str] = []
    def fake_preview(system, *args, **kwargs):
        called_with.append(system)
        return [], 0, 0, []
    main_env["monkeypatch"].setattr(rom_filter_copy, "preview_system", fake_preview)
    main_env["monkeypatch"].setattr("sys.argv", _disk_check_argv(main_env))

    rom_filter_copy.main()
    assert called_with == ["snes"]  # 'psx' skipped, didn't reach preview


def test_main_warns_and_continues_on_malformed_gamelist(main_env, capsys):
    """One bad gamelist.xml must not kill the whole run — other systems
    keep going, with a stderr warning identifying the culprit."""
    # 'bad' has malformed XML; 'snes' is well-formed and reachable.
    bad_dir = main_env["esde"] / "gamelists" / "bad"
    bad_dir.mkdir()
    (bad_dir / "gamelist.xml").write_text("<gameList><game><path>", encoding="utf-8")
    _make_system_dir(main_env["esde"], "snes")

    main_env["monkeypatch"].setattr("sys.argv", _disk_check_argv(main_env))
    rom_filter_copy.main()

    err = capsys.readouterr().err
    assert "bad" in err and "malformed" in err


def test_main_eof_at_confirm_prompt_aborts_cleanly(main_env, capsys):
    """Ctrl-D / closed stdin at the prompt → sys.exit(1) with 'Aborted.' message."""
    _make_system_dir(main_env["esde"], "snes")
    _install_fake_preview(main_env, rom_bytes=1, media_bytes=1)
    main_env["monkeypatch"].setattr(rom_filter_copy, "_free_space", lambda _p: 10**12)
    def raise_eof(_prompt):
        raise EOFError
    main_env["monkeypatch"].setattr("builtins.input", raise_eof)
    main_env["monkeypatch"].setattr("sys.argv", _disk_check_argv(main_env))

    with pytest.raises(SystemExit) as exc:
        rom_filter_copy.main()
    assert exc.value.code == 1
    assert "Aborted" in capsys.readouterr().out


def test_main_errors_when_systems_filter_matches_nothing(main_env):
    """--systems with names that match no real system dir → exit with a
    clear error rather than silently doing nothing."""
    _make_system_dir(main_env["esde"], "snes")
    main_env["monkeypatch"].setattr("sys.argv", _disk_check_argv(main_env) + [
        "--systems", "nonexistent",
    ])
    with pytest.raises(SystemExit) as exc:
        rom_filter_copy.main()
    assert "matched no available systems" in str(exc.value)


def test_main_skip_systems_excludes_named_systems(main_env):
    """--skip-systems should exclude listed systems and process the rest."""
    for sysname in ("snes", "psx", "gba"):
        _make_system_dir(main_env["esde"], sysname)

    called_with: list[str] = []
    def fake_preview(system, *args, **kwargs):
        called_with.append(system)
        return [], 0, 0, []
    main_env["monkeypatch"].setattr(rom_filter_copy, "preview_system", fake_preview)
    main_env["monkeypatch"].setattr("sys.argv", [
        "rom_filter_copy.py",
        "--target-roms-dir",      str(main_env["target_roms"]),
        "--target-esde-data-dir", str(main_env["target_esde"]),
        "--roms-dir",             str(main_env["roms"]),
        "--esde-data-dir",        str(main_env["esde"]),
        "--skip-systems",         "psx",
    ])

    rom_filter_copy.main()
    assert sorted(called_with) == ["gba", "snes"]


def test_main_errors_when_skip_systems_excludes_all(main_env):
    """--skip-systems that excludes every available system → exit with a clear error."""
    _make_system_dir(main_env["esde"], "snes")
    main_env["monkeypatch"].setattr("sys.argv", _disk_check_argv(main_env) + [
        "--skip-systems", "snes",
    ])
    with pytest.raises(SystemExit) as exc:
        rom_filter_copy.main()
    assert "excluded all available systems" in str(exc.value)


def test_main_errors_when_systems_and_skip_systems_both_given(main_env):
    """--systems and --skip-systems are mutually exclusive → exit with error."""
    _make_system_dir(main_env["esde"], "snes")
    main_env["monkeypatch"].setattr("sys.argv", _disk_check_argv(main_env) + [
        "--systems", "snes",
        "--skip-systems", "psx",
    ])
    with pytest.raises(SystemExit):
        rom_filter_copy.main()


def test_main_errors_when_roms_dir_does_not_exist(main_env):
    """Bad --roms-dir path → fail fast with a pointed error, not 'all games missing'."""
    _make_system_dir(main_env["esde"], "snes")
    main_env["monkeypatch"].setattr("sys.argv", [
        "rom_filter_copy.py",
        "--target-roms-dir",      str(main_env["target_roms"]),
        "--target-esde-data-dir", str(main_env["target_esde"]),
        "--roms-dir",             "/nonexistent/roms",
        "--esde-data-dir",        str(main_env["esde"]),
    ])
    with pytest.raises(SystemExit) as exc:
        rom_filter_copy.main()
    assert "--roms-dir" in str(exc.value)


def test_main_errors_when_gamelists_subdir_missing(main_env, tmp_path):
    """--esde-data-dir exists but has no gamelists/ subdir → user hasn't
    scraped yet. Error message should say so explicitly."""
    bare_esde = tmp_path / "bare-esde"
    bare_esde.mkdir()
    main_env["monkeypatch"].setattr("sys.argv", [
        "rom_filter_copy.py",
        "--target-roms-dir",      str(main_env["target_roms"]),
        "--target-esde-data-dir", str(main_env["target_esde"]),
        "--roms-dir",             str(main_env["roms"]),
        "--esde-data-dir",        str(bare_esde),
    ])
    with pytest.raises(SystemExit) as exc:
        rom_filter_copy.main()
    assert "gamelists/" in str(exc.value)


def test_main_abort_exits_with_code_1(main_env):
    """Answering 'n' at the prompt exits with code 1, not 0."""
    _make_system_dir(main_env["esde"], "snes")
    main_env["monkeypatch"].setattr("builtins.input", lambda _: "n")
    main_env["monkeypatch"].setattr("sys.argv", _disk_check_argv(main_env))
    with pytest.raises(SystemExit) as exc:
        rom_filter_copy.main()
    assert exc.value.code == 1


def test_main_yes_flag_bypasses_prompt(main_env):
    """--yes skips the confirmation prompt entirely."""
    _make_system_dir(main_env["esde"], "snes")
    def must_not_be_called(_prompt):
        raise AssertionError("input() must not be called with --yes")
    main_env["monkeypatch"].setattr("builtins.input", must_not_be_called)
    main_env["monkeypatch"].setattr("sys.argv", _disk_check_argv(main_env) + ["--yes"])
    rom_filter_copy.main()  # must not raise


def test_main_dry_run_skips_copy(main_env, capsys):
    """--dry-run prints preview and 'Dry run' message but never calls copy_system."""
    _make_system_dir(main_env["esde"], "snes")
    _install_fake_preview(main_env, rom_bytes=1, media_bytes=1)
    main_env["monkeypatch"].setattr(rom_filter_copy, "_free_space", lambda _p: 10**12)
    copy_called = []
    main_env["monkeypatch"].setattr(rom_filter_copy, "copy_system",
                                    lambda *a, **kw: copy_called.append(True))
    main_env["monkeypatch"].setattr("sys.argv", _disk_check_argv(main_env) + ["--dry-run"])
    rom_filter_copy.main()
    assert "Dry run" in capsys.readouterr().out
    assert not copy_called


def test_main_dry_run_and_yes_are_mutually_exclusive(main_env):
    """--dry-run and --yes together must exit non-zero (usage error)."""
    _make_system_dir(main_env["esde"], "snes")
    main_env["monkeypatch"].setattr("sys.argv",
                                    _disk_check_argv(main_env) + ["--dry-run", "--yes"])
    with pytest.raises(SystemExit):
        rom_filter_copy.main()


def test_main_verbose_lists_game_titles(main_env, capsys):
    """--verbose prints each game's <name> element under the system line."""
    snes_dir = main_env["esde"] / "gamelists" / "snes"
    snes_dir.mkdir(parents=True)
    (snes_dir / "gamelist.xml").write_text(
        '<?xml version="1.0"?><gameList>'
        '<game><path>./Good.zip</path><rating>0.9</rating><name>Super Mario World</name></game>'
        '</gameList>',
        encoding="utf-8",
    )
    _make_file(main_env["roms"] / "snes" / "Good.zip")
    main_env["monkeypatch"].setattr(rom_filter_copy, "_free_space", lambda _p: 10**12)
    main_env["monkeypatch"].setattr("sys.argv", _disk_check_argv(main_env) + ["--verbose"])
    rom_filter_copy.main()
    assert "Super Mario World" in capsys.readouterr().out


def test_main_verbose_falls_back_to_stem_when_no_name(main_env, capsys):
    """--verbose falls back to the ROM stem when the game has no <name> element."""
    snes_dir = main_env["esde"] / "gamelists" / "snes"
    snes_dir.mkdir(parents=True)
    (snes_dir / "gamelist.xml").write_text(
        '<?xml version="1.0"?><gameList>'
        '<game><path>./NoName.zip</path><rating>0.9</rating></game>'
        '</gameList>',
        encoding="utf-8",
    )
    _make_file(main_env["roms"] / "snes" / "NoName.zip")
    main_env["monkeypatch"].setattr(rom_filter_copy, "_free_space", lambda _p: 10**12)
    main_env["monkeypatch"].setattr("sys.argv", _disk_check_argv(main_env) + ["--verbose"])
    rom_filter_copy.main()
    assert "NoName" in capsys.readouterr().out


def test_main_copy_loop_shows_progress_counter(main_env, capsys):
    """Copy output includes (idx/total) counter for each system."""
    _make_system_dir(main_env["esde"], "snes")
    _install_fake_preview(main_env, rom_bytes=1, media_bytes=1)
    main_env["monkeypatch"].setattr(rom_filter_copy, "_free_space", lambda _p: 10**12)
    main_env["monkeypatch"].setattr(rom_filter_copy, "copy_system", lambda *a, **kw: None)
    main_env["monkeypatch"].setattr("sys.argv", _disk_check_argv(main_env))
    rom_filter_copy.main()
    assert "(1/1 systems)" in capsys.readouterr().out


def test_main_list_systems_prints_and_exits(main_env, capsys):
    """--list-systems prints sorted system names and returns without copying."""
    for sysname in ("nes", "snes"):
        _make_system_dir(main_env["esde"], sysname)
    copy_called = []
    main_env["monkeypatch"].setattr(rom_filter_copy, "copy_system",
                                    lambda *a, **kw: copy_called.append(True))
    main_env["monkeypatch"].setattr("sys.argv", [
        "rom_filter_copy.py",
        "--roms-dir",      str(main_env["roms"]),
        "--esde-data-dir", str(main_env["esde"]),
        "--list-systems",
    ])
    rom_filter_copy.main()  # must not raise SystemExit
    assert capsys.readouterr().out.strip().splitlines() == ["nes", "snes"]
    assert not copy_called


def test_main_copy_all_systems_flag_overrides_config(main_env):
    """--copy-all-systems nes sets copy_all=True for nes and False for snes."""
    for sysname in ("nes", "snes"):
        _make_system_dir(main_env["esde"], sysname)
    captured: dict[str, bool] = {}
    def fake_preview(system, gl_path, min_rating, include_unrated, copy_all, roms_dir, media_dir, **_kw):
        captured[system] = copy_all
        return [], 0, 0, []
    main_env["monkeypatch"].setattr(rom_filter_copy, "preview_system", fake_preview)
    main_env["monkeypatch"].setattr("sys.argv",
                                    _disk_check_argv(main_env) + ["--copy-all-systems", "nes"])
    rom_filter_copy.main()
    assert captured["nes"]  is True
    assert captured["snes"] is False


def test_main_copy_all_systems_bare_flag_clears_config(main_env):
    """--copy-all-systems with no args overrides config copy_all_systems to empty."""
    _make_system_dir(main_env["esde"], "nes")
    main_env["monkeypatch"].setattr(
        rom_filter_copy, "load_config", lambda _: {"copy_all_systems": ["nes"]})
    captured: dict[str, bool] = {}
    def fake_preview(system, gl_path, min_rating, include_unrated, copy_all, roms_dir, media_dir, **_kw):
        captured[system] = copy_all
        return [], 0, 0, []
    main_env["monkeypatch"].setattr(rom_filter_copy, "preview_system", fake_preview)
    main_env["monkeypatch"].setattr("sys.argv",
                                    _disk_check_argv(main_env) + ["--copy-all-systems"])
    rom_filter_copy.main()
    assert captured["nes"] is False


def test_main_happy_path_runs_copy_and_skips_empty_systems(main_env, capsys):
    """End-to-end through main(): user answers 'y', copy runs, ROM and gamelist
    land on target. Also exercises the 'if not games: continue' skip for a
    second system whose only game is filtered out by rating."""
    # System with one passing game.
    snes_dir = main_env["esde"] / "gamelists" / "snes"
    snes_dir.mkdir(parents=True)
    (snes_dir / "gamelist.xml").write_text(
        '<?xml version="1.0"?><gameList><game>'
        '<path>./Good.zip</path><rating>0.9</rating>'
        '</game></gameList>',
        encoding="utf-8",
    )
    _make_file(main_env["roms"] / "snes" / "Good.zip")

    # System whose only game falls below the threshold → empty plan entry.
    empty_dir = main_env["esde"] / "gamelists" / "empty"
    empty_dir.mkdir(parents=True)
    (empty_dir / "gamelist.xml").write_text(
        '<?xml version="1.0"?><gameList><game>'
        '<path>./Mid.zip</path><rating>0.5</rating>'
        '</game></gameList>',
        encoding="utf-8",
    )
    _make_file(main_env["roms"] / "empty" / "Mid.zip")

    main_env["monkeypatch"].setattr("builtins.input", lambda _p: "y")
    main_env["monkeypatch"].setattr("sys.argv", _disk_check_argv(main_env) + [
        "--rating", "8.0",
    ])

    rom_filter_copy.main()

    # snes: copied. empty: skipped via 'if not games: continue', no system dir created.
    assert (main_env["target_roms"] / "snes" / "Good.zip").is_file()
    assert (main_env["target_esde"] / "gamelists" / "snes" / "gamelist.xml").is_file()
    assert not (main_env["target_roms"] / "empty").exists()
    assert not (main_env["target_esde"] / "gamelists" / "empty").exists()
    assert "Done." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# per-system rating overrides
# ---------------------------------------------------------------------------

def test_main_system_ratings_config_applies_per_system(main_env):
    """Config system_ratings overrides min_rating for matching systems only."""
    for sysname in ("n3ds", "snes"):
        _make_system_dir(main_env["esde"], sysname)
    main_env["monkeypatch"].setattr(
        rom_filter_copy, "load_config",
        lambda _: {"system_ratings": {"n3ds": 7.5}},
    )
    captured: dict[str, float] = {}
    def fake_preview(system, gl_path, min_rating, include_unrated, copy_all, roms_dir, media_dir, **_kw):
        captured[system] = min_rating
        return [], 0, 0, []
    main_env["monkeypatch"].setattr(rom_filter_copy, "preview_system", fake_preview)
    main_env["monkeypatch"].setattr("sys.argv", _disk_check_argv(main_env) + ["--rating", "7.0"])
    rom_filter_copy.main()
    assert captured["n3ds"] == pytest.approx(0.75)
    assert captured["snes"] == pytest.approx(0.70)


def test_main_system_ratings_cli_overrides_config(main_env):
    """--system-ratings CLI flag overrides the config value for that system."""
    _make_system_dir(main_env["esde"], "n3ds")
    main_env["monkeypatch"].setattr(
        rom_filter_copy, "load_config",
        lambda _: {"system_ratings": {"n3ds": 7.0}},
    )
    captured: dict[str, float] = {}
    def fake_preview(system, gl_path, min_rating, include_unrated, copy_all, roms_dir, media_dir, **_kw):
        captured[system] = min_rating
        return [], 0, 0, []
    main_env["monkeypatch"].setattr(rom_filter_copy, "preview_system", fake_preview)
    main_env["monkeypatch"].setattr(
        "sys.argv", _disk_check_argv(main_env) + ["--system-ratings", "n3ds=8.0"],
    )
    rom_filter_copy.main()
    assert captured["n3ds"] == pytest.approx(0.80)


def test_main_system_ratings_invalid_format_exits(main_env):
    """--system-ratings with a bad value (no '=') exits non-zero."""
    _make_system_dir(main_env["esde"], "snes")
    main_env["monkeypatch"].setattr(
        "sys.argv", _disk_check_argv(main_env) + ["--system-ratings", "badvalue"],
    )
    with pytest.raises(SystemExit) as exc:
        rom_filter_copy.main()
    assert exc.value.code != 0

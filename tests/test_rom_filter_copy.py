"""Tests for rom_filter_copy."""

import math
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import _config
import _copy as _copy_mod
import _preview
import rom_filter_copy
from _copy import copy_system, delete_pruned
from _filters import (
    expand_raw_genre_ratings,
    expand_raw_genres,
    format_size,
    parse_rating,
    parse_rom_path,
    should_include,
)
from _media import build_media_index, parse_m3u
from _preview import PreviewOptions, preview_system


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
# parse_m3u
# ---------------------------------------------------------------------------

def test_parse_m3u_empty_file(tmp_path):
    m3u = tmp_path / "Game.m3u"
    m3u.write_text("", encoding="utf-8")
    assert parse_m3u(m3u) == []


def test_parse_m3u_comment_lines_only(tmp_path):
    m3u = tmp_path / "Game.m3u"
    m3u.write_text("# disc 1\n# disc 2\n", encoding="utf-8")
    assert parse_m3u(m3u) == []


def test_parse_m3u_returns_paths_relative_to_m3u_dir(tmp_path):
    m3u = tmp_path / "Game.m3u"
    m3u.write_text("Game (Disc 1).bin\nGame (Disc 2).bin\n", encoding="utf-8")
    assert parse_m3u(m3u) == [
        tmp_path / "Game (Disc 1).bin",
        tmp_path / "Game (Disc 2).bin",
    ]


def test_parse_m3u_strips_whitespace_from_lines(tmp_path):
    m3u = tmp_path / "Game.m3u"
    m3u.write_text("  Game (Disc 1).bin  \n  Game (Disc 2).bin\n", encoding="utf-8")
    assert parse_m3u(m3u) == [
        tmp_path / "Game (Disc 1).bin",
        tmp_path / "Game (Disc 2).bin",
    ]


def test_parse_m3u_skips_blank_lines(tmp_path):
    m3u = tmp_path / "Game.m3u"
    m3u.write_text("Game (Disc 1).bin\n\nGame (Disc 2).bin\n", encoding="utf-8")
    assert len(parse_m3u(m3u)) == 2


def test_parse_m3u_missing_file_returns_empty():
    assert parse_m3u(Path("/nonexistent/Game.m3u")) == []


def test_parse_m3u_mixed_comments_and_discs(tmp_path):
    m3u = tmp_path / "Game.m3u"
    m3u.write_text("# playlist\nGame (Disc 1).bin\n# note\nGame (Disc 2).bin\n", encoding="utf-8")
    result = parse_m3u(m3u)
    assert len(result) == 2
    assert result[0].name == "Game (Disc 1).bin"
    assert result[1].name == "Game (Disc 2).bin"


# ---------------------------------------------------------------------------
# preview_system (end-to-end against a fake gamelist + rom/media tree)
# ---------------------------------------------------------------------------

def _write_gamelist(path: Path, games: list[dict]) -> None:
    """Write a minimal gamelist.xml. Each game dict supports keys: path, rating, genre."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ['<?xml version="1.0"?>', "<gameList>"]
    for g in games:
        lines.append("  <game>")
        if "path" in g:
            lines.append(f"    <path>{g['path']}</path>")
        if "rating" in g:
            lines.append(f"    <rating>{g['rating']}</rating>")
        if "genre" in g:
            lines.append(f"    <genre>{g['genre']}</genre>")
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
    games, skipped, missing, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    assert (games, skipped, missing) == ([], 0, 0)


def test_preview_includes_above_threshold(tree):
    _write_gamelist(tree["gl_path"], [{"path": "./Good.zip", "rating": "0.85"}])
    rom_src   = _make_file(tree["roms_dir"] / "snes" / "Good.zip")
    cover_src = _make_file(tree["media_dir"] / "snes" / "covers" / "Good.png")

    games, skipped, missing, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
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

    games, skipped, missing, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    assert (games, skipped, missing) == ([], 1, 0)


def test_preview_unrated_excluded_by_default(tree):
    _write_gamelist(tree["gl_path"], [{"path": "./Unknown.zip"}])
    _make_file(tree["roms_dir"] / "snes" / "Unknown.zip")

    games, skipped, missing, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    assert (len(games), skipped, missing) == (0, 1, 0)


def test_preview_unrated_included_with_flag(tree):
    _write_gamelist(tree["gl_path"], [{"path": "./Unknown.zip"}])
    _make_file(tree["roms_dir"] / "snes" / "Unknown.zip")

    games, skipped, missing, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=True, copy_all=False))
    assert (len(games), skipped, missing) == (1, 0, 0)


def test_preview_missing_rom_counted_separately(tree):
    _write_gamelist(tree["gl_path"], [{"path": "./Ghost.zip", "rating": "0.9"}])
    # No ROM file on disk.

    games, skipped, missing, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    assert (len(games), skipped, missing) == (0, 0, 1)


def test_preview_copy_all_overrides_rating(tree):
    _write_gamelist(tree["gl_path"], [
        {"path": "./Low.zip", "rating": "0.1"},
        {"path": "./None.zip"},
    ])
    _make_file(tree["roms_dir"] / "snes" / "Low.zip")
    _make_file(tree["roms_dir"] / "snes" / "None.zip")

    games, skipped, missing, _ = preview_system(
        tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"],
        PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=True),
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

    games, _, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    assert len(games) == 1
    assert games[0]["media_files"] == [cover]


def test_preview_empty_path_element_ignored(tree):
    _write_gamelist(tree["gl_path"], [
        {"path": "", "rating": "0.9"},
        {"path": "./Real.zip", "rating": "0.9"},
    ])
    _make_file(tree["roms_dir"] / "snes" / "Real.zip")

    games, skipped, missing, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    # Empty path entry is dropped silently (not counted as skipped or missing).
    assert (len(games), skipped, missing) == (1, 0, 0)


def test_preview_malformed_rating_treated_as_unrated(tree):
    _write_gamelist(tree["gl_path"], [{"path": "./Game.zip", "rating": "not-a-number"}])
    _make_file(tree["roms_dir"] / "snes" / "Game.zip")

    # Without --include-unrated: skipped.
    games, skipped, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    assert (len(games), skipped) == (0, 1)

    # With --include-unrated: included.
    games, skipped, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=True, copy_all=False))
    assert (len(games), skipped) == (1, 0)


def test_preview_counts_all_skipped_games(tree):
    _write_gamelist(tree["gl_path"], [
        {"path": "./Bad1.zip", "rating": "0.3"},
        {"path": "./Bad2.zip", "rating": "0.4"},
    ])
    _make_file(tree["roms_dir"] / "snes" / "Bad1.zip")
    _make_file(tree["roms_dir"] / "snes" / "Bad2.zip")

    games, skipped, missing, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    assert (len(games), skipped, missing) == (0, 2, 0)


def test_preview_continues_after_skipped_game(tree):
    """Skipped game does not stop iteration — subsequent qualifying games are included."""
    _write_gamelist(tree["gl_path"], [
        {"path": "./Bad.zip",  "rating": "0.3"},
        {"path": "./Good.zip", "rating": "0.9"},
    ])
    _make_file(tree["roms_dir"] / "snes" / "Bad.zip")
    _make_file(tree["roms_dir"] / "snes" / "Good.zip")

    games, skipped, missing, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    assert len(games) == 1
    assert skipped == 1
    assert games[0]["rom_filename"] == Path("Good.zip")


def test_preview_counts_all_missing_roms(tree):
    _write_gamelist(tree["gl_path"], [
        {"path": "./Ghost1.zip", "rating": "0.9"},
        {"path": "./Ghost2.zip", "rating": "0.9"},
    ])
    # Neither ROM exists on disk.

    games, skipped, missing, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    assert (len(games), skipped, missing) == (0, 0, 2)


def test_preview_continues_after_missing_rom(tree):
    """Missing ROM does not stop iteration — subsequent present ROMs are included."""
    _write_gamelist(tree["gl_path"], [
        {"path": "./Ghost.zip", "rating": "0.9"},
        {"path": "./Good.zip",  "rating": "0.9"},
    ])
    # Ghost.zip absent; Good.zip present.
    _make_file(tree["roms_dir"] / "snes" / "Good.zip")

    games, skipped, missing, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    assert len(games) == 1
    assert missing == 1
    assert games[0]["rom_filename"] == Path("Good.zip")


def test_preview_copy_bytes_equal_full_size_when_no_targets(tree):
    """Without skip-existing targets, copy_rom_bytes and copy_media_bytes equal
    the full source sizes (no subtraction for already-present files)."""
    _write_gamelist(tree["gl_path"], [{"path": "./Good.zip", "rating": "0.9"}])
    _make_file(tree["roms_dir"]  / "snes" / "Good.zip")
    _make_file(tree["media_dir"] / "snes" / "covers" / "Good.png")

    games, _, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    assert games[0]["copy_rom_bytes"]   == games[0]["rom_bytes"]
    assert games[0]["copy_media_bytes"] == games[0]["media_bytes"]


# ---------------------------------------------------------------------------
# preview_system — genre filtering
# ---------------------------------------------------------------------------

def test_preview_genre_include_keeps_matching_genre(tree):
    _write_gamelist(tree["gl_path"], [
        {"path": "./Action.zip", "rating": "0.9", "genre": "Action"},
        {"path": "./RPG.zip",    "rating": "0.9", "genre": "RPG"},
    ])
    _make_file(tree["roms_dir"] / "snes" / "Action.zip")
    _make_file(tree["roms_dir"] / "snes" / "RPG.zip")

    games, skipped, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False, genres={"Action"}))
    assert len(games) == 1
    assert games[0]["rom_filename"] == Path("Action.zip")
    assert skipped == 1


def test_preview_genre_include_excludes_no_genre_games(tree):
    """genres include-set drops games with no <genre> element (they don't match any genre)."""
    _write_gamelist(tree["gl_path"], [
        {"path": "./Tagged.zip",   "rating": "0.9", "genre": "Action"},
        {"path": "./Untagged.zip", "rating": "0.9"},
    ])
    _make_file(tree["roms_dir"] / "snes" / "Tagged.zip")
    _make_file(tree["roms_dir"] / "snes" / "Untagged.zip")

    games, skipped, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False, genres={"Action"}))
    assert len(games) == 1
    assert games[0]["rom_filename"] == Path("Tagged.zip")
    assert skipped == 1


def test_preview_genre_skip_excludes_matching_genre(tree):
    _write_gamelist(tree["gl_path"], [
        {"path": "./Action.zip", "rating": "0.9", "genre": "Action"},
        {"path": "./RPG.zip",    "rating": "0.9", "genre": "RPG"},
    ])
    _make_file(tree["roms_dir"] / "snes" / "Action.zip")
    _make_file(tree["roms_dir"] / "snes" / "RPG.zip")

    games, skipped, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False, skip_genres={"Action"}))
    assert len(games) == 1
    assert games[0]["rom_filename"] == Path("RPG.zip")
    assert skipped == 1


def test_preview_genre_skip_passes_through_no_genre_games(tree):
    """skip_genres does not drop games with no <genre> element — they don't match the exclusion."""
    _write_gamelist(tree["gl_path"], [
        {"path": "./Untagged.zip", "rating": "0.9"},
        {"path": "./Casino.zip",   "rating": "0.9", "genre": "Casino"},
    ])
    _make_file(tree["roms_dir"] / "snes" / "Untagged.zip")
    _make_file(tree["roms_dir"] / "snes" / "Casino.zip")

    games, skipped, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False, skip_genres={"Casino"}))
    assert len(games) == 1
    assert games[0]["rom_filename"] == Path("Untagged.zip")
    assert skipped == 1


def test_preview_genre_filter_applied_after_rating_filter(tree):
    """Rating filter is checked first; a below-threshold game is skipped by rating,
    not genre — genre filter never sees it."""
    _write_gamelist(tree["gl_path"], [
        {"path": "./Low.zip", "rating": "0.3", "genre": "Action"},
    ])
    _make_file(tree["roms_dir"] / "snes" / "Low.zip")

    games, skipped, _, skipped_details = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False, genres={"Action"}))
    assert len(games) == 0
    assert skipped == 1
    assert len(skipped_details) == 1


def test_preview_genre_include_exact_matches_only(tree):
    """genres uses exact matching — pass expanded raw strings to match subgenres."""
    _write_gamelist(tree["gl_path"], [
        {"path": "./Soccer.zip",  "rating": "0.9", "genre": "Sports / Football (Soccer)"},
        {"path": "./Baseball.zip","rating": "0.9", "genre": "Sports / Baseball"},
        {"path": "./RPG.zip",     "rating": "0.9", "genre": "Role Playing Game"},
    ])
    for name in ("Soccer.zip", "Baseball.zip", "RPG.zip"):
        _make_file(tree["roms_dir"] / "snes" / name)

    # Bare "Sports" no longer matches subgenres — pass the expanded raw strings.
    games, skipped, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False, genres={"Sports / Football (Soccer)", "Sports / Baseball"}))
    assert sorted(g["rom_filename"].name for g in games) == ["Baseball.zip", "Soccer.zip"]
    assert skipped == 1


def test_preview_genre_skip_exact_matches_only(tree):
    """skip_genres uses exact matching — bare 'Sports' does not exclude 'Sports / *'."""
    _write_gamelist(tree["gl_path"], [
        {"path": "./Soccer.zip",  "rating": "0.9", "genre": "Sports / Football (Soccer)"},
        {"path": "./RPG.zip",     "rating": "0.9", "genre": "Role Playing Game"},
    ])
    for name in ("Soccer.zip", "RPG.zip"):
        _make_file(tree["roms_dir"] / "snes" / name)

    # Pass the expanded raw string to exclude the subgenre.
    games, skipped, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False, skip_genres={"Sports / Football (Soccer)"}))
    assert len(games) == 1
    assert games[0]["rom_filename"].name == "RPG.zip"
    assert skipped == 1


def test_preview_genre_include_multiple_exact(tree):
    """Multiple entries in genres act as OR against exact raw strings."""
    _write_gamelist(tree["gl_path"], [
        {"path": "./Shoot.zip",    "rating": "0.9", "genre": "Shoot'em Up / Vertical"},
        {"path": "./Shooter.zip",  "rating": "0.9", "genre": "Shooter / FPV"},
        {"path": "./Puzzle.zip",   "rating": "0.9", "genre": "Puzzle"},
    ])
    for name in ("Shoot.zip", "Shooter.zip", "Puzzle.zip"):
        _make_file(tree["roms_dir"] / "snes" / name)

    games, skipped, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False, genres={"Shoot'em Up / Vertical", "Shooter / FPV"}))
    assert sorted(g["rom_filename"].name for g in games) == ["Shoot.zip", "Shooter.zip"]
    assert skipped == 1


def test_preview_no_genre_filter_includes_all_genres(tree):
    """genres=None and skip_genres=None → genre is irrelevant, all rated games pass."""
    _write_gamelist(tree["gl_path"], [
        {"path": "./A.zip", "rating": "0.9", "genre": "Action"},
        {"path": "./B.zip", "rating": "0.9", "genre": "RPG"},
        {"path": "./C.zip", "rating": "0.9"},
    ])
    for name in ("A.zip", "B.zip", "C.zip"):
        _make_file(tree["roms_dir"] / "snes" / name)

    games, skipped, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    assert len(games) == 3
    assert skipped == 0


# ---------------------------------------------------------------------------
# preview_system — genre rating overrides
# ---------------------------------------------------------------------------

def test_preview_genre_rating_raises_bar_for_genre(tree):
    """A genre_rating override filters games that would pass the global threshold."""
    _write_gamelist(tree["gl_path"], [
        {"path": "./Soccer.zip", "rating": "0.75", "genre": "Sports"},
        {"path": "./RPG.zip",    "rating": "0.75", "genre": "RPG"},
    ])
    _make_file(tree["roms_dir"] / "snes" / "Soccer.zip")
    _make_file(tree["roms_dir"] / "snes" / "RPG.zip")

    # Global threshold 0.7; Sports requires 0.9 → Soccer filtered out, RPG passes.
    games, skipped, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False, genre_ratings={"Sports": 0.9}))
    assert len(games) == 1
    assert games[0]["rom_filename"].name == "RPG.zip"
    assert skipped == 1


def test_preview_genre_rating_stronger_always_wins(tree):
    """When system min_rating and genre_rating differ, the higher (stricter) one applies."""
    _write_gamelist(tree["gl_path"], [
        {"path": "./Soccer.zip", "rating": "0.7", "genre": "Sports"},
    ])
    _make_file(tree["roms_dir"] / "snes" / "Soccer.zip")

    # system min_rating=0.6, genre override=0.9, game rating=0.7 → excluded (0.9 wins)
    games, skipped, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.6, include_unrated=False, copy_all=False, genre_ratings={"Sports": 0.9}))
    assert len(games) == 0
    assert skipped == 1


def test_preview_genre_rating_cannot_lower_bar(tree):
    """A genre_rating lower than the system threshold has no effect (max wins)."""
    _write_gamelist(tree["gl_path"], [
        {"path": "./Soccer.zip", "rating": "0.7", "genre": "Sports"},
    ])
    _make_file(tree["roms_dir"] / "snes" / "Soccer.zip")

    # system min_rating=0.9, genre override=0.6 → game still fails (0.9 wins via max)
    games, skipped, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.9, include_unrated=False, copy_all=False, genre_ratings={"Sports": 0.6}))
    assert len(games) == 0
    assert skipped == 1


def test_preview_genre_rating_exact_raw_string(tree):
    """genre_ratings key must be an exact raw genre string after expansion."""
    _write_gamelist(tree["gl_path"], [
        {"path": "./Soccer.zip", "rating": "0.75", "genre": "Sports / Football (Soccer)"},
        {"path": "./RPG.zip",    "rating": "0.75", "genre": "Role Playing Game"},
    ])
    _make_file(tree["roms_dir"] / "snes" / "Soccer.zip")
    _make_file(tree["roms_dir"] / "snes" / "RPG.zip")

    # Exact raw string raises bar for that genre only.
    games, skipped, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False, genre_ratings={"Sports / Football (Soccer)": 0.9}))
    assert len(games) == 1
    assert games[0]["rom_filename"].name == "RPG.zip"
    assert skipped == 1


def test_preview_genre_rating_bare_canonical_no_match(tree):
    """Without expansion, a bare canonical key 'Sports' does not match 'Sports / Football (Soccer)'."""
    _write_gamelist(tree["gl_path"], [
        {"path": "./Soccer.zip", "rating": "0.75", "genre": "Sports / Football (Soccer)"},
    ])
    _make_file(tree["roms_dir"] / "snes" / "Soccer.zip")

    # "Sports" is not the raw genre string — no override applies, game passes at 0.7.
    games, skipped, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False, genre_ratings={"Sports": 0.9}))
    assert len(games) == 1
    assert skipped == 0


def test_preview_genre_rating_unrated_passes_with_include_unrated(tree):
    """include_unrated=True still lets unrated games through even when genre has an override.
    Genre overrides raise the rating bar — they don't impose a 'must have a rating' requirement."""
    _write_gamelist(tree["gl_path"], [
        {"path": "./NoRating.zip", "genre": "Sports"},
    ])
    _make_file(tree["roms_dir"] / "snes" / "NoRating.zip")

    games, _, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=True, copy_all=False, genre_ratings={"Sports": 0.9}))
    assert len(games) == 1


def test_preview_genre_rating_copy_all_bypasses_override(tree):
    """copy_all=True includes everything regardless of rating or genre rating overrides."""
    _write_gamelist(tree["gl_path"], [
        {"path": "./Soccer.zip", "rating": "0.1", "genre": "Sports"},
    ])
    _make_file(tree["roms_dir"] / "snes" / "Soccer.zip")

    games, _, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=True, genre_ratings={"Sports": 0.9}))
    assert len(games) == 1


def test_preview_genre_rating_no_genre_not_affected(tree):
    """A game with no <genre> element is not matched by any genre_ratings key."""
    _write_gamelist(tree["gl_path"], [
        {"path": "./Untagged.zip", "rating": "0.75"},
    ])
    _make_file(tree["roms_dir"] / "snes" / "Untagged.zip")

    games, _, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False, genre_ratings={"Sports": 0.9}))
    assert len(games) == 1


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

    games, _, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False, overwrite=False, target_roms_dir=t_roms, target_esde_data_dir=t_esde))
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

    games, _, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False, overwrite=False, target_roms_dir=t_roms, target_esde_data_dir=t_esde))
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

    games, _, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False, overwrite=False, target_roms_dir=t_roms, target_esde_data_dir=t_esde))
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

    games, _, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False, overwrite=False, target_roms_dir=t_roms, target_esde_data_dir=t_esde))
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

    games, _, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
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

    games, _, missing, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
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

    games, _, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
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

    games, _, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
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

    games, _, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
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

    games, _, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
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

    games, _, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    copy_system(tree["system"], games, t_roms, t_esde, overwrite=True)

    assert pre_cover.read_bytes() == b"x"


def test_copy_system_succeeds_on_second_run_to_same_target(tree, tmp_path):
    """Running copy_system twice must not raise FileExistsError — every mkdir
    must use exist_ok=True."""
    t_roms, t_esde = _targets(tmp_path)
    _write_gamelist(tree["gl_path"], [{"path": "./Good.zip", "rating": "0.9"}])
    _make_file(tree["roms_dir"]  / "snes" / "Good.zip")
    _make_file(tree["media_dir"] / "snes" / "covers" / "Good.png")

    games, _, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
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

    games, _, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
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

    games, _, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    copy_system(tree["system"], games, t_roms, t_esde, overwrite=False)

    assert _read_target_gamelist_paths(t_esde, "snes") == ["./Good.zip"]


# ---------------------------------------------------------------------------
# preview_system — m3u multi-disc support
# ---------------------------------------------------------------------------

def _write_m3u(path: Path, disc_names: list[str]) -> None:
    """Write an m3u file listing disc filenames (one per line, relative, same dir)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(disc_names) + "\n", encoding="utf-8")


def test_preview_m3u_rom_bytes_includes_all_discs(tree):
    """rom_bytes for an m3u entry must be m3u file size + all disc image sizes."""
    roms_sys = tree["roms_dir"] / "snes"
    _write_gamelist(tree["gl_path"], [{"path": "./Game.m3u", "rating": "0.9"}])
    m3u = roms_sys / "Game.m3u"
    _write_m3u(m3u, ["Game (Disc 1).bin", "Game (Disc 2).bin"])
    (roms_sys / "Game (Disc 1).bin").write_bytes(b"A" * 100)
    (roms_sys / "Game (Disc 2).bin").write_bytes(b"B" * 200)

    games, _, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    assert len(games) == 1
    assert games[0]["rom_bytes"] == m3u.stat().st_size + 100 + 200


def test_preview_m3u_src_file_size_is_m3u_file_only(tree):
    """src_file_size is the .m3u file itself, not the combined disc total."""
    roms_sys = tree["roms_dir"] / "snes"
    _write_gamelist(tree["gl_path"], [{"path": "./Game.m3u", "rating": "0.9"}])
    m3u = roms_sys / "Game.m3u"
    _write_m3u(m3u, ["Game (Disc 1).bin"])
    (roms_sys / "Game (Disc 1).bin").write_bytes(b"X" * 500)

    games, _, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    assert games[0]["src_file_size"] == m3u.stat().st_size
    assert games[0]["src_file_size"] != games[0]["rom_bytes"]


def test_preview_m3u_discs_not_counted_as_skipped(tree):
    """Disc images belonging to an included m3u must not inflate the skipped count."""
    roms_sys = tree["roms_dir"] / "snes"
    _write_gamelist(tree["gl_path"], [{"path": "./Game.m3u", "rating": "0.9"}])
    _write_m3u(roms_sys / "Game.m3u", ["Game (Disc 1).bin", "Game (Disc 2).bin"])
    (roms_sys / "Game (Disc 1).bin").write_bytes(b"A")
    (roms_sys / "Game (Disc 2).bin").write_bytes(b"B")

    games, skipped, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    assert len(games) == 1
    assert skipped == 0


def test_preview_m3u_missing_disc_counted_as_missing(tree):
    """If any disc image referenced by an m3u is absent, the game counts as missing."""
    roms_sys = tree["roms_dir"] / "snes"
    _write_gamelist(tree["gl_path"], [{"path": "./Game.m3u", "rating": "0.9"}])
    _write_m3u(roms_sys / "Game.m3u", ["Game (Disc 1).bin", "Game (Disc 2).bin"])
    (roms_sys / "Game (Disc 1).bin").write_bytes(b"A")
    # Game (Disc 2).bin intentionally absent

    games, _, missing, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    assert len(games) == 0
    assert missing == 1


def test_preview_m3u_missing_m3u_file_counted_as_missing(tree):
    """If the .m3u file itself is absent on disk, the game counts as missing."""
    _write_gamelist(tree["gl_path"], [{"path": "./Ghost.m3u", "rating": "0.9"}])

    games, _, missing, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    assert len(games) == 0
    assert missing == 1


def test_preview_m3u_entry_has_correct_discs_tuple(tree):
    """game entry m3u_discs must be (abs_path, rel_path, size) tuples for each disc."""
    roms_sys = tree["roms_dir"] / "snes"
    _write_gamelist(tree["gl_path"], [{"path": "./Game.m3u", "rating": "0.9"}])
    _write_m3u(roms_sys / "Game.m3u", ["Game (Disc 1).bin", "Game (Disc 2).bin"])
    disc1 = roms_sys / "Game (Disc 1).bin"
    disc2 = roms_sys / "Game (Disc 2).bin"
    disc1.write_bytes(b"A" * 10)
    disc2.write_bytes(b"B" * 20)

    games, _, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    discs = games[0]["m3u_discs"]
    assert len(discs) == 2
    assert {d[0] for d in discs} == {disc1, disc2}
    assert {d[1] for d in discs} == {Path("Game (Disc 1).bin"), Path("Game (Disc 2).bin")}
    assert {d[2] for d in discs} == {10, 20}


def test_preview_m3u_media_matched_by_m3u_stem(tree):
    """Media art is looked up by the .m3u stem, not by individual disc stems."""
    roms_sys = tree["roms_dir"] / "snes"
    _write_gamelist(tree["gl_path"], [{"path": "./Game.m3u", "rating": "0.9"}])
    _write_m3u(roms_sys / "Game.m3u", ["Game (Disc 1).bin"])
    (roms_sys / "Game (Disc 1).bin").write_bytes(b"A")
    cover = _make_file(tree["media_dir"] / "snes" / "covers" / "Game.png")
    _make_file(tree["media_dir"] / "snes" / "covers" / "Game (Disc 1).png")

    games, _, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    assert games[0]["media_files"] == [cover]


def test_preview_m3u_and_regular_game_coexist(tree):
    """An m3u game and a normal ROM in the same gamelist are both handled correctly."""
    roms_sys = tree["roms_dir"] / "snes"
    _write_gamelist(tree["gl_path"], [
        {"path": "./Game.m3u",   "rating": "0.9"},
        {"path": "./Normal.zip", "rating": "0.9"},
    ])
    _write_m3u(roms_sys / "Game.m3u", ["Game (Disc 1).bin"])
    (roms_sys / "Game (Disc 1).bin").write_bytes(b"A")
    _make_file(roms_sys / "Normal.zip")

    games, skipped, missing, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    assert len(games) == 2
    assert skipped == 0
    assert missing == 0
    normal = next(g for g in games if g["rom_filename"] == Path("Normal.zip"))
    assert normal["m3u_discs"] == []
    assert normal["src_file_size"] == normal["rom_bytes"]


def test_preview_m3u_skip_existing_m3u_on_target_reduces_copy_bytes(tree, tmp_path):
    """m3u already on target at matching size → its bytes drop out of copy_rom_bytes."""
    t_roms = tmp_path / "t-roms"
    t_esde = tmp_path / "t-esde"
    roms_sys = tree["roms_dir"] / "snes"
    _write_gamelist(tree["gl_path"], [{"path": "./Game.m3u", "rating": "0.9"}])
    m3u = roms_sys / "Game.m3u"
    _write_m3u(m3u, ["Game (Disc 1).bin"])
    (roms_sys / "Game (Disc 1).bin").write_bytes(b"D" * 100)
    pre = t_roms / "snes" / "Game.m3u"
    pre.parent.mkdir(parents=True)
    pre.write_bytes(m3u.read_bytes())

    games, _, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False, overwrite=False, target_roms_dir=t_roms, target_esde_data_dir=t_esde))
    assert games[0]["copy_rom_bytes"] == 100


def test_preview_m3u_skip_existing_disc_on_target_reduces_copy_bytes(tree, tmp_path):
    """Disc already on target at matching size → its bytes drop out of copy_rom_bytes."""
    t_roms = tmp_path / "t-roms"
    t_esde = tmp_path / "t-esde"
    roms_sys = tree["roms_dir"] / "snes"
    _write_gamelist(tree["gl_path"], [{"path": "./Game.m3u", "rating": "0.9"}])
    _write_m3u(roms_sys / "Game.m3u", ["Game (Disc 1).bin"])
    (roms_sys / "Game (Disc 1).bin").write_bytes(b"D" * 100)
    pre = t_roms / "snes" / "Game (Disc 1).bin"
    pre.parent.mkdir(parents=True)
    pre.write_bytes(b"D" * 100)

    games, _, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False, overwrite=False, target_roms_dir=t_roms, target_esde_data_dir=t_esde))
    m3u_size = (roms_sys / "Game.m3u").stat().st_size
    assert games[0]["copy_rom_bytes"] == m3u_size


# ---------------------------------------------------------------------------
# copy_system — m3u multi-disc support
# ---------------------------------------------------------------------------

def test_copy_system_m3u_copies_m3u_and_all_discs(tree, tmp_path):
    """copy_system must copy the .m3u file and every disc image it references."""
    t_roms, t_esde = _targets(tmp_path)
    roms_sys = tree["roms_dir"] / "snes"
    _write_gamelist(tree["gl_path"], [{"path": "./Game.m3u", "rating": "0.9"}])
    _write_m3u(roms_sys / "Game.m3u", ["Game (Disc 1).bin", "Game (Disc 2).bin"])
    (roms_sys / "Game (Disc 1).bin").write_bytes(b"A")
    (roms_sys / "Game (Disc 2).bin").write_bytes(b"B")

    games, _, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    copy_system(tree["system"], games, t_roms, t_esde)

    assert (t_roms / "snes" / "Game.m3u").is_file()
    assert (t_roms / "snes" / "Game (Disc 1).bin").read_bytes() == b"A"
    assert (t_roms / "snes" / "Game (Disc 2).bin").read_bytes() == b"B"


def test_copy_system_m3u_gamelist_references_m3u_not_discs(tree, tmp_path):
    """The target gamelist.xml must reference the .m3u path, not disc image paths."""
    t_roms, t_esde = _targets(tmp_path)
    roms_sys = tree["roms_dir"] / "snes"
    _write_gamelist(tree["gl_path"], [{"path": "./Game.m3u", "rating": "0.9"}])
    _write_m3u(roms_sys / "Game.m3u", ["Game (Disc 1).bin"])
    (roms_sys / "Game (Disc 1).bin").write_bytes(b"A")

    games, _, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    copy_system(tree["system"], games, t_roms, t_esde)

    assert _read_target_gamelist_paths(t_esde, "snes") == ["./Game.m3u"]


def test_copy_system_m3u_skip_existing_preserves_matching_disc(tree, tmp_path):
    """overwrite=False: same-size disc already on target is not re-copied."""
    t_roms, t_esde = _targets(tmp_path)
    roms_sys = tree["roms_dir"] / "snes"
    _write_gamelist(tree["gl_path"], [{"path": "./Game.m3u", "rating": "0.9"}])
    _write_m3u(roms_sys / "Game.m3u", ["Game (Disc 1).bin"])
    (roms_sys / "Game (Disc 1).bin").write_bytes(b"x")
    pre_disc = t_roms / "snes" / "Game (Disc 1).bin"
    pre_disc.parent.mkdir(parents=True)
    pre_disc.write_bytes(b"R")

    games, _, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    copy_system(tree["system"], games, t_roms, t_esde, overwrite=False)

    assert pre_disc.read_bytes() == b"R"


def test_copy_system_m3u_overwrite_clobbers_disc(tree, tmp_path):
    """overwrite=True: same-size disc on target is replaced with source content."""
    t_roms, t_esde = _targets(tmp_path)
    roms_sys = tree["roms_dir"] / "snes"
    _write_gamelist(tree["gl_path"], [{"path": "./Game.m3u", "rating": "0.9"}])
    _write_m3u(roms_sys / "Game.m3u", ["Game (Disc 1).bin"])
    (roms_sys / "Game (Disc 1).bin").write_bytes(b"x")
    pre_disc = t_roms / "snes" / "Game (Disc 1).bin"
    pre_disc.parent.mkdir(parents=True)
    pre_disc.write_bytes(b"R")

    games, _, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    copy_system(tree["system"], games, t_roms, t_esde, overwrite=True)

    assert pre_disc.read_bytes() == b"x"


# ---------------------------------------------------------------------------
# Integration: normal ROMs + m3u multi-disc in the same run
# ---------------------------------------------------------------------------

@pytest.fixture
def mixed_tree(tmp_path):
    """Fully-populated fixture with both a normal game and a 2-disc m3u game.

    gamelist has three entries:
      - Normal Game.zip   (rating 0.9 — passes threshold)
      - Low Rated Game.zip (rating 0.3 — filtered out)
      - Multi Disc Game.m3u (rating 0.9 — passes; references two .bin discs)
    Media art exists for the passing normal game and the m3u game only.
    """
    roms_dir  = tmp_path / "roms"
    media_dir = tmp_path / "media"
    gl_path   = tmp_path / "gamelists" / "psx" / "gamelist.xml"
    roms_sys  = roms_dir / "psx"

    normal_rom   = _make_file(roms_sys / "Normal Game.zip")
    normal_cover = _make_file(media_dir / "psx" / "covers" / "Normal Game.png")
    _make_file(roms_sys / "Low Rated Game.zip")

    m3u = roms_sys / "Multi Disc Game.m3u"
    _write_m3u(m3u, ["Multi Disc Game (Disc 1).bin", "Multi Disc Game (Disc 2).bin"])
    disc1 = roms_sys / "Multi Disc Game (Disc 1).bin"
    disc2 = roms_sys / "Multi Disc Game (Disc 2).bin"
    disc1.write_bytes(b"D1" * 50)
    disc2.write_bytes(b"D2" * 50)
    m3u_cover = _make_file(media_dir / "psx" / "covers" / "Multi Disc Game.png")

    _write_gamelist(gl_path, [
        {"path": "./Normal Game.zip",     "rating": "0.9"},
        {"path": "./Low Rated Game.zip",  "rating": "0.3"},
        {"path": "./Multi Disc Game.m3u", "rating": "0.9"},
    ])

    return {
        "roms_dir":     roms_dir,
        "media_dir":    media_dir,
        "gl_path":      gl_path,
        "system":       "psx",
        "normal_rom":   normal_rom,
        "normal_cover": normal_cover,
        "m3u":          m3u,
        "disc1":        disc1,
        "disc2":        disc2,
        "m3u_cover":    m3u_cover,
    }


def test_integration_mixed_normal_and_m3u_full_pipeline(mixed_tree, tmp_path):
    """End-to-end: preview + copy with a gamelist that mixes normal ROMs and an
    m3u multi-disc game.  Verifies that:
    - Rating filter drops the low-rated game (skipped=1, not included)
    - Normal ROM and its cover art land at the target
    - m3u file and both disc images land at the target
    - Disc images do not appear as separate gamelist entries
    - Target gamelist has exactly two entries (normal ROM + m3u)
    - Low-rated game is absent from the target entirely
    """
    t_roms = tmp_path / "roms-out"
    t_esde = tmp_path / "esde-out"

    games, skipped, missing, _ = preview_system(mixed_tree["system"], mixed_tree["gl_path"], mixed_tree["roms_dir"], mixed_tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))

    assert len(games) == 2
    assert skipped == 1
    assert missing == 0

    rom_names = {g["rom_filename"].name for g in games}
    assert rom_names == {"Normal Game.zip", "Multi Disc Game.m3u"}

    copy_system(mixed_tree["system"], games, t_roms, t_esde)

    sys_dir  = t_roms / "psx"
    media_out = t_esde / "downloaded_media" / "psx" / "covers"

    assert (sys_dir / "Normal Game.zip").is_file()
    assert (media_out / "Normal Game.png").is_file()

    assert (sys_dir / "Multi Disc Game.m3u").is_file()
    assert (sys_dir / "Multi Disc Game (Disc 1).bin").read_bytes() == b"D1" * 50
    assert (sys_dir / "Multi Disc Game (Disc 2).bin").read_bytes() == b"D2" * 50
    assert (media_out / "Multi Disc Game.png").is_file()

    assert not (sys_dir / "Low Rated Game.zip").exists()

    gl_paths = _read_target_gamelist_paths(t_esde, "psx")
    assert sorted(gl_paths) == ["./Multi Disc Game.m3u", "./Normal Game.zip"]


def test_integration_m3u_discs_excluded_from_gamelist_entries(mixed_tree, tmp_path):
    """The disc .bin files referenced by an m3u must never appear as standalone
    <game> entries in the target gamelist — they are copied as data only."""
    t_roms = tmp_path / "roms-out"
    t_esde = tmp_path / "esde-out"

    games, _, _, _ = preview_system(mixed_tree["system"], mixed_tree["gl_path"], mixed_tree["roms_dir"], mixed_tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    copy_system(mixed_tree["system"], games, t_roms, t_esde)

    gl_paths = _read_target_gamelist_paths(t_esde, "psx")
    for p in gl_paths:
        assert not p.endswith(".bin"), f"disc image appeared as gamelist entry: {p}"


def test_integration_skip_existing_leaves_matching_discs_untouched(mixed_tree, tmp_path):
    """skip-existing (overwrite=False): disc images already on target at the
    correct size are not overwritten; absent discs are still copied."""
    t_roms = tmp_path / "roms-out"
    t_esde = tmp_path / "esde-out"

    pre_disc1 = t_roms / "psx" / "Multi Disc Game (Disc 1).bin"
    pre_disc1.parent.mkdir(parents=True)
    pre_disc1.write_bytes(b"D1" * 50)  # same size as source, different would-be content

    games, _, _, _ = preview_system(mixed_tree["system"], mixed_tree["gl_path"], mixed_tree["roms_dir"], mixed_tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False, overwrite=False, target_roms_dir=t_roms, target_esde_data_dir=t_esde))
    copy_system(mixed_tree["system"], games, t_roms, t_esde, overwrite=False)

    # Disc 1 was already present at matching size — must not be overwritten.
    assert pre_disc1.read_bytes() == b"D1" * 50
    # Disc 2 was absent — must be copied.
    assert (t_roms / "psx" / "Multi Disc Game (Disc 2).bin").read_bytes() == b"D2" * 50


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

    monkeypatch.setattr(_config, "load_config", lambda _path: {})
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
    def fake_preview(system, gl_path, roms_dir, media_dir, opts, **_kw):
        captured["min_rating"] = opts.min_rating
        return [], 0, 0, []
    main_env["monkeypatch"].setattr(_preview, "preview_system", fake_preview)
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
    main_env["monkeypatch"].setattr(_preview, "preview_system", fake_preview)
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
    main_env["monkeypatch"].setattr(_preview, "preview_system", fake_preview)


def test_main_errors_on_insufficient_disk_space_roms(main_env):
    """ROMs target is too full → exit before the prompt with a clear message."""
    _make_system_dir(main_env["esde"], "snes")
    _install_fake_preview(main_env, rom_bytes=10_000_000, media_bytes=1)

    def fake_free(path: Path) -> int:
        return 1 if path == main_env["target_roms"] else 10**12
    main_env["monkeypatch"].setattr(_copy_mod, "_free_space", fake_free)
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
    main_env["monkeypatch"].setattr(_copy_mod, "_free_space", fake_free)
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
    main_env["monkeypatch"].setattr(_copy_mod, "_free_space", lambda _p: 10**12)
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
    main_env["monkeypatch"].setattr(_preview, "preview_system", fake_preview)
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
    main_env["monkeypatch"].setattr(_copy_mod, "_free_space", lambda _p: 10**12)
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
    main_env["monkeypatch"].setattr(_preview, "preview_system", fake_preview)
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
    main_env["monkeypatch"].setattr(_copy_mod, "_free_space", lambda _p: 10**12)
    copy_called = []
    main_env["monkeypatch"].setattr(_copy_mod, "copy_system",
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
    main_env["monkeypatch"].setattr(_copy_mod, "_free_space", lambda _p: 10**12)
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
    main_env["monkeypatch"].setattr(_copy_mod, "_free_space", lambda _p: 10**12)
    main_env["monkeypatch"].setattr("sys.argv", _disk_check_argv(main_env) + ["--verbose"])
    rom_filter_copy.main()
    assert "NoName" in capsys.readouterr().out


def test_main_copy_loop_shows_progress_counter(main_env, capsys):
    """Copy output includes (idx/total) counter for each system."""
    _make_system_dir(main_env["esde"], "snes")
    _install_fake_preview(main_env, rom_bytes=1, media_bytes=1)
    main_env["monkeypatch"].setattr(_copy_mod, "_free_space", lambda _p: 10**12)
    main_env["monkeypatch"].setattr(_copy_mod, "copy_system", lambda *a, **kw: None)
    main_env["monkeypatch"].setattr("sys.argv", _disk_check_argv(main_env))
    rom_filter_copy.main()
    assert "(1/1 systems)" in capsys.readouterr().out


def test_main_list_systems_prints_and_exits(main_env, capsys):
    """--list-systems prints sorted system names and returns without copying."""
    for sysname in ("nes", "snes"):
        _make_system_dir(main_env["esde"], sysname)
    copy_called = []
    main_env["monkeypatch"].setattr(_copy_mod, "copy_system",
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
    def fake_preview(system, gl_path, roms_dir, media_dir, opts, **_kw):
        captured[system] = opts.copy_all
        return [], 0, 0, []
    main_env["monkeypatch"].setattr(_preview, "preview_system", fake_preview)
    main_env["monkeypatch"].setattr("sys.argv",
                                    _disk_check_argv(main_env) + ["--copy-all-systems", "nes"])
    rom_filter_copy.main()
    assert captured["nes"]  is True
    assert captured["snes"] is False


def test_main_copy_all_systems_bare_flag_clears_config(main_env):
    """--copy-all-systems with no args overrides config copy_all_systems to empty."""
    _make_system_dir(main_env["esde"], "nes")
    main_env["monkeypatch"].setattr(
        _config, "load_config", lambda _: {"copy_all_systems": ["nes"]})
    captured: dict[str, bool] = {}
    def fake_preview(system, gl_path, roms_dir, media_dir, opts, **_kw):
        captured[system] = opts.copy_all
        return [], 0, 0, []
    main_env["monkeypatch"].setattr(_preview, "preview_system", fake_preview)
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
# gui.save_config / load_config roundtrip (guards skip_systems data-loss bug)
# ---------------------------------------------------------------------------

def test_gui_save_config_roundtrip(tmp_path, monkeypatch):
    """save_config writes skip_systems, include_unrated, verbose, and
    system_ratings; load_config reads them back with identical values."""
    import gui

    cfg_path = tmp_path / "config.local.toml"
    monkeypatch.setattr(gui, "LOCAL_CONFIG", cfg_path)

    original = {
        "roms_dir": "/mnt/f/ROMs",
        "esde_data_dir": "/mnt/c/ES-DE",
        "target_roms_dir": "/mnt/g/ROMs",
        "target_esde_data_dir": "/mnt/g/ES-DE",
        "rating": 7.5,
        "overwrite": False,
        "prune": True,
        "include_unrated": True,
        "verbose": True,
        "systems_include_mode": True,
        "systems": ["snes", "psx"],
        "skip_systems": [],
        "copy_all_systems": ["snes"],
        "system_ratings": {"n3ds": 7.5, "psx": 6.0},
        "genre_ratings": {"Sports": 9.0, "Casino": 10.0},
        "skip_genres": ["Casino", "Maze"],
    }

    gui.save_config(original)
    loaded = gui.load_config()

    assert loaded["systems_include_mode"] is True
    assert loaded["systems"] == ["psx", "snes"]
    assert loaded["skip_systems"] == []
    assert loaded["include_unrated"] is True
    assert loaded["verbose"] is True
    assert loaded["prune"] is True
    assert loaded["system_ratings"] == {"n3ds": 7.5, "psx": 6.0}
    assert loaded["genre_ratings"] == {"Sports": 9.0, "Casino": 10.0}
    assert loaded["skip_genres"] == ["Casino", "Maze"]


# ---------------------------------------------------------------------------
# per-system rating overrides
# ---------------------------------------------------------------------------

def test_main_system_ratings_config_applies_per_system(main_env):
    """Config system_ratings overrides min_rating for matching systems only."""
    for sysname in ("n3ds", "snes"):
        _make_system_dir(main_env["esde"], sysname)
    main_env["monkeypatch"].setattr(
        _config, "load_config",
        lambda _: {"system_ratings": {"n3ds": 7.5}},
    )
    captured: dict[str, float] = {}
    def fake_preview(system, gl_path, roms_dir, media_dir, opts, **_kw):
        captured[system] = opts.min_rating
        return [], 0, 0, []
    main_env["monkeypatch"].setattr(_preview, "preview_system", fake_preview)
    main_env["monkeypatch"].setattr("sys.argv", _disk_check_argv(main_env) + ["--rating", "7.0"])
    rom_filter_copy.main()
    assert captured["n3ds"] == pytest.approx(0.75)
    assert captured["snes"] == pytest.approx(0.70)


def test_main_system_ratings_cli_overrides_config(main_env):
    """--system-ratings CLI flag overrides the config value for that system."""
    _make_system_dir(main_env["esde"], "n3ds")
    main_env["monkeypatch"].setattr(
        _config, "load_config",
        lambda _: {"system_ratings": {"n3ds": 7.0}},
    )
    captured: dict[str, float] = {}
    def fake_preview(system, gl_path, roms_dir, media_dir, opts, **_kw):
        captured[system] = opts.min_rating
        return [], 0, 0, []
    main_env["monkeypatch"].setattr(_preview, "preview_system", fake_preview)
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


def test_main_genres_flag_passes_include_set_to_preview(main_env):
    """--genres Action RPG → preview_system receives genres={'Action','RPG'}."""
    _make_system_dir(main_env["esde"], "snes")
    captured: dict = {}
    def fake_preview(system, gl_path, roms_dir, media_dir, opts, **_kw):
        captured["genres"] = opts.genres
        captured["skip_genres"] = opts.skip_genres
        captured["genre_ratings"] = opts.genre_ratings
        return [], 0, 0, []
    main_env["monkeypatch"].setattr(_preview, "preview_system", fake_preview)
    main_env["monkeypatch"].setattr("sys.argv",
        _disk_check_argv(main_env) + ["--genres", "Action", "RPG"])
    rom_filter_copy.main()
    assert captured.get("genres") == {"Action", "RPG"}
    assert captured.get("skip_genres") is None


def test_main_skip_genres_flag_passes_exclude_set_to_preview(main_env):
    """--skip-genres Casino → preview_system receives skip_genres={'Casino'}."""
    _make_system_dir(main_env["esde"], "snes")
    captured: dict = {}
    def fake_preview(system, gl_path, roms_dir, media_dir, opts, **_kw):
        captured["genres"] = opts.genres
        captured["skip_genres"] = opts.skip_genres
        captured["genre_ratings"] = opts.genre_ratings
        return [], 0, 0, []
    main_env["monkeypatch"].setattr(_preview, "preview_system", fake_preview)
    main_env["monkeypatch"].setattr("sys.argv",
        _disk_check_argv(main_env) + ["--skip-genres", "Casino"])
    rom_filter_copy.main()
    assert captured.get("skip_genres") == {"Casino"}
    assert captured.get("genres") is None


def test_main_genres_and_skip_genres_mutually_exclusive(main_env):
    """--genres and --skip-genres together → exit non-zero."""
    _make_system_dir(main_env["esde"], "snes")
    main_env["monkeypatch"].setattr("sys.argv",
        _disk_check_argv(main_env) + ["--genres", "Action", "--skip-genres", "Casino"])
    with pytest.raises(SystemExit):
        rom_filter_copy.main()


def test_main_no_genre_args_passes_none_to_preview(main_env):
    """No genre args → both genre params are None (no filtering)."""
    _make_system_dir(main_env["esde"], "snes")
    captured: dict = {}
    def fake_preview(system, gl_path, roms_dir, media_dir, opts, **_kw):
        captured["genres"] = opts.genres
        captured["skip_genres"] = opts.skip_genres
        captured["genre_ratings"] = opts.genre_ratings
        return [], 0, 0, []
    main_env["monkeypatch"].setattr(_preview, "preview_system", fake_preview)
    main_env["monkeypatch"].setattr("sys.argv", _disk_check_argv(main_env))
    rom_filter_copy.main()
    assert captured.get("genres") is None
    assert captured.get("skip_genres") is None


def test_main_genre_ratings_flag_parsed_correctly(main_env):
    """--genre-ratings Sports=9.0 → preview_system receives genre_ratings={'Sports': 0.9}."""
    _make_system_dir(main_env["esde"], "snes")
    captured: dict = {}
    def fake_preview(system, gl_path, roms_dir, media_dir, opts, **_kw):
        captured["genres"] = opts.genres
        captured["skip_genres"] = opts.skip_genres
        captured["genre_ratings"] = opts.genre_ratings
        return [], 0, 0, []
    main_env["monkeypatch"].setattr(_preview, "preview_system", fake_preview)
    main_env["monkeypatch"].setattr("sys.argv",
        _disk_check_argv(main_env) + ["--genre-ratings", "Sports=9.0", "Casino=10.0"])
    rom_filter_copy.main()
    assert captured.get("genre_ratings") == pytest.approx({"Sports": 0.9, "Casino": 1.0})


def test_main_genre_ratings_config_applies(main_env):
    """genre_ratings from config are normalized and passed to preview_system."""
    _make_system_dir(main_env["esde"], "snes")
    main_env["monkeypatch"].setattr(
        _config, "load_config",
        lambda _: {"genre_ratings": {"Sports": 9.0}},
    )
    captured: dict = {}
    def fake_preview(system, gl_path, roms_dir, media_dir, opts, **_kw):
        captured["genres"] = opts.genres
        captured["skip_genres"] = opts.skip_genres
        captured["genre_ratings"] = opts.genre_ratings
        return [], 0, 0, []
    main_env["monkeypatch"].setattr(_preview, "preview_system", fake_preview)
    main_env["monkeypatch"].setattr("sys.argv", _disk_check_argv(main_env))
    rom_filter_copy.main()
    assert captured.get("genre_ratings") == pytest.approx({"Sports": 0.9})


def test_main_genre_ratings_expanded_via_default_genre_map(main_env):
    """genre_ratings canonical keys are expanded through genre_map from DEFAULT_CONFIG.

    Regression: config.local.toml never includes [genre_map], so without falling
    back to DEFAULT_CONFIG the expansion never runs and sub-genres like
    'Sports / Basketball' are silently ignored by a 'Sports' override.
    """
    _make_system_dir(main_env["esde"], "snes")
    captured: dict = {}
    def fake_preview(system, gl_path, roms_dir, media_dir, opts, **_kw):
        captured["genres"] = opts.genres
        captured["skip_genres"] = opts.skip_genres
        captured["genre_ratings"] = opts.genre_ratings
        return [], 0, 0, []
    main_env["monkeypatch"].setattr(_preview, "preview_system", fake_preview)

    # Simulate the real-world split: DEFAULT_CONFIG has genre_map, local config has
    # only genre_ratings (no genre_map) — this is exactly what the GUI produces.
    def mock_load_config(path: Path) -> dict:
        if path == _config.DEFAULT_CONFIG:
            return {"genre_map": {"Sports": ["Sports / Basketball", "Sports / Football (Soccer)"]}}
        return {}
    main_env["monkeypatch"].setattr(_config, "load_config", mock_load_config)
    main_env["monkeypatch"].setattr("sys.argv",
        _disk_check_argv(main_env) + ["--genre-ratings", "Sports=9.5"])

    rom_filter_copy.main()
    assert captured.get("genre_ratings") == pytest.approx({
        "Sports / Basketball":        0.95,
        "Sports / Football (Soccer)": 0.95,
    })


def test_main_genre_ratings_invalid_format_exits(main_env):
    """--genre-ratings with a bad value (no '=') exits non-zero."""
    _make_system_dir(main_env["esde"], "snes")
    main_env["monkeypatch"].setattr(
        "sys.argv", _disk_check_argv(main_env) + ["--genre-ratings", "badvalue"],
    )
    with pytest.raises(SystemExit) as exc:
        rom_filter_copy.main()
    assert exc.value.code != 0


# ---------------------------------------------------------------------------
# skipped_details carries rom_filename for prune support
# ---------------------------------------------------------------------------

def test_skipped_details_includes_rom_filename_rating_filter(tree):
    """Games skipped by rating have rom_filename in their skipped_details entry."""
    _write_gamelist(tree["gl_path"], [{"path": "./Bad.zip", "rating": "0.3"}])
    _make_file(tree["roms_dir"] / "snes" / "Bad.zip")

    _, _, _, skipped_details = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    assert len(skipped_details) == 1
    assert skipped_details[0]["rom_filename"] == Path("Bad.zip")


def test_skipped_details_includes_rom_filename_genre_filter(tree):
    """Games skipped by genre filter also carry rom_filename."""
    _write_gamelist(tree["gl_path"], [
        {"path": "./Sports.zip", "rating": "0.9", "genre": "Sports"},
    ])
    _make_file(tree["roms_dir"] / "snes" / "Sports.zip")

    _, _, _, skipped_details = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False, skip_genres={"Sports"}))
    assert len(skipped_details) == 1
    assert skipped_details[0]["rom_filename"] == Path("Sports.zip")


# ---------------------------------------------------------------------------
# delete_pruned
# ---------------------------------------------------------------------------

def test_delete_pruned_removes_rom_from_target(tmp_path):
    """A ROM on the target that matches a pruned entry is deleted."""
    t_roms = tmp_path / "roms"
    t_esde = tmp_path / "esde"
    rom = t_roms / "snes" / "Bad.zip"
    rom.parent.mkdir(parents=True)
    rom.write_bytes(b"x" * 100)

    deleted, freed = delete_pruned("snes", [{"rom_filename": Path("Bad.zip")}], t_roms, t_esde)

    assert not rom.exists()
    assert deleted == 1
    assert freed == 100


def test_delete_pruned_noop_when_rom_not_on_target(tmp_path):
    """No error when the pruned ROM doesn't exist on target."""
    t_roms = tmp_path / "roms"
    t_esde = tmp_path / "esde"

    deleted, freed = delete_pruned("snes", [{"rom_filename": Path("Ghost.zip")}], t_roms, t_esde)

    assert deleted == 0
    assert freed == 0


def test_delete_pruned_also_removes_media_files(tmp_path):
    """Media files on target matching the pruned game's stem are also deleted."""
    t_roms = tmp_path / "roms"
    t_esde = tmp_path / "esde"
    (t_roms / "snes").mkdir(parents=True)
    (t_roms / "snes" / "Bad.zip").write_bytes(b"x")

    cover = t_esde / "downloaded_media" / "snes" / "covers" / "Bad.png"
    cover.parent.mkdir(parents=True)
    cover.write_bytes(b"c" * 50)
    shot = t_esde / "downloaded_media" / "snes" / "screenshots" / "Bad.jpg"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(b"s" * 30)

    deleted, freed = delete_pruned("snes", [{"rom_filename": Path("Bad.zip")}], t_roms, t_esde)

    assert not cover.exists()
    assert not shot.exists()
    assert deleted == 3       # ROM + cover + screenshot
    assert freed == 1 + 50 + 30


def test_delete_pruned_leaves_other_games_media_intact(tmp_path):
    """Only media matching the pruned game's stem is removed; other games' media survives."""
    t_roms = tmp_path / "roms"
    t_esde = tmp_path / "esde"
    (t_roms / "snes").mkdir(parents=True)
    (t_roms / "snes" / "Bad.zip").write_bytes(b"x")

    covers = t_esde / "downloaded_media" / "snes" / "covers"
    covers.mkdir(parents=True)
    bad_cover  = covers / "Bad.png"
    good_cover = covers / "Good.png"
    bad_cover.write_bytes(b"b")
    good_cover.write_bytes(b"g")

    delete_pruned("snes", [{"rom_filename": Path("Bad.zip")}], t_roms, t_esde)

    assert not bad_cover.exists()
    assert good_cover.exists()


def test_delete_pruned_m3u_removes_discs_and_playlist(tmp_path):
    """For an m3u entry on target, the .m3u and all disc files it references are deleted."""
    t_roms = tmp_path / "roms"
    t_esde = tmp_path / "esde"
    sys_dir = t_roms / "psx"
    sys_dir.mkdir(parents=True)

    disc1 = sys_dir / "Game (Disc 1).bin"
    disc2 = sys_dir / "Game (Disc 2).bin"
    disc1.write_bytes(b"A" * 100)
    disc2.write_bytes(b"B" * 200)
    m3u = sys_dir / "Game.m3u"
    m3u.write_text("Game (Disc 1).bin\nGame (Disc 2).bin\n", encoding="utf-8")
    m3u_size = m3u.stat().st_size

    deleted, freed = delete_pruned("psx", [{"rom_filename": Path("Game.m3u")}], t_roms, t_esde)

    assert not m3u.exists()
    assert not disc1.exists()
    assert not disc2.exists()
    assert deleted == 3
    assert freed == m3u_size + 300


def test_delete_pruned_ignores_entry_without_rom_filename(tmp_path):
    """Entries missing the rom_filename key are silently skipped."""
    t_roms = tmp_path / "roms"
    t_esde = tmp_path / "esde"

    deleted, freed = delete_pruned("snes", [{"name": "No Filename", "rating": 0.3}], t_roms, t_esde)

    assert deleted == 0
    assert freed == 0


def test_delete_pruned_media_only_when_rom_absent_from_target(tmp_path):
    """Media is deleted even when the ROM file itself isn't on target."""
    t_roms = tmp_path / "roms"
    t_esde = tmp_path / "esde"
    # ROM not placed on target, only media
    cover = t_esde / "downloaded_media" / "snes" / "covers" / "Old.png"
    cover.parent.mkdir(parents=True)
    cover.write_bytes(b"c" * 20)

    deleted, freed = delete_pruned("snes", [{"rom_filename": Path("Old.zip")}], t_roms, t_esde)

    assert not cover.exists()
    assert deleted == 1
    assert freed == 20


# ---------------------------------------------------------------------------
# main --prune integration
# ---------------------------------------------------------------------------

def test_main_prune_deletes_filtered_rom(tmp_path, monkeypatch):
    """--prune: a ROM already on target that no longer passes the filter is deleted."""
    esde   = tmp_path / "esde"
    roms   = tmp_path / "roms"
    t_roms = tmp_path / "out-roms"
    t_esde = tmp_path / "out-esde"

    (esde / "downloaded_media").mkdir(parents=True)
    snes_gl = esde / "gamelists" / "snes" / "gamelist.xml"
    snes_gl.parent.mkdir(parents=True)
    snes_gl.write_text(
        '<?xml version="1.0"?><gameList>'
        '<game><path>./Good.zip</path><rating>0.9</rating></game>'
        '<game><path>./Bad.zip</path><rating>0.3</rating></game>'
        '</gameList>',
        encoding="utf-8",
    )
    _make_file(roms / "snes" / "Good.zip")
    _make_file(roms / "snes" / "Bad.zip")
    _make_file(t_roms / "snes" / "Good.zip")
    _make_file(t_roms / "snes" / "Bad.zip")  # stale file that should be pruned

    monkeypatch.setattr(_config, "load_config", lambda _path: {})
    monkeypatch.setattr("builtins.input", lambda _: "y")
    monkeypatch.setattr("sys.argv", [
        "rom_filter_copy.py",
        "--target-roms-dir",      str(t_roms),
        "--target-esde-data-dir", str(t_esde),
        "--roms-dir",             str(roms),
        "--esde-data-dir",        str(esde),
        "--rating", "7.0",
        "--prune",
    ])

    rom_filter_copy.main()

    assert (t_roms / "snes" / "Good.zip").exists()
    assert not (t_roms / "snes" / "Bad.zip").exists()


def test_main_prune_dry_run_no_delete(tmp_path, monkeypatch):
    """--prune --dry-run shows preview but does not delete any files."""
    esde   = tmp_path / "esde"
    roms   = tmp_path / "roms"
    t_roms = tmp_path / "out-roms"
    t_esde = tmp_path / "out-esde"

    (esde / "downloaded_media").mkdir(parents=True)
    snes_gl = esde / "gamelists" / "snes" / "gamelist.xml"
    snes_gl.parent.mkdir(parents=True)
    snes_gl.write_text(
        '<?xml version="1.0"?><gameList>'
        '<game><path>./Good.zip</path><rating>0.9</rating></game>'
        '<game><path>./Bad.zip</path><rating>0.3</rating></game>'
        '</gameList>',
        encoding="utf-8",
    )
    _make_file(roms / "snes" / "Good.zip")
    _make_file(roms / "snes" / "Bad.zip")
    _make_file(t_roms / "snes" / "Bad.zip")  # would be pruned in real run

    monkeypatch.setattr(_config, "load_config", lambda _path: {})
    monkeypatch.setattr("sys.argv", [
        "rom_filter_copy.py",
        "--target-roms-dir",      str(t_roms),
        "--target-esde-data-dir", str(t_esde),
        "--roms-dir",             str(roms),
        "--esde-data-dir",        str(esde),
        "--rating", "7.0",
        "--prune", "--dry-run",
    ])

    rom_filter_copy.main()

    assert (t_roms / "snes" / "Bad.zip").exists()  # not deleted in dry-run


def test_main_prune_also_deletes_media(tmp_path, monkeypatch):
    """--prune removes media files associated with pruned ROMs."""
    esde   = tmp_path / "esde"
    roms   = tmp_path / "roms"
    t_roms = tmp_path / "out-roms"
    t_esde = tmp_path / "out-esde"

    (esde / "downloaded_media").mkdir(parents=True)
    snes_gl = esde / "gamelists" / "snes" / "gamelist.xml"
    snes_gl.parent.mkdir(parents=True)
    snes_gl.write_text(
        '<?xml version="1.0"?><gameList>'
        '<game><path>./Good.zip</path><rating>0.9</rating></game>'
        '<game><path>./Bad.zip</path><rating>0.3</rating></game>'
        '</gameList>',
        encoding="utf-8",
    )
    _make_file(roms / "snes" / "Good.zip")
    _make_file(roms / "snes" / "Bad.zip")
    _make_file(t_roms / "snes" / "Bad.zip")
    stale_cover = t_esde / "downloaded_media" / "snes" / "covers" / "Bad.png"
    stale_cover.parent.mkdir(parents=True)
    stale_cover.write_bytes(b"old cover")

    monkeypatch.setattr(_config, "load_config", lambda _path: {})
    monkeypatch.setattr("builtins.input", lambda _: "y")
    monkeypatch.setattr("sys.argv", [
        "rom_filter_copy.py",
        "--target-roms-dir",      str(t_roms),
        "--target-esde-data-dir", str(t_esde),
        "--roms-dir",             str(roms),
        "--esde-data-dir",        str(esde),
        "--rating", "7.0",
        "--prune",
    ])

    rom_filter_copy.main()

    assert not (t_roms / "snes" / "Bad.zip").exists()
    assert not stale_cover.exists()


# ---------------------------------------------------------------------------
# expand_raw_genres
# ---------------------------------------------------------------------------

_SAMPLE_MAP: dict[str, list[str]] = {
    "Sports": ["Sports", "Sports / Baseball", "Sports / Football (Soccer)"],
    "Racing": ["Racing, Driving", "Racing FPV"],
}


def test_expand_raw_genres_known_canonical():
    result = expand_raw_genres({"Sports"}, _SAMPLE_MAP)
    assert result == {"Sports", "Sports / Baseball", "Sports / Football (Soccer)"}


def test_expand_raw_genres_multiple_canonicals():
    result = expand_raw_genres({"Sports", "Racing"}, _SAMPLE_MAP)
    assert result == {
        "Sports", "Sports / Baseball", "Sports / Football (Soccer)",
        "Racing, Driving", "Racing FPV",
    }


def test_expand_raw_genres_unknown_passthrough():
    result = expand_raw_genres({"Unknown Genre"}, _SAMPLE_MAP)
    assert result == {"Unknown Genre"}


def test_expand_raw_genres_empty_map():
    result = expand_raw_genres({"Sports"}, {})
    assert result == {"Sports"}


def test_expand_raw_genres_empty_input():
    result = expand_raw_genres(set(), _SAMPLE_MAP)
    assert result == set()


# ---------------------------------------------------------------------------
# expand_raw_genre_ratings
# ---------------------------------------------------------------------------

def test_expand_raw_genre_ratings_known_canonical():
    result = expand_raw_genre_ratings({"Sports": 0.9}, _SAMPLE_MAP)
    assert result == {
        "Sports": 0.9,
        "Sports / Baseball": 0.9,
        "Sports / Football (Soccer)": 0.9,
    }


def test_expand_raw_genre_ratings_unknown_passthrough():
    result = expand_raw_genre_ratings({"Raw Genre String": 0.8}, _SAMPLE_MAP)
    assert result == {"Raw Genre String": 0.8}


def test_expand_raw_genre_ratings_max_wins_on_overlap():
    # Two canonicals sharing a raw string (edge case) — stricter rating wins.
    overlap_map = {
        "A": ["shared", "a_only"],
        "B": ["shared", "b_only"],
    }
    result = expand_raw_genre_ratings({"A": 0.7, "B": 0.9}, overlap_map)
    assert result["shared"] == 0.9
    assert result["a_only"] == 0.7
    assert result["b_only"] == 0.9


def test_expand_raw_genre_ratings_empty():
    assert expand_raw_genre_ratings({}, _SAMPLE_MAP) == {}


# ---------------------------------------------------------------------------
# _dir_size (coverage: recursive subdirectory branch, missing-dir branch)
# ---------------------------------------------------------------------------

def test_dir_size_sums_nested_subdirectories(tmp_path):
    from _copy import _dir_size
    d = tmp_path / "sys"
    (d / "sub").mkdir(parents=True)
    (d / "top.bin").write_bytes(b"A" * 5)
    (d / "sub" / "deep.bin").write_bytes(b"B" * 15)
    assert _dir_size(d) == 20


def test_dir_size_missing_dir_returns_zero(tmp_path):
    from _copy import _dir_size
    assert _dir_size(tmp_path / "nonexistent") == 0


# ---------------------------------------------------------------------------
# build_target_media_index
# ---------------------------------------------------------------------------

def test_build_target_media_index_missing_system_dir(tmp_path):
    from _media import build_target_media_index
    assert build_target_media_index("snes", tmp_path) == {}


def test_build_target_media_index_maps_files_to_sizes(tmp_path):
    from _media import build_target_media_index
    cover = tmp_path / "snes" / "covers" / "Good.png"
    cover.parent.mkdir(parents=True)
    cover.write_bytes(b"X" * 42)
    assert build_target_media_index("snes", tmp_path) == {cover: 42}


def test_build_target_media_index_skips_non_dir_type_entry(tmp_path):
    """A stray file at the system root (not a type dir) is ignored."""
    from _media import build_target_media_index
    stray = tmp_path / "snes" / "stray.txt"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"x")
    cover = tmp_path / "snes" / "covers" / "Good.png"
    cover.parent.mkdir(parents=True)
    cover.write_bytes(b"Y")
    assert build_target_media_index("snes", tmp_path) == {cover: 1}


def test_build_target_media_index_skips_non_file_inside_type_dir(tmp_path):
    """A subdirectory inside a type dir (e.g. covers/subdir/) is ignored."""
    from _media import build_target_media_index
    (tmp_path / "snes" / "covers" / "subdir").mkdir(parents=True)
    cover = tmp_path / "snes" / "covers" / "Good.png"
    cover.write_bytes(b"Z")
    assert build_target_media_index("snes", tmp_path) == {cover: 1}


# ---------------------------------------------------------------------------
# build_media_index — non-file entry inside type dir
# ---------------------------------------------------------------------------

def test_build_media_index_skips_non_file_inside_type_dir(tmp_path):
    """A subdirectory inside covers/ must not crash build_media_index."""
    (tmp_path / "snes" / "covers" / "subdir").mkdir(parents=True)
    cover = _make_file(tmp_path / "snes" / "covers" / "Game.png")
    assert build_media_index("snes", tmp_path) == {"Game": [(cover, 1)]}


# ---------------------------------------------------------------------------
# copy_system — error handling (copy failure prints warnings)
# ---------------------------------------------------------------------------

def test_copy_system_prints_warning_when_copy_fails(tree, tmp_path, monkeypatch, capsys):
    """When _copy2_retry raises OSError, copy_system prints a warnings block."""
    t_roms, t_esde = _targets(tmp_path)
    _write_gamelist(tree["gl_path"], [{"path": "./Good.zip", "rating": "0.9"}])
    _make_file(tree["roms_dir"] / "snes" / "Good.zip")

    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(_copy_mod, "_copy2_retry", fail)

    games, _, _, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    copy_system(tree["system"], games, t_roms, t_esde)

    assert "WARNING" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# delete_pruned — non-dir / non-file entries in media scan
# ---------------------------------------------------------------------------

def test_delete_pruned_skips_non_dir_type_entry_in_media_dir(tmp_path):
    """A stray file at the media-system root (not a type dir) is ignored."""
    t_roms = tmp_path / "roms"
    t_esde = tmp_path / "esde"
    rom = t_roms / "snes" / "Bad.zip"
    rom.parent.mkdir(parents=True)
    rom.write_bytes(b"x")
    stray = t_esde / "downloaded_media" / "snes" / "stray.txt"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"y")
    cover = t_esde / "downloaded_media" / "snes" / "covers" / "Bad.png"
    cover.parent.mkdir(parents=True)
    cover.write_bytes(b"c")

    deleted, _ = delete_pruned("snes", [{"rom_filename": Path("Bad.zip")}], t_roms, t_esde)

    assert not rom.exists()
    assert not cover.exists()
    assert stray.exists()  # stray file at system root untouched
    assert deleted == 2


def test_delete_pruned_skips_non_file_inside_type_dir(tmp_path):
    """A subdirectory inside a type dir (e.g. covers/subdir/) is skipped."""
    t_roms = tmp_path / "roms"
    t_esde = tmp_path / "esde"
    rom = t_roms / "snes" / "Bad.zip"
    rom.parent.mkdir(parents=True)
    rom.write_bytes(b"x")
    (t_esde / "downloaded_media" / "snes" / "covers" / "subdir").mkdir(parents=True)
    cover = t_esde / "downloaded_media" / "snes" / "covers" / "Bad.png"
    cover.write_bytes(b"c")

    deleted, _ = delete_pruned("snes", [{"rom_filename": Path("Bad.zip")}], t_roms, t_esde)

    assert not rom.exists()
    assert not cover.exists()
    assert deleted == 2


def test_delete_pruned_prints_warning_when_unlink_fails(tmp_path, monkeypatch, capsys):
    """When file deletion raises OSError, a WARNING line is printed."""
    t_roms = tmp_path / "roms"
    t_esde = tmp_path / "esde"
    rom = t_roms / "snes" / "Bad.zip"
    rom.parent.mkdir(parents=True)
    rom.write_bytes(b"x" * 10)

    def fail_unlink(self, missing_ok=False):
        raise OSError("permission denied")
    monkeypatch.setattr(Path, "unlink", fail_unlink)

    deleted, freed = delete_pruned("snes", [{"rom_filename": Path("Bad.zip")}], t_roms, t_esde)

    assert deleted == 0
    assert "WARNING" in capsys.readouterr().out


def test_delete_pruned_survives_scandir_error_on_type_dir(tmp_path, monkeypatch):
    """OSError scanning a media type dir is caught and doesn't abort the prune."""
    t_roms = tmp_path / "roms"
    t_esde = tmp_path / "esde"
    rom = t_roms / "snes" / "Bad.zip"
    rom.parent.mkdir(parents=True)
    rom.write_bytes(b"x")
    (t_esde / "downloaded_media" / "snes" / "covers").mkdir(parents=True)

    real_scandir = _copy_mod.os.scandir
    calls = [0]
    def patched_scandir(path):
        calls[0] += 1
        if calls[0] > 1:
            raise OSError("access denied")
        return real_scandir(path)
    monkeypatch.setattr(_copy_mod.os, "scandir", patched_scandir)

    deleted, _ = delete_pruned("snes", [{"rom_filename": Path("Bad.zip")}], t_roms, t_esde)
    assert not rom.exists()  # ROM was deleted despite media scan error


# ---------------------------------------------------------------------------
# preview_system — skipped-game size when ROM absent from disk
# ---------------------------------------------------------------------------

def test_preview_skipped_by_rating_rom_not_on_disk_has_zero_size(tree):
    """A below-threshold game whose ROM file is absent gets rom_size=0 in skipped_details."""
    _write_gamelist(tree["gl_path"], [{"path": "./Absent.zip", "rating": "0.3"}])
    # ROM intentionally not created on disk

    _, _, _, skipped_details = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    assert len(skipped_details) == 1
    assert skipped_details[0]["rom_size"] == 0


def test_preview_skipped_by_genre_rom_not_on_disk_has_zero_size(tree):
    """A genre-excluded game whose ROM file is absent gets rom_size=0 in skipped_details."""
    _write_gamelist(tree["gl_path"], [
        {"path": "./Casino.zip", "rating": "0.9", "genre": "Casino"},
    ])
    # ROM intentionally not on disk

    _, _, _, skipped_details = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False, skip_genres={"Casino"}))
    assert len(skipped_details) == 1
    assert skipped_details[0]["rom_size"] == 0


# ---------------------------------------------------------------------------
# preview_system — m3u disc path outside system dir
# ---------------------------------------------------------------------------

def test_preview_m3u_disc_outside_system_dir_is_skipped(tree):
    """A disc whose absolute path doesn't start with roms_dir/system raises ValueError
    in relative_to() — the disc is silently skipped and the game is still included."""
    roms_sys = tree["roms_dir"] / "snes"
    _write_gamelist(tree["gl_path"], [{"path": "./Game.m3u", "rating": "0.9"}])
    m3u = roms_sys / "Game.m3u"
    m3u.parent.mkdir(parents=True, exist_ok=True)
    # Absolute path — Path(parent) / "/abs/path" keeps the absolute path, so
    # relative_to(roms_dir/snes) raises ValueError (not a subpath of snes/).
    m3u.write_text("/nonexistent/outside/disc.bin\n", encoding="utf-8")

    games, _, missing, _ = preview_system(tree["system"], tree["gl_path"], tree["roms_dir"], tree["media_dir"], PreviewOptions(min_rating=0.7, include_unrated=False, copy_all=False))
    assert len(games) == 1
    assert missing == 0
    assert games[0]["m3u_discs"] == []


# ---------------------------------------------------------------------------
# main — additional coverage
# ---------------------------------------------------------------------------

def test_main_verbose_shows_skipped_game_details(main_env, capsys):
    """--verbose lists skipped games with their rating and size under each system."""
    snes_dir = main_env["esde"] / "gamelists" / "snes"
    snes_dir.mkdir(parents=True)
    (snes_dir / "gamelist.xml").write_text(
        '<?xml version="1.0"?><gameList>'
        '<game><path>./Good.zip</path><rating>0.9</rating><name>Good Game</name></game>'
        '<game><path>./Bad.zip</path><rating>0.3</rating><name>Bad Game</name></game>'
        '</gameList>',
        encoding="utf-8",
    )
    _make_file(main_env["roms"] / "snes" / "Good.zip")
    _make_file(main_env["roms"] / "snes" / "Bad.zip")
    main_env["monkeypatch"].setattr(_copy_mod, "_free_space", lambda _p: 10**12)
    main_env["monkeypatch"].setattr("sys.argv", _disk_check_argv(main_env) + ["--verbose"])
    rom_filter_copy.main()

    out = capsys.readouterr().out
    assert "Good Game" in out  # included game listed with +
    assert "Bad Game" in out   # skipped game listed with -
    assert "3.0" in out        # rating displayed (0.3 * 10)


def test_main_shows_missing_count_in_summary(main_env, capsys):
    """When ROM files are absent from disk, the summary prints the missing count."""
    snes_dir = main_env["esde"] / "gamelists" / "snes"
    snes_dir.mkdir(parents=True)
    (snes_dir / "gamelist.xml").write_text(
        '<?xml version="1.0"?><gameList>'
        '<game><path>./Present.zip</path><rating>0.9</rating></game>'
        '<game><path>./Missing.zip</path><rating>0.9</rating></game>'
        '</gameList>',
        encoding="utf-8",
    )
    _make_file(main_env["roms"] / "snes" / "Present.zip")
    # Missing.zip intentionally absent from disk
    main_env["monkeypatch"].setattr(_copy_mod, "_free_space", lambda _p: 10**12)
    main_env["monkeypatch"].setattr("sys.argv", _disk_check_argv(main_env))
    rom_filter_copy.main()

    assert "1 games skipped" in capsys.readouterr().out


def test_main_skip_systems_unknown_name_warns(main_env, capsys):
    """--skip-systems with an unknown name prints a warning but continues."""
    _make_system_dir(main_env["esde"], "snes")
    main_env["monkeypatch"].setattr("sys.argv", _disk_check_argv(main_env) + [
        "--skip-systems", "unknownsys",
    ])
    rom_filter_copy.main()
    assert "unknownsys" in capsys.readouterr().err


def test_main_genre_map_expands_genres_include(main_env):
    """When genre_map is present in config, --genres canonical names are expanded."""
    _make_system_dir(main_env["esde"], "snes")
    captured: dict = {}
    def fake_preview(system, gl_path, roms_dir, media_dir, opts, **_kw):
        captured["genres"] = opts.genres
        captured["skip_genres"] = opts.skip_genres
        captured["genre_ratings"] = opts.genre_ratings
        return [], 0, 0, []
    main_env["monkeypatch"].setattr(_preview, "preview_system", fake_preview)

    def mock_load_config(path: Path) -> dict:
        if path == _config.DEFAULT_CONFIG:
            return {"genre_map": {"Sports": ["Sports", "Sports / Basketball"]}}
        return {}
    main_env["monkeypatch"].setattr(_config, "load_config", mock_load_config)
    main_env["monkeypatch"].setattr("sys.argv",
        _disk_check_argv(main_env) + ["--genres", "Sports"])

    rom_filter_copy.main()
    assert captured.get("genres") == {"Sports", "Sports / Basketball"}


def test_main_genre_map_expands_skip_genres(main_env):
    """When genre_map is present, --skip-genres canonical names are expanded."""
    _make_system_dir(main_env["esde"], "snes")
    captured: dict = {}
    def fake_preview(system, gl_path, roms_dir, media_dir, opts, **_kw):
        captured["genres"] = opts.genres
        captured["skip_genres"] = opts.skip_genres
        captured["genre_ratings"] = opts.genre_ratings
        return [], 0, 0, []
    main_env["monkeypatch"].setattr(_preview, "preview_system", fake_preview)

    def mock_load_config(path: Path) -> dict:
        if path == _config.DEFAULT_CONFIG:
            return {"genre_map": {"Casino": ["Casino", "Casino / Cards"]}}
        return {}
    main_env["monkeypatch"].setattr(_config, "load_config", mock_load_config)
    main_env["monkeypatch"].setattr("sys.argv",
        _disk_check_argv(main_env) + ["--skip-genres", "Casino"])

    rom_filter_copy.main()
    assert captured.get("skip_genres") == {"Casino", "Casino / Cards"}


def test_main_errors_when_roms_dir_arg_missing(main_env):
    """No --roms-dir in config or CLI → parser.error before any filesystem access."""
    _make_system_dir(main_env["esde"], "snes")
    main_env["monkeypatch"].setattr("sys.argv", [
        "rom_filter_copy.py",
        "--target-roms-dir",      str(main_env["target_roms"]),
        "--target-esde-data-dir", str(main_env["target_esde"]),
        "--esde-data-dir",        str(main_env["esde"]),
        # --roms-dir intentionally omitted
    ])
    with pytest.raises(SystemExit) as exc:
        rom_filter_copy.main()
    assert exc.value.code != 0


def test_main_errors_when_esde_data_dir_arg_missing(main_env):
    """No --esde-data-dir in config or CLI → parser.error before any filesystem access."""
    _make_system_dir(main_env["esde"], "snes")
    main_env["monkeypatch"].setattr("sys.argv", [
        "rom_filter_copy.py",
        "--target-roms-dir",      str(main_env["target_roms"]),
        "--target-esde-data-dir", str(main_env["target_esde"]),
        "--roms-dir",             str(main_env["roms"]),
        # --esde-data-dir intentionally omitted
    ])
    with pytest.raises(SystemExit) as exc:
        rom_filter_copy.main()
    assert exc.value.code != 0


def test_main_errors_when_esde_data_dir_does_not_exist(main_env):
    """--esde-data-dir path doesn't exist → sys.exit with a clear error message."""
    main_env["monkeypatch"].setattr("sys.argv", [
        "rom_filter_copy.py",
        "--target-roms-dir",      str(main_env["target_roms"]),
        "--target-esde-data-dir", str(main_env["target_esde"]),
        "--roms-dir",             str(main_env["roms"]),
        "--esde-data-dir",        "/nonexistent/esde",
    ])
    with pytest.raises(SystemExit) as exc:
        rom_filter_copy.main()
    assert "--esde-data-dir" in str(exc.value)


def test_main_keyboard_interrupt_exits_130(main_env):
    """KeyboardInterrupt during copy exits with code 130 and prints Cancelled."""
    _make_system_dir(main_env["esde"], "snes")
    _install_fake_preview(main_env, rom_bytes=1, media_bytes=1)
    main_env["monkeypatch"].setattr(_copy_mod, "_free_space", lambda _p: 10**12)

    def raise_interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    main_env["monkeypatch"].setattr(_copy_mod, "copy_system", raise_interrupt)
    main_env["monkeypatch"].setattr("sys.argv", _disk_check_argv(main_env))

    with pytest.raises(SystemExit) as exc:
        rom_filter_copy.main()
    assert exc.value.code == 130

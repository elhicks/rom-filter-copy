"""End-to-end integration test: invokes rom_filter_copy.py as a subprocess
against checked-in fixture data and asserts on the resulting tree at the
target. This is the closest test we have to a real first run — it exercises
argparse, config loading, stdin prompting, real XML parsing, real file
copies, and the full main() orchestration."""

import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

PROJECT  = Path(__file__).parent
SCRIPT   = PROJECT / "rom_filter_copy.py"
FIXTURES = PROJECT / "fixtures" / "sample-esde"


def _gamelist_paths(target_esde: Path, system: str) -> list[str]:
    gl = target_esde / "gamelists" / system / "gamelist.xml"
    out: list[str] = []
    for g in ET.parse(gl).getroot().findall("game"):
        el = g.find("path")
        assert el is not None and el.text is not None
        out.append(el.text)
    return out


def _targets(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "out-roms", tmp_path / "out-esde"


def test_integration_end_to_end(tmp_path):
    t_roms, t_esde = _targets(tmp_path)

    result = subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--config",               str(FIXTURES / "sample.toml"),
            "--roms-dir",             str(FIXTURES / "roms"),
            "--esde-data-dir",        str(FIXTURES / "esde"),
            "--target-roms-dir",      str(t_roms),
            "--target-esde-data-dir", str(t_esde),
        ],
        input="y\n",
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )

    # ---- ROMs ----------------------------------------------------------

    # snes: rating filter applies. GoodGame and bracket-stem FF4 pass (>=0.7).
    assert (t_roms / "snes" / "GoodGame.zip").is_file()
    assert (t_roms / "snes" / "Final Fantasy IV (USA) [!].sfc").is_file()
    # snes: LowRated (0.5) and Unrated are filtered out; Ghost.zip was
    # referenced but absent on disk — must not appear at target.
    assert not (t_roms / "snes" / "LowRated.zip").exists()
    assert not (t_roms / "snes" / "Unrated.zip").exists()
    assert not (t_roms / "snes" / "Ghost.zip").exists()

    # nes is in copy_all_systems → rating filter bypassed, both ROMs land.
    assert (t_roms / "nes" / "ClassicA.nes").is_file()
    assert (t_roms / "nes" / "ClassicB.nes").is_file()

    # ---- Media ---------------------------------------------------------

    media = t_esde / "downloaded_media"
    assert (media / "snes" / "covers"      / "GoodGame.png").is_file()
    assert (media / "snes" / "screenshots" / "GoodGame.jpg").is_file()
    # Bracket-stem regression: the cover for FF4 must transfer with its
    # literal [!] in the filename intact.
    assert (media / "snes" / "covers" / "Final Fantasy IV (USA) [!].png").is_file()
    # Filtered-out game's media must NOT transfer.
    assert not (media / "snes" / "covers" / "LowRated.png").exists()
    # Copy-all media transfers.
    assert (media / "nes" / "covers" / "ClassicA.png").is_file()
    assert (media / "nes" / "covers" / "ClassicB.png").is_file()

    # ---- Gamelist contents at target -----------------------------------

    assert sorted(_gamelist_paths(t_esde, "snes")) == sorted([
        "./GoodGame.zip",
        "./Final Fantasy IV (USA) [!].sfc",
    ])
    assert sorted(_gamelist_paths(t_esde, "nes")) == sorted([
        "./ClassicA.nes",
        "./ClassicB.nes",
    ])

    # ---- Preview output ------------------------------------------------

    out = result.stdout
    # Per-system summary lines.
    assert "[snes]" in out
    assert "[nes]" in out and "(copy all)" in out
    # Missing-ROM accounting surfaces in the totals.
    assert "1 games skipped (ROM file not found on disk)" in out
    # Final tally: 2 snes + 2 nes = 4.
    assert "Total: 4 games" in out


def test_integration_declining_prompt_writes_nothing(tmp_path):
    """Saying 'n' at the confirm prompt must not touch the target."""
    t_roms, t_esde = _targets(tmp_path)

    result = subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--config",               str(FIXTURES / "sample.toml"),
            "--roms-dir",             str(FIXTURES / "roms"),
            "--esde-data-dir",        str(FIXTURES / "esde"),
            "--target-roms-dir",      str(t_roms),
            "--target-esde-data-dir", str(t_esde),
        ],
        input="n\n",
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "Aborted." in result.stdout
    assert not t_roms.exists()
    assert not t_esde.exists()


# ---------------------------------------------------------------------------
# Helpers for the additional scenarios below
# ---------------------------------------------------------------------------

def _base_cmd(t_roms: Path, t_esde: Path, **overrides) -> list[str]:
    """Standard fixture-driven invocation, with optional CLI overrides."""
    cmd = [
        sys.executable, str(SCRIPT),
        "--config",               str(FIXTURES / "sample.toml"),
        "--roms-dir",             str(FIXTURES / "roms"),
        "--esde-data-dir",        str(FIXTURES / "esde"),
        "--target-roms-dir",      str(t_roms),
        "--target-esde-data-dir", str(t_esde),
    ]
    for k, v in overrides.items():
        cmd.append(f"--{k.replace('_', '-')}")
        if isinstance(v, list):
            cmd.extend(v)
        elif v is not None:
            cmd.append(str(v))
    return cmd


def _run(cmd: list[str], stdin: str = "y\n"):
    return subprocess.run(
        cmd, input=stdin, text=True, capture_output=True, timeout=30,
    )


# ---------------------------------------------------------------------------
# Idempotency / re-run safety
# ---------------------------------------------------------------------------

def test_integration_rerun_is_idempotent(tmp_path):
    """A second run over an already-populated target must succeed and produce
    the same tree (no crash on overwrites, no duplicated gamelist entries)."""
    t_roms, t_esde = _targets(tmp_path)
    cmd = _base_cmd(t_roms, t_esde)

    def snapshot() -> list[str]:
        out: list[str] = []
        for root in (t_roms, t_esde):
            if root.exists():
                out.extend(
                    f"{root.name}/{p.relative_to(root).as_posix()}"
                    for p in root.rglob("*") if p.is_file()
                )
        return sorted(out)

    r1 = _run(cmd)
    assert r1.returncode == 0, r1.stderr
    first = snapshot()

    r2 = _run(cmd)
    assert r2.returncode == 0, r2.stderr
    second = snapshot()

    assert first == second
    # Gamelist not appended-to.
    assert sorted(_gamelist_paths(t_esde, "snes")) == sorted([
        "./GoodGame.zip",
        "./Final Fantasy IV (USA) [!].sfc",
    ])
    assert sorted(_gamelist_paths(t_esde, "nes")) == sorted([
        "./ClassicA.nes",
        "./ClassicB.nes",
    ])


# ---------------------------------------------------------------------------
# CLI flag plumbing at the subprocess layer
# ---------------------------------------------------------------------------

def test_integration_systems_flag_restricts_output_tree(tmp_path):
    """--systems snes must produce only snes/ on disk — no nes/ anywhere."""
    t_roms, t_esde = _targets(tmp_path)
    result = _run(_base_cmd(t_roms, t_esde, systems=["snes"]))
    assert result.returncode == 0, result.stderr

    assert (t_roms / "snes").is_dir()
    assert not (t_roms / "nes").exists()
    assert (t_esde / "gamelists" / "snes").is_dir()
    assert not (t_esde / "gamelists" / "nes").exists()
    assert (t_esde / "downloaded_media" / "snes").is_dir()
    assert not (t_esde / "downloaded_media" / "nes").exists()


def test_integration_include_unrated_flag_pulls_in_unrated_games(tmp_path):
    """--include-unrated lets the Unrated.zip entry through, but LowRated
    (0.5, below the 7.0/10 threshold) is still excluded."""
    t_roms, t_esde = _targets(tmp_path)
    result = _run(_base_cmd(t_roms, t_esde, include_unrated=None))  # bare flag
    assert result.returncode == 0, result.stderr

    assert (t_roms / "snes" / "Unrated.zip").is_file()
    assert (t_roms / "snes" / "GoodGame.zip").is_file()
    assert not (t_roms / "snes" / "LowRated.zip").exists()


def test_integration_rating_threshold_lowered(tmp_path):
    """--rating 5.0 → 0.5 internally. LowRated (rating 0.5) is now included
    via the inclusive boundary; Unrated is still excluded (no rating)."""
    t_roms, t_esde = _targets(tmp_path)
    result = _run(_base_cmd(t_roms, t_esde, rating="5.0"))
    assert result.returncode == 0, result.stderr

    assert (t_roms / "snes" / "LowRated.zip").is_file()
    assert (t_roms / "snes" / "GoodGame.zip").is_file()
    assert not (t_roms / "snes" / "Unrated.zip").exists()


# ---------------------------------------------------------------------------
# Config-only invocation (no overriding CLI args)
# ---------------------------------------------------------------------------

def test_integration_config_only_invocation(tmp_path):
    """All paths/values supplied via TOML; --config is the only CLI arg.
    Exercises the two-phase argparse + config-as-defaults wiring end-to-end."""
    t_roms, t_esde = _targets(tmp_path)
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        f'''roms_dir = "{(FIXTURES / "roms").as_posix()}"
esde_data_dir = "{(FIXTURES / "esde").as_posix()}"
target_roms_dir = "{t_roms.as_posix()}"
target_esde_data_dir = "{t_esde.as_posix()}"
rating = 7.0
copy_all_systems = ["nes"]
''',
        encoding="utf-8",
    )

    result = _run([sys.executable, str(SCRIPT), "--config", str(cfg)])
    assert result.returncode == 0, result.stderr

    assert (t_roms / "snes" / "GoodGame.zip").is_file()
    assert (t_roms / "nes" / "ClassicA.nes").is_file()


# ---------------------------------------------------------------------------
# Error / edge paths
# ---------------------------------------------------------------------------

def test_integration_missing_gamelists_dir_errors(tmp_path):
    """Pointing --esde-data-dir at a directory without a gamelists/ subdir
    must exit non-zero with a clear stderr message and not touch the target."""
    t_roms, t_esde = _targets(tmp_path)
    empty_esde = tmp_path / "empty-esde"
    empty_esde.mkdir()

    result = _run(_base_cmd(t_roms, t_esde, esde_data_dir=str(empty_esde)), stdin="")
    assert result.returncode != 0
    assert "ERROR" in result.stderr
    assert "gamelists/" in result.stderr
    assert not t_roms.exists()
    assert not t_esde.exists()


def test_integration_eof_at_prompt_aborts_cleanly(tmp_path):
    """Ctrl-D / closed stdin at the confirm prompt must not crash with an
    EOFError traceback. Regression for the try/except in main()."""
    t_roms, t_esde = _targets(tmp_path)
    result = _run(_base_cmd(t_roms, t_esde), stdin="")
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "Traceback" not in result.stderr
    assert "Aborted." in result.stdout
    assert not t_roms.exists()
    assert not t_esde.exists()


def test_integration_malformed_gamelist_skips_system_and_continues(tmp_path):
    """A broken gamelist.xml in one system must NOT take down the rest of the
    run. The good system still processes; the broken one gets a stderr warning."""
    t_roms, t_esde = _targets(tmp_path)
    esde = tmp_path / "esde"

    # Broken system.
    (esde / "gamelists" / "broken").mkdir(parents=True)
    (esde / "gamelists" / "broken" / "gamelist.xml").write_text("<gameList><game><path>")
    # A healthy system alongside it (mirror the snes fixture in miniature).
    (esde / "gamelists" / "snes").mkdir()
    (esde / "gamelists" / "snes" / "gamelist.xml").write_text(
        '<?xml version="1.0"?><gameList>'
        '<game><path>./GoodGame.zip</path><rating>0.9</rating></game>'
        '</gameList>'
    )
    (esde / "downloaded_media").mkdir()

    result = _run(_base_cmd(t_roms, t_esde, esde_data_dir=str(esde)), stdin="y\n")
    assert result.returncode == 0, result.stderr
    # Broken system surfaced as a warning, not a crash.
    assert "WARNING" in result.stderr
    assert "broken" in result.stderr
    assert "Traceback" not in result.stderr
    # Good system still copied.
    assert (t_roms / "snes" / "GoodGame.zip").is_file()
    assert sorted(_gamelist_paths(t_esde, "snes")) == ["./GoodGame.zip"]


# ---------------------------------------------------------------------------
# Clearer error messages for the most common misconfigurations
# ---------------------------------------------------------------------------

def test_integration_missing_config_file_errors(tmp_path):
    """Explicitly passing --config to a nonexistent file must error out, not
    silently fall back to an empty config."""
    t_roms, t_esde = _targets(tmp_path)
    result = _run(
        [
            sys.executable, str(SCRIPT),
            "--config",               str(tmp_path / "does-not-exist.toml"),
            "--roms-dir",             str(FIXTURES / "roms"),
            "--esde-data-dir",        str(FIXTURES / "esde"),
            "--target-roms-dir",      str(t_roms),
            "--target-esde-data-dir", str(t_esde),
        ],
        stdin="",
    )
    assert result.returncode != 0
    assert "ERROR" in result.stderr
    assert "--config" in result.stderr
    assert not t_roms.exists()
    assert not t_esde.exists()


def test_integration_missing_roms_dir_errors_clearly(tmp_path):
    """If --roms-dir doesn't exist, the user should see a clear path error —
    NOT a misleading 'N games skipped (ROM file not found on disk)' summary
    that suggests individual ROMs are gone."""
    t_roms, t_esde = _targets(tmp_path)
    result = _run(_base_cmd(t_roms, t_esde, roms_dir=str(tmp_path / "no-such-roms")), stdin="")
    assert result.returncode != 0
    assert "ERROR" in result.stderr
    assert "--roms-dir" in result.stderr
    # The misleading old failure mode must not surface.
    assert "ROM file not found on disk" not in result.stdout
    assert not t_roms.exists()
    assert not t_esde.exists()


def test_integration_unknown_system_in_filter_warns_but_continues(tmp_path):
    """--systems with a mix of known and unknown names: warn about the
    unknown one but still process the known one."""
    t_roms, t_esde = _targets(tmp_path)
    result = _run(_base_cmd(t_roms, t_esde, systems=["snes", "bogus-system"]))
    assert result.returncode == 0, result.stderr
    assert "WARNING" in result.stderr
    assert "bogus-system" in result.stderr
    # The known system still got copied.
    assert (t_roms / "snes" / "GoodGame.zip").is_file()


def test_integration_only_unknown_systems_in_filter_errors(tmp_path):
    """If every name in --systems is unknown, exit with a clear error
    instead of silently doing nothing and printing 'Total: 0 games'."""
    t_roms, t_esde = _targets(tmp_path)
    result = _run(_base_cmd(t_roms, t_esde, systems=["bogus-a", "bogus-b"]), stdin="")
    assert result.returncode != 0
    assert "ERROR" in result.stderr
    assert "matched no available systems" in result.stderr
    assert not t_roms.exists()
    assert not t_esde.exists()

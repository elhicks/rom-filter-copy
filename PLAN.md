# Next steps for rom-filter-copy

A plan to hand to a fresh Claude Code conversation. In the new session, start with:

> Read PLAN.md and work through the tasks in priority order. Confirm with me after each task before moving on. For task 1, I'll run the script myself — you interpret the output.

---

## Project context (read first)

Personal ROM filter/copy tool. Reads ES-DE `gamelist.xml` files, filters games by rating, copies qualifying ROMs + scraped media to a target drive (SD card for an Ayn Thor handheld) in ES-DE-compatible layout.

**Setup:**
- Project root: `~/rom-filter-copy/`
- ROMs: `/mnt/f/ROMs` (Windows F:\ROMs via WSL)
- ES-DE data: `/mnt/c/Users/ELH/ES-DE`
- Target SD card: `/mnt/g`
- Python 3.14, pytest 9.0.2 (apt: `python3-pytest`)

**Layout:**
- `rom_filter_copy.py` — main script. Pure helpers (`parse_rating`, `parse_rom_path`, `should_include`), filesystem-touching (`build_media_index`, `preview_system`, `copy_system`), CLI (`main`).
- `config.toml` — production config (real paths). Ships with `target_roms_dir = ""` and `target_esde_data_dir = ""` so users must explicitly set them.
- `config.filter-test.toml` — validation-only config: empty `copy_all_systems` so every system goes through the rating filter. Used for exercising `should_include` against real metadata.
- `test_rom_filter_copy.py` — unit tests
- `test_integration.py` — integration tests (subprocess against fixtures)
- `fixtures/sample-esde/` — checked-in fixture data (snes + nes, mixed dispositions)
- `./validate` — lints, type-checks, and runs tests; `./validate unit` / `./validate integration` / `./validate -k pattern` for subsets

**Current state:** 105 tests passing, 94% coverage. Beyond the original baseline:
- Config file renamed `rom_filter_copy.toml` → `config.toml`.
- Single `target` split into two **independent** keys: `target_roms_dir` and `target_esde_data_dir`. ROMs go directly to `{target_roms_dir}/{system}/...` (no inserted `/ROMs/`). ES-DE data goes to `{target_esde_data_dir}/{gamelists,downloaded_media}/...`. The two targets may live on different filesystems. CLI flags: `--target-roms-dir` and `--target-esde-data-dir`. **Both required.**
- `collect_media_files` replaced with `build_media_index(system, media_dir) -> dict[str, list[tuple[Path, int]]]` — one `os.scandir` pass per system, sizes captured during the walk so callers don't restat. ~3.75× faster than the previous per-game walk on WSL→NTFS.
- `preview_system` combines `src_rom.exists()` + `.stat()` into a single try-stat (saves 1 stat per game).
- Real-data verification of Task 1 completed (see Task 1 status below).
- **Task 2 done.** Per-target free-space pre-check (`_free_space`) runs after the per-system summary, before the confirm prompt. Walks up to the nearest existing ancestor so first-runs (target dir not yet created) still get a real reading.
- **Skip-existing is now the default copy mode.** `preview_system` accepts `target_roms_dir` / `target_esde_data_dir` / `overwrite` kw-only args and builds a `build_target_media_index` (full-path-keyed scandir of the target's `downloaded_media/{system}/`) to determine which files are already present with matching size. Each game dict gains `copy_rom_bytes` and `copy_media_bytes` — these (not full sizes) drive the disk-space check. `copy_system` re-checks at copy time via `_size_matches(dst, expected_size)` and skips when match. Gamelist.xml is **always** rewritten so metadata changes propagate. `--overwrite` flag (config key `overwrite`, default false) forces a full re-copy when needed. Per-system summary appends `to copy: X` when skip saved bytes; Total: line shows full size + to-copy + saved bytes; startup banner shows `Mode: skip-existing` or `Mode: overwrite`. README + config.toml document the new flag.

Before starting work: run `./validate` and confirm 105 pass. Run `./validate check` to confirm ruff + mypy clean. Coverage available via `./validate --cov=rom_filter_copy --cov-report=term-missing`.

---

## Tasks

### 1. Dry run against real data ✅ DONE

Verified end-to-end against real `/mnt/c` (ES-DE data) and `/mnt/f` (ROMs):

- **Real gamelist structure** (read directly from `/mnt/c/Users/ELH/ES-DE/gamelists/nes/gamelist.xml`): 736 game entries, **no `<folder>` elements**, **no XML namespaces**. Fields used: `path`, `name`, `desc`, `rating`, `releasedate`, `developer`, `publisher`, `genre`, `players`. Path format matches the assumed `./filename` shape. Ratings are floats in 0.0–1.0 (e.g. `0.4`, `0.6`, `0.7`, integer `1`). No fixture variants needed — existing fixtures cover the real shape.
- **Preview against real data** runs in ~35 s for NES (7,288 media files indexed, 2.84 GB total media — that's just one system). Was unusable before the perf rewrite (didn't finish 18 min on one system).
- **End-to-end copy** verified by running `--config config.filter-test.toml --target-roms-dir /tmp/dry-run/ROMs --target-esde-data-dir /tmp/dry-run/ES-DE --systems nes --rating 9.0`. Output: 57 ROMs (~7.6 MB) + 566 media files (~242 MB) + 1 written gamelist. All scraped fields preserved verbatim in the written XML (multi-line `<desc>` with embedded newlines, special chars, etc.). Bracket-stem filenames like `Pac-Man - Championship Edition (USA, Europe) (Namco Museum Archives Vol 1).zip` made it through the indexing intact.

**Caveat — not validated yet:** the script's output layout (`{target_roms_dir}/{system}/...` and `{target_esde_data_dir}/{gamelists,downloaded_media}/...`) matches the ES-DE convention but **hasn't been booted on the Ayn Thor**. To verify the device actually finds the data, drop a small filtered tree onto the SD card and boot — if metadata + artwork show up in the Thor's ES-DE, the layout is right. If not, check the Thor's `es_settings.xml` for the paths it expects and adjust the `target_*_dir` configs accordingly. Two independent targets means no script change needed for any plausible Thor layout — just config values.

---

### 2. Disk-space pre-check ✅ DONE

`_free_space(path)` walks up to the nearest existing ancestor before calling `shutil.disk_usage` so first-runs (target dir not yet created) still get a real free-space reading. Check runs after the `Total:` print, before the `input()` prompt, once per target with clear per-target error messages.

Bytes accounting was split — per game, `copy_rom_bytes` goes against `target_roms_dir` and `copy_media_bytes` goes against `target_esde_data_dir` (see skip-existing notes above for why these are "copy" rather than "full" bytes). Tests: `test_main_errors_on_insufficient_disk_space_roms/_esde`, `test_main_passes_disk_check_when_enough_space`.

---

### 3. Static analysis (ruff + mypy) ✅ DONE

Installed via `pip install --user --break-system-packages ruff mypy` (apt has no `ruff` candidate on this distro). The `./validate` script prepends `~/.local/bin` to PATH so the binaries resolve.

`./validate check` runs `ruff check . && mypy <typed files>`; `./validate all` chains that with the full pytest run. Ruff is clean. Mypy errors fixed:

- `format_size` introduced a local `size: float` to avoid `int /= 1024` annotation mismatch.
- `preview_system` skip-check restructured from `skip_check = (... and not None ...)` to an inline `if not overwrite and target_roms_dir is not None and target_esde_data_dir is not None:` block so mypy narrows `Path | None → Path` inside.
- Test helpers `_gamelist_paths` / `_read_target_gamelist_paths` replaced their `g.find("path").text` comprehension with an explicit loop + `assert el is not None and el.text is not None` (test-only — a malformed fixture should fail loudly).

**Result:** `./validate check` clean, `./validate all` runs lint + types + 98 tests.

---

### 4. Coverage measurement ✅ DONE

Installed via `pip install --user --break-system-packages pytest-cov`. Run with `./validate --cov=rom_filter_copy --cov-report=term-missing`.

Started at 87% (39 lines uncovered). Audited each gap and added 7 tests for the real-behavior branches:

- `test_main_skips_system_dir_with_no_gamelist_xml` — PLAN-flagged gap (line 356)
- `test_main_warns_and_continues_on_malformed_gamelist` — ET.ParseError path; one bad system doesn't kill the run
- `test_main_eof_at_confirm_prompt_aborts_cleanly` — Ctrl-D / closed stdin treated as decline
- `test_main_errors_when_systems_filter_matches_nothing` — `--systems` with all-unknown names
- `test_main_errors_when_roms_dir_does_not_exist` — fail-fast path validation
- `test_main_errors_when_gamelists_subdir_missing` — un-scraped library detection
- `test_main_happy_path_runs_copy_and_skips_empty_systems` — first end-to-end main() test with `input="y"`, exercising the copy phase AND the `if not games: continue` skip via a second system whose only game gets filtered out

Now at 94% (18 lines uncovered). The remaining gaps are all defensive guards (scandir non-file/non-dir, `_free_space` root bailout), argparse error wrappers for arg-required cases (parallel coverage exists via the target-dir tests), display-only print branches (skip-existing savings line, missing-games count), and the `__main__` block. Per the PLAN philosophy, stopping here.

---

### 5. Document known quirks in README

Append a `## Behavior notes` section to `README.md`. These behaviors are all tested but not documented — real users will hit them and search the README first.

Cover:
- `<path>` values that are absolute or contain `..` are silently dropped (security guard against scraper bugs)
- Malformed `gamelist.xml` causes that one system to be skipped with a stderr warning; other systems continue
- Games with no `<rating>` are excluded by default — opt in with `--include-unrated`
- Systems listed in `copy_all_systems` bypass the rating filter entirely (intended for small/retro libraries)
- Re-running over a populated target is idempotent: files are overwritten in place, gamelists are rewritten not appended
- Ctrl-D / EOF at the confirm prompt is treated as "no" and aborts cleanly
- Unknown names in `--systems` warn on stderr but the run continues with the known names; if ALL names are unknown, errors out

**Success:** README has a "Behavior notes" section covering all of the above; section is scannable, one paragraph or bullet per behavior.

---

### 6. Mutation testing ✅ DONE

Ran `mutmut` 3.5.0 (v3 CLI — uses `setup.cfg [mutmut]` config, not v2 `--paths-to-mutate` flag). Scoped to `rom_filter_copy.py` with `pytest_add_cli_args = test_rom_filter_copy.py` (unit tests only; integration tests spawn subprocesses and can't host mutmut's trampolines). `setup.cfg` also adds `[tool:pytest] norecursedirs = mutants` and `[ruff] exclude = mutants` so validate stays clean with the mutants dir present.

**Survivors classified and resolved:**

Accepted as equivalent (no tests added):
- `parse_rom_path`: `removeprefix("./")` → `removesuffix("./")` — `Path("./foo")` == `Path("foo")`, normalizes away
- `preview_system`: `skip_check = None` vs `False` — both falsy
- `copy_system`: XML formatting/encoding mutations (indent space, encoding case, xml_declaration bool) — tests parse content, not formatting
- `_free_space`: `sys.exit(None)` vs error string — process exits either way
- `build_media_index/build_target_media_index` continue→break in scandir loops — ordering-dependent, existing test covers intent; reliable assertion requires controlling OS scandir order
- `load_config` (7 "no tests") + `main` (~150 survivors) — structural; `main()` and `load_config()` are integration-test surfaces not exercised by unit tests

8 new tests added for real behavioral gaps (113 total, up from 105):
- `test_preview_counts_all_skipped_games` — `skipped += 1` → `= 1`
- `test_preview_continues_after_skipped_game` — `break` instead of `continue` after skip
- `test_preview_counts_all_missing_roms` — `missing += 1` → `= 1`
- `test_preview_continues_after_missing_rom` — `break` instead of `continue` after missing
- `test_preview_copy_bytes_equal_full_size_when_no_targets` — `copy_rom/media_bytes = None` in else branch
- `test_preview_skip_existing_accumulates_all_unmatched_media` — `copy_media_bytes = src_size` vs `+=`, and inverted media-match condition
- `test_copy_system_overwrites_media_when_overwrite_true` — `or` → `and` in media copy condition
- `test_copy_system_succeeds_on_second_run_to_same_target` — `exist_ok=True` → falsy variants

---

## Order of operations

1. ✅ **Confirm baseline:** `./validate` shows 105 passing.
2. ✅ **Task 1** — dry run against real data verified end-to-end. Device-boot validation still open (see Task 1 caveat).
3. ✅ **Task 2** — disk-space pre-check landed; skip-existing copy mode also landed (default-on, with `--overwrite` to force).
4. ✅ **Task 3** — ruff + mypy clean, wired into `./validate check` and `./validate all`.
5. ✅ **Task 4** — coverage at 94%; 7 tests added for real-behavior gaps.
6. **Task 5** — README behavior notes. (Skip-existing + overwrite blurb already added during Task 2; remaining behavior bullets still TODO.)
7. ✅ **Task 6** — mutation testing with `mutmut` complete. 8 new tests, 113 total.

Stop and get user sign-off between tasks. Don't bundle.

#!/usr/bin/env python3
"""
Filter ROMs by rating and copy to a target drive, preserving ES-DE structure.

Configuration is read from config.local.toml beside this script (created
automatically from config.toml on first run). CLI arguments override config.

Usage:
    python3 rom_filter_copy.py
    python3 rom_filter_copy.py --target-roms-dir /mnt/g/ROMs \\
                               --target-esde-data-dir /mnt/g/ES-DE --rating 8.0

Output structure:
    {target_roms_dir}/{system}/game.zip
    {target_esde_data_dir}/gamelists/{system}/gamelist.xml
    {target_esde_data_dir}/downloaded_media/{system}/{media_type}/game.png

ROM and ES-DE destinations are independent — they don't need to share a root.
"""

import argparse
import logging
import os
import shutil
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

from _copy import _dir_size, _free_space, _size_matches, _wsl_to_windows, copy_system
from _filters import (
    expand_raw_genre_ratings,
    expand_raw_genres,
    format_size,
    parse_rating,
    parse_rom_path,
    should_include,
)
from _media import build_media_index, build_target_media_index, parse_m3u

sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)  # type: ignore[union-attr]
sys.stderr.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)  # type: ignore[union-attr]


SCRIPT_DIR      = Path(__file__).parent
PROJECT_ROOT    = SCRIPT_DIR.parent
DEFAULT_CONFIG  = PROJECT_ROOT / "config.toml"
LOCAL_CONFIG    = PROJECT_ROOT / "config.local.toml"
CONFIG_FILE     = LOCAL_CONFIG


def load_config(path: Path) -> dict:
    if path.exists():
        with open(path, "rb") as f:
            return tomllib.load(f)
    return {}


def _merge_configs(base: dict, override: dict) -> dict:
    """Merge two config dicts; nested tables are extended rather than replaced."""
    merged = dict(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **val}
        else:
            merged[key] = val
    return merged


def preview_system(system: str, gamelist_path: Path, min_rating: float,
                   include_unrated: bool, copy_all: bool,
                   roms_dir: Path, media_dir: Path,
                   *,
                   target_roms_dir: Path | None = None,
                   target_esde_data_dir: Path | None = None,
                   overwrite: bool = True,
                   genres: set[str] | None = None,
                   skip_genres: set[str] | None = None,
                   genre_ratings: dict[str, float] | None = None) -> tuple[list[dict], int, int, list[dict]]:
    tree = ET.parse(gamelist_path)
    root = tree.getroot()

    media_index = build_media_index(system, media_dir)

    # skip-existing requires both target roots so we can probe what's already on
    # the destination filesystems. When overwrite=True (or targets missing), we
    # short-circuit the skip math and treat copy_*_bytes as the full source size.
    if not overwrite and target_roms_dir is not None and target_esde_data_dir is not None:
        skip_check        = True
        target_roms_sys   = target_roms_dir / system
        target_media_root = target_esde_data_dir / "downloaded_media"
        target_media_index = build_target_media_index(system, target_media_root)
    else:
        skip_check = False

    games = []
    missing = 0
    skipped_details: list[dict] = []

    for game in root.findall("game"):
        path_el   = game.find("path")
        rating_el = game.find("rating")

        rom_filename = parse_rom_path(path_el.text if path_el is not None else None)
        if rom_filename is None:
            continue

        rom_stem = rom_filename.stem
        rating   = parse_rating(rating_el.text if rating_el is not None else None)
        genre    = (game.findtext("genre") or "").strip()

        # Genre rating overrides raise the bar for specific genres (stronger wins).
        effective_min = min_rating
        if genre_ratings and genre:
            g_override = genre_ratings.get(genre)
            if g_override is not None:
                effective_min = max(effective_min, g_override)

        if not should_include(rating, effective_min, include_unrated, copy_all):
            src_rom = roms_dir / system / rom_filename
            try:
                rom_size = src_rom.stat().st_size
            except FileNotFoundError:
                rom_size = 0
            skipped_details.append({
                "name":     game.findtext("name") or rom_filename.stem,
                "rating":   rating,
                "rom_size": rom_size,
            })
            continue

        genre_included = genres is None or genre in genres
        genre_excluded = skip_genres is not None and genre in skip_genres
        if not genre_included or genre_excluded:
            src_rom = roms_dir / system / rom_filename
            try:
                rom_size = src_rom.stat().st_size
            except FileNotFoundError:
                rom_size = 0
            skipped_details.append({
                "name":     game.findtext("name") or rom_filename.stem,
                "rating":   rating,
                "rom_size": rom_size,
            })
            continue

        src_rom = roms_dir / system / rom_filename
        try:
            src_file_size = src_rom.stat().st_size
        except FileNotFoundError:
            missing += 1
            continue

        # For m3u playlists, collect each referenced disc image.
        # (abs_path, rel_path_within_system, size)
        m3u_discs: list[tuple[Path, Path, int]] = []
        if rom_filename.suffix.lower() == '.m3u':
            any_disc_missing = False
            for disc_abs in parse_m3u(src_rom):
                try:
                    disc_rel = disc_abs.relative_to(roms_dir / system)
                except ValueError:
                    continue  # disc outside system dir — skip
                try:
                    m3u_discs.append((disc_abs, disc_rel, disc_abs.stat().st_size))
                except FileNotFoundError:
                    any_disc_missing = True
            if any_disc_missing:
                missing += 1
                continue

        rom_size = src_file_size + sum(s for _, _, s in m3u_discs)

        media_entries = media_index.get(rom_stem, [])
        media_size = sum(size for _path, size in media_entries)

        if skip_check:
            copy_rom_bytes = 0
            if not _size_matches(target_roms_sys / rom_filename, src_file_size):
                copy_rom_bytes += src_file_size
            for _, disc_rel, disc_size in m3u_discs:
                if not _size_matches(target_roms_sys / disc_rel, disc_size):
                    copy_rom_bytes += disc_size
            copy_media_bytes = 0
            for src_path, src_size in media_entries:
                target_path = target_media_root / system / src_path.parent.name / src_path.name
                if target_media_index.get(target_path) == src_size:
                    continue
                copy_media_bytes += src_size
        else:
            copy_rom_bytes   = rom_size
            copy_media_bytes = media_size

        games.append({
            "game":             game,
            "rom_filename":     rom_filename,
            "src_rom":          src_rom,
            "src_file_size":    src_file_size,
            "m3u_discs":        m3u_discs,
            "media_files":      [p for p, _size in media_entries],
            "rom_bytes":        rom_size,
            "media_bytes":      media_size,
            "bytes":            rom_size + media_size,
            "copy_rom_bytes":   copy_rom_bytes,
            "copy_media_bytes": copy_media_bytes,
        })

    included = {g["rom_filename"] for g in games}
    for g in games:
        for _, disc_rel, _ in g["m3u_discs"]:  # type: ignore[attr-defined]
            included.add(disc_rel)
    skipped = 0
    for dirpath, _, filenames in os.walk(roms_dir / system):
        p = Path(dirpath)
        for name in filenames:
            if (p / name).relative_to(roms_dir / system) not in included:
                skipped += 1

    return games, skipped, missing, skipped_details


def main():
    if not LOCAL_CONFIG.exists() and DEFAULT_CONFIG.exists():
        shutil.copy(DEFAULT_CONFIG, LOCAL_CONFIG)

    # Pre-parse to learn which config file to load, since the full parser uses
    # config values as defaults.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=str(CONFIG_FILE))
    pre_args, _ = pre.parse_known_args()
    config_path     = Path(pre_args.config)
    config_explicit = pre_args.config != str(CONFIG_FILE)
    if config_explicit and not config_path.exists():
        sys.exit(f"ERROR: --config file not found: {config_path}")
    raw_config = load_config(config_path)
    # Use DEFAULT_CONFIG as a base so that tables defined there (e.g. genre_map)
    # are available even when config.local.toml was created before they existed.
    # Nested tables are merged; local keys win on collision.
    if DEFAULT_CONFIG.exists() and config_path.resolve() != DEFAULT_CONFIG.resolve():
        config = _merge_configs(load_config(DEFAULT_CONFIG), raw_config)
    else:
        config = raw_config

    log_path = PROJECT_ROOT / "rom_filter_copy.log"
    logging.basicConfig(
        filename=log_path,
        level=logging.ERROR,
        format="%(asctime)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        encoding="utf-8",
    )

    parser = argparse.ArgumentParser(description="Filter ROMs by rating and copy to target drive.")
    parser.add_argument("--config",                 default=str(CONFIG_FILE),                       help="Path to TOML config file (default: config.local.toml beside the script)")
    parser.add_argument("--target-roms-dir",        default=config.get("target_roms_dir"),         help="Where ROMs go. Files land at {target_roms_dir}/{system}/game.zip.")
    parser.add_argument("--target-esde-data-dir",   default=config.get("target_esde_data_dir"),    help="Where ES-DE data goes. Gamelists at {target_esde_data_dir}/gamelists/{system}/, media at {target_esde_data_dir}/downloaded_media/{system}/...")
    parser.add_argument("--roms-dir",               default=config.get("roms_dir"),                help="Root of your ROM collection, e.g. /mnt/f/ROMs")
    parser.add_argument("--esde-data-dir",          default=config.get("esde_data_dir"),           help="ES-DE data directory containing gamelists/ and downloaded_media/")
    parser.add_argument("--rating",                  type=float, default=config.get("rating", 7.0), help="Minimum rating out of 10 (default: 7.0)")
    parser.add_argument("--systems",                 nargs="*",                                      help="Limit to specific systems, e.g. --systems psx gc")
    parser.add_argument("--skip-systems",            nargs="*", default=config.get("skip_systems"),  help="Exclude specific systems, e.g. --skip-systems arcade mame")
    parser.add_argument("--include-unrated",         action="store_true", default=config.get("include_unrated", False), help="Include games with no rating data")
    parser.add_argument("--overwrite",               action="store_true", default=config.get("overwrite", False),
                                                                                                      help="Force re-copy of files that already exist on target with matching size. Default: skip existing.")
    parser.add_argument("--yes", "-y",               action="store_true",                            help="Skip confirmation prompt and proceed immediately")
    parser.add_argument("--dry-run",                 action="store_true",                            help="Preview only; do not copy any files (exits 0)")
    parser.add_argument("--verbose", "-v",           action="store_true", default=config.get("verbose", False), help="List individual game titles during preview")
    parser.add_argument("--list-systems",            action="store_true",                            help="Print available system names and exit (no copy)")
    parser.add_argument("--copy-all-systems",        nargs="*", metavar="SYSTEM",                   help="Copy these systems in full regardless of rating (overrides config value)")
    parser.add_argument("--system-ratings",          nargs="*", metavar="SYSTEM=RATING",            help="Per-system rating overrides, e.g. --system-ratings n3ds=7.5 psx=6.0")
    parser.add_argument("--genre-ratings",           nargs="*", metavar="GENRE=RATING",             help="Per-genre rating overrides (stronger wins), e.g. --genre-ratings Sports=9.0")
    parser.add_argument("--genres",                  nargs="*", metavar="GENRE",                    help="Limit to genres matching these substrings, e.g. --genres Sports Shoot (games with no genre are excluded)")
    parser.add_argument("--skip-genres",             nargs="*", default=config.get("skip_genres"),  metavar="GENRE", help="Exclude genres matching these substrings, e.g. --skip-genres Casino Sports")
    args = parser.parse_args()

    if args.systems and args.skip_systems:
        parser.error("--systems and --skip-systems are mutually exclusive.")
    if args.genres and args.skip_genres:
        parser.error("--genres and --skip-genres are mutually exclusive.")
    if args.yes and args.dry_run:
        parser.error("--yes and --dry-run are mutually exclusive.")

    if not args.roms_dir:
        parser.error("--roms-dir is required (or set 'roms_dir' in config.local.toml)")
    if not args.esde_data_dir:
        parser.error("--esde-data-dir is required (or set 'esde_data_dir' in config.local.toml)")

    roms_dir      = Path(_wsl_to_windows(args.roms_dir))
    esde_data_dir = Path(_wsl_to_windows(args.esde_data_dir))
    gamelists_dir = esde_data_dir / "gamelists"
    media_dir     = esde_data_dir / "downloaded_media"

    # Validate source paths upfront so failures point at the actual problem instead
    # of bubbling up later as misleading "all games missing" output.
    if not roms_dir.is_dir():
        sys.exit(f"ERROR: --roms-dir does not exist or is not a directory: {roms_dir}")
    if not esde_data_dir.is_dir():
        sys.exit(f"ERROR: --esde-data-dir does not exist or is not a directory: {esde_data_dir}")
    if not gamelists_dir.is_dir():
        sys.exit(
            f"ERROR: gamelists/ subdirectory not found under --esde-data-dir: {gamelists_dir}\n"
            f"       Make sure ES-DE has scraped your library at least once."
        )

    if args.list_systems:
        available = sorted(p.name for p in gamelists_dir.iterdir() if p.is_dir())
        for s in available:
            print(s)
        return

    if not args.target_roms_dir:
        parser.error("--target-roms-dir is required. Pass --target-roms-dir /path, or set 'target_roms_dir' in config.toml.")
    if not args.target_esde_data_dir:
        parser.error("--target-esde-data-dir is required. Pass --target-esde-data-dir /path, or set 'target_esde_data_dir' in config.toml.")

    copy_all_systems     = set(config.get("copy_all_systems", []))
    if args.copy_all_systems is not None:
        copy_all_systems = set(args.copy_all_systems)
    target_roms_dir      = Path(_wsl_to_windows(args.target_roms_dir))
    target_esde_data_dir = Path(_wsl_to_windows(args.target_esde_data_dir))
    min_rating           = args.rating / 10.0

    genres_include: set[str] | None = set(args.genres) if args.genres else None
    genres_skip: set[str] | None = set(args.skip_genres) if args.skip_genres else None

    system_ratings: dict[str, float] = {
        k: float(v) / 10.0 for k, v in config.get("system_ratings", {}).items()
    }
    if args.system_ratings:
        for item in args.system_ratings:
            try:
                k, v = item.split("=", 1)
                system_ratings[k.strip()] = float(v) / 10.0
            except ValueError:
                parser.error(f"--system-ratings: invalid format {item!r}, expected SYSTEM=RATING")

    genre_ratings: dict[str, float] = {
        k: float(v) / 10.0 for k, v in config.get("genre_ratings", {}).items()
    }
    if args.genre_ratings:
        for item in args.genre_ratings:
            try:
                k, v = item.split("=", 1)
                genre_ratings[k.strip()] = float(v) / 10.0
            except ValueError:
                parser.error(f"--genre-ratings: invalid format {item!r}, expected GENRE=RATING")

    genre_map: dict[str, list[str]] = config.get("genre_map", {})
    if genre_map:
        if genres_include is not None:
            genres_include = expand_raw_genres(genres_include, genre_map)
        if genres_skip is not None:
            genres_skip = expand_raw_genres(genres_skip, genre_map)
        if genre_ratings:
            genre_ratings = expand_raw_genre_ratings(genre_ratings, genre_map)

    print(f"ROMs dir:        {roms_dir}")
    print(f"ES-DE data dir:  {esde_data_dir}")
    print(f"ROMs target:     {target_roms_dir}")
    print(f"ES-DE target:    {target_esde_data_dir}")
    print(f"Min rating:      {args.rating}/10")
    print(f"Include unrated: {args.include_unrated}")
    print(f"Mode:            {'overwrite' if args.overwrite else 'skip-existing'}")
    if copy_all_systems:
        print(f"Copy all:        {', '.join(sorted(copy_all_systems))}")
    print()

    available = sorted(p.name for p in gamelists_dir.iterdir() if p.is_dir())
    if args.systems:
        unknown = sorted(set(args.systems) - set(available))
        if unknown:
            print(
                f"WARNING: --systems names not found in {gamelists_dir}: {', '.join(unknown)}",
                file=sys.stderr,
            )
            print(f"         Available: {', '.join(available)}", file=sys.stderr)
        systems = [s for s in available if s in args.systems]
        if not systems:
            sys.exit("ERROR: --systems filter matched no available systems. Nothing to do.")
    elif args.skip_systems:
        skip_set = set(args.skip_systems)
        unknown = sorted(skip_set - set(available))
        if unknown:
            print(
                f"WARNING: --skip-systems names not found in {gamelists_dir}: {', '.join(unknown)}",
                file=sys.stderr,
            )
            print(f"         Available: {', '.join(available)}", file=sys.stderr)
        systems = [s for s in available if s not in skip_set]
        if not systems:
            sys.exit("ERROR: --skip-systems excluded all available systems. Nothing to do.")
    else:
        systems = available

    print("Previewing selection...\n")

    plan                  = {}
    total_included        = 0
    total_skipped         = 0
    total_missing         = 0
    total_bytes           = 0
    total_source_bytes    = 0
    total_copy_rom_bytes  = 0
    total_copy_esde_bytes = 0

    for system in systems:
        gamelist_path = gamelists_dir / system / "gamelist.xml"
        if not gamelist_path.exists():
            continue

        copy_all             = system in copy_all_systems
        effective_min_rating = system_ratings.get(system, min_rating)
        try:
            games, skipped, missing, skipped_details = preview_system(
                system, gamelist_path, effective_min_rating, args.include_unrated, copy_all,
                roms_dir, media_dir,
                target_roms_dir=target_roms_dir,
                target_esde_data_dir=target_esde_data_dir,
                overwrite=args.overwrite,
                genres=genres_include,
                skip_genres=genres_skip,
                genre_ratings=genre_ratings or None,
            )
        except ET.ParseError as e:
            # One bad gamelist shouldn't kill the whole run.
            print(
                f"  [{system}]  WARNING: skipping — gamelist.xml is malformed ({e})",
                file=sys.stderr,
            )
            continue
        sys_bytes           = sum(g["bytes"]            for g in games)
        sys_copy_rom_bytes  = sum(g["copy_rom_bytes"]   for g in games)
        sys_copy_esde_bytes = sum(g["copy_media_bytes"] for g in games)
        sys_copy_bytes      = sys_copy_rom_bytes + sys_copy_esde_bytes
        sys_source_bytes    = _dir_size(roms_dir / system) + _dir_size(media_dir / system)

        tag = " (copy all)" if copy_all else (
            f" (rating: {effective_min_rating * 10:g})" if system in system_ratings else ""
        )
        missing_str = f"  missing: {missing}" if missing else ""
        size_str = (f"{format_size(sys_bytes)} / {format_size(sys_source_bytes)}"
                    if skipped > 0 and sys_source_bytes > 0 else format_size(sys_bytes))
        # Only mention "to copy" when skip-existing actually saved something.
        copy_str = f"  to copy: {format_size(sys_copy_bytes)}" if sys_copy_bytes < sys_bytes else ""
        print(f"  [{system}]{tag}  included: {len(games)}  skipped: {skipped}{missing_str}  size: {size_str}{copy_str}")

        if args.verbose:
            for g in games:
                title = g["game"].findtext("name") or str(g["rom_filename"].stem)
                print(f"    + {title}")
            for s in sorted(skipped_details, key=lambda x: (x["rating"] is None, x["rating"] or 0)):
                rating_str = f"{s['rating'] * 10:.1f}" if s["rating"] is not None else "unrated"
                size_str   = format_size(s["rom_size"]) if s["rom_size"] else "not on disk"
                print(f"    - {s['name']}  [{rating_str}]  {size_str}")

        plan[system]           = games
        total_included        += len(games)
        total_skipped         += skipped
        total_missing         += missing
        total_bytes           += sys_bytes
        total_source_bytes    += sys_source_bytes
        total_copy_rom_bytes  += sys_copy_rom_bytes
        total_copy_esde_bytes += sys_copy_esde_bytes

    total_copy_bytes = total_copy_rom_bytes + total_copy_esde_bytes
    print()
    source_str = f" / {format_size(total_source_bytes)}" if total_skipped > 0 and total_source_bytes > 0 else ""
    if total_copy_bytes < total_bytes:
        savings = total_bytes - total_copy_bytes
        print(f"Total: {total_included} games  ({format_size(total_bytes)}{source_str}; {format_size(total_copy_bytes)} to copy, {format_size(savings)} already on target)")
    else:
        print(f"Total: {total_included} games  ({format_size(total_bytes)}{source_str})")
    print(f"       {total_skipped} games on disk, not included")
    if total_missing:
        print(f"       {total_missing} games skipped (ROM file not found on disk)")
    print()

    for label, target, needed in (
        ("--target-roms-dir",      target_roms_dir,      total_copy_rom_bytes),
        ("--target-esde-data-dir", target_esde_data_dir, total_copy_esde_bytes),
    ):
        free = _free_space(target)
        if needed > free:
            sys.exit(
                f"ERROR: Not enough free space on {label} target.\n"
                f"       Needed: {format_size(needed)}\n"
                f"       Free:   {format_size(free)} on {target}"
            )

    if args.dry_run:
        print("Dry run complete. No files were copied.")
        return

    if args.yes:
        answer = "y"
    else:
        try:
            answer = input("Proceed with copy? [y/N] ").strip().lower()
        except EOFError:
            # Ctrl-D / closed stdin → treat as decline rather than crashing.
            print()
            answer = ""
    if answer != "y":
        print("Aborted.")
        sys.exit(1)

    print()
    systems_to_copy = [(s, g) for s, g in plan.items() if g]
    try:
        for idx, (system, games) in enumerate(systems_to_copy, start=1):
            print(f"Copying [{system}] ({idx}/{len(systems_to_copy)} systems)...")
            copy_system(system, games, target_roms_dir, target_esde_data_dir, overwrite=args.overwrite)
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)

    print()
    if total_copy_bytes < total_bytes:
        print(f"Done. {total_included} games on target ({format_size(total_bytes)}; {format_size(total_copy_bytes)} written this run).")
    else:
        print(f"Done. {total_included} games copied ({format_size(total_bytes)}).")


if __name__ == "__main__":
    main()

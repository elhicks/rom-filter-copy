#!/usr/bin/env python3
"""
Filter ROMs by rating and copy to a target drive, preserving ES-DE structure.

Configuration is read from config.toml beside this script.
CLI arguments override config file values.

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
import os
import sys
import tomllib
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

SCRIPT_DIR  = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "config.toml"


def load_config(path: Path) -> dict:
    if path.exists():
        with open(path, "rb") as f:
            return tomllib.load(f)
    return {}


def format_size(total_bytes: int) -> str:
    size = float(total_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def parse_rating(text: str | None) -> float | None:
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_rom_path(text: str | None) -> Path | None:
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    stripped = text.removeprefix("./")
    if not stripped:
        return None
    path = Path(stripped)
    # Reject anything that would escape the system's ROM dir when joined.
    if path.is_absolute() or ".." in path.parts:
        return None
    return path


def should_include(rating: float | None, min_rating: float,
                   include_unrated: bool, copy_all: bool) -> bool:
    if copy_all:
        return True
    if rating is None:
        return include_unrated
    return rating >= min_rating


def build_target_media_index(system: str, target_media_dir: Path) -> dict[Path, int]:
    # Mirrors build_media_index's scandir pass but keyed by full target path,
    # since skip-existing matches by exact destination path + size.
    sys_dir = target_media_dir / system
    index: dict[Path, int] = {}
    try:
        type_entries = list(os.scandir(sys_dir))
    except FileNotFoundError:
        return index
    for type_entry in type_entries:
        if not type_entry.is_dir():
            continue
        with os.scandir(type_entry.path) as files:
            for f in files:
                if not f.is_file():
                    continue
                index[Path(f.path)] = f.stat().st_size
    return index


def _size_matches(dst: Path, expected_size: int) -> bool:
    try:
        return dst.stat().st_size == expected_size
    except FileNotFoundError:
        return False


def build_media_index(system: str, media_dir: Path) -> dict[str, list[tuple[Path, int]]]:
    # One scandir pass over the system's media tree, keyed by ROM stem and
    # carrying file size alongside the path. Critical on WSL→NTFS:
    #   - scandir's d_type lets is_file() skip a stat per entry,
    #   - capturing entry.stat() once means callers don't restat for size.
    src_system_media = media_dir / system
    index: dict[str, list[tuple[Path, int]]] = {}
    try:
        type_entries = list(os.scandir(src_system_media))
    except FileNotFoundError:
        return index
    for type_entry in type_entries:
        if not type_entry.is_dir():
            continue
        with os.scandir(type_entry.path) as files:
            for f in files:
                if not f.is_file():
                    continue
                path = Path(f.path)
                index.setdefault(path.stem, []).append((path, f.stat().st_size))
    return index


def preview_system(system: str, gamelist_path: Path, min_rating: float,
                   include_unrated: bool, copy_all: bool,
                   roms_dir: Path, media_dir: Path,
                   *,
                   target_roms_dir: Path | None = None,
                   target_esde_data_dir: Path | None = None,
                   overwrite: bool = True) -> tuple[list[dict], int, int]:
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
    skipped = 0
    missing = 0

    for game in root.findall("game"):
        path_el   = game.find("path")
        rating_el = game.find("rating")

        rom_filename = parse_rom_path(path_el.text if path_el is not None else None)
        if rom_filename is None:
            continue

        rom_stem = rom_filename.stem
        rating   = parse_rating(rating_el.text if rating_el is not None else None)

        if not should_include(rating, min_rating, include_unrated, copy_all):
            skipped += 1
            continue

        src_rom = roms_dir / system / rom_filename
        try:
            rom_size = src_rom.stat().st_size
        except FileNotFoundError:
            missing += 1
            continue

        media_entries = media_index.get(rom_stem, [])
        media_size = sum(size for _path, size in media_entries)

        if skip_check:
            rom_already = _size_matches(target_roms_sys / rom_filename, rom_size)
            copy_rom_bytes = 0 if rom_already else rom_size
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
            "media_files":      [p for p, _size in media_entries],
            "rom_bytes":        rom_size,
            "media_bytes":      media_size,
            "bytes":            rom_size + media_size,
            "copy_rom_bytes":   copy_rom_bytes,
            "copy_media_bytes": copy_media_bytes,
        })

    return games, skipped, missing


def copy_system(system: str, games: list[dict],
                target_roms_dir: Path, target_esde_data_dir: Path,
                *, overwrite: bool = True):
    target_roms   = target_roms_dir / system
    target_media  = target_esde_data_dir / "downloaded_media"
    target_gl_dir = target_esde_data_dir / "gamelists" / system

    for entry in games:
        src_rom = entry["src_rom"]
        if src_rom.exists():
            dst = target_roms / entry["rom_filename"]
            if overwrite or not _size_matches(dst, entry["rom_bytes"]):
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_rom, dst)

        for f in entry["media_files"]:
            dst = target_media / system / f.parent.name / f.name
            if overwrite or not _size_matches(dst, f.stat().st_size):
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst)

    # gamelist.xml is the canonical metadata index — always rewrite so changes
    # to ratings/desc/etc. propagate even when no media bytes change.
    new_root = ET.Element("gameList")
    for entry in games:
        new_root.append(entry["game"])
    target_gl_dir.mkdir(parents=True, exist_ok=True)
    ET.indent(new_root, space="\t")
    ET.ElementTree(new_root).write(
        target_gl_dir / "gamelist.xml",
        encoding="utf-8",
        xml_declaration=True,
    )


def _free_space(path: Path) -> int:
    # Targets may not exist yet (first run on a freshly-formatted card); probe
    # the nearest existing ancestor so we still get a real disk-usage reading.
    check_at = path
    while not check_at.exists():
        if check_at.parent == check_at:
            sys.exit(f"ERROR: target path's parent does not exist: {path}")
        check_at = check_at.parent
    return shutil.disk_usage(check_at).free


def main():
    # Pre-parse to learn which config file to load, since the full parser uses
    # config values as defaults.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=str(CONFIG_FILE))
    pre_args, _ = pre.parse_known_args()
    config_path     = Path(pre_args.config)
    config_explicit = pre_args.config != str(CONFIG_FILE)
    if config_explicit and not config_path.exists():
        sys.exit(f"ERROR: --config file not found: {config_path}")
    config = load_config(config_path)

    parser = argparse.ArgumentParser(description="Filter ROMs by rating and copy to target drive.")
    parser.add_argument("--config",                 default=str(CONFIG_FILE),                       help="Path to TOML config file (default: config.toml beside the script)")
    parser.add_argument("--target-roms-dir",        default=config.get("target_roms_dir"),         help="Where ROMs go. Files land at {target_roms_dir}/{system}/game.zip.")
    parser.add_argument("--target-esde-data-dir",   default=config.get("target_esde_data_dir"),    help="Where ES-DE data goes. Gamelists at {target_esde_data_dir}/gamelists/{system}/, media at {target_esde_data_dir}/downloaded_media/{system}/...")
    parser.add_argument("--roms-dir",               default=config.get("roms_dir"),                help="Root of your ROM collection, e.g. /mnt/f/ROMs")
    parser.add_argument("--esde-data-dir",          default=config.get("esde_data_dir"),           help="ES-DE data directory containing gamelists/ and downloaded_media/")
    parser.add_argument("--rating",                  type=float, default=config.get("rating", 7.0), help="Minimum rating out of 10 (default: 7.0)")
    parser.add_argument("--systems",                 nargs="*",                                      help="Limit to specific systems, e.g. --systems psx gc")
    parser.add_argument("--skip-systems",            nargs="*", default=config.get("skip_systems"),  help="Exclude specific systems, e.g. --skip-systems arcade mame")
    parser.add_argument("--include-unrated",         action="store_true",                            help="Include games with no rating data")
    parser.add_argument("--overwrite",               action="store_true", default=config.get("overwrite", False),
                                                                                                      help="Force re-copy of files that already exist on target with matching size. Default: skip existing.")
    parser.add_argument("--yes", "-y",               action="store_true",                            help="Skip confirmation prompt and proceed immediately")
    parser.add_argument("--dry-run",                 action="store_true",                            help="Preview only; do not copy any files (exits 0)")
    parser.add_argument("--verbose", "-v",           action="store_true",                            help="List individual game titles during preview")
    parser.add_argument("--list-systems",            action="store_true",                            help="Print available system names and exit (no copy)")
    parser.add_argument("--copy-all-systems",        nargs="*", metavar="SYSTEM",                   help="Copy these systems in full regardless of rating (overrides config value)")
    args = parser.parse_args()

    if args.systems and args.skip_systems:
        parser.error("--systems and --skip-systems are mutually exclusive.")
    if args.yes and args.dry_run:
        parser.error("--yes and --dry-run are mutually exclusive.")

    if not args.roms_dir:
        parser.error("--roms-dir is required (or set 'roms_dir' in config.toml)")
    if not args.esde_data_dir:
        parser.error("--esde-data-dir is required (or set 'esde_data_dir' in config.toml)")

    roms_dir      = Path(args.roms_dir)
    esde_data_dir = Path(args.esde_data_dir)
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
    target_roms_dir      = Path(args.target_roms_dir)
    target_esde_data_dir = Path(args.target_esde_data_dir)
    min_rating           = args.rating / 10.0

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
    total_copy_rom_bytes  = 0
    total_copy_esde_bytes = 0

    for system in systems:
        gamelist_path = gamelists_dir / system / "gamelist.xml"
        if not gamelist_path.exists():
            continue

        copy_all = system in copy_all_systems
        try:
            games, skipped, missing = preview_system(
                system, gamelist_path, min_rating, args.include_unrated, copy_all,
                roms_dir, media_dir,
                target_roms_dir=target_roms_dir,
                target_esde_data_dir=target_esde_data_dir,
                overwrite=args.overwrite,
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

        tag = " (copy all)" if copy_all else ""
        missing_str = f"  missing: {missing}" if missing else ""
        # Only mention "to copy" when skip-existing actually saved something.
        copy_str = f"  to copy: {format_size(sys_copy_bytes)}" if sys_copy_bytes < sys_bytes else ""
        print(f"  [{system}]{tag}  included: {len(games)}  skipped: {skipped}{missing_str}  size: {format_size(sys_bytes)}{copy_str}")

        if args.verbose:
            for g in games:
                title = g["game"].findtext("name") or str(g["rom_filename"].stem)
                print(f"    {title}")

        plan[system]           = games
        total_included        += len(games)
        total_skipped         += skipped
        total_missing         += missing
        total_bytes           += sys_bytes
        total_copy_rom_bytes  += sys_copy_rom_bytes
        total_copy_esde_bytes += sys_copy_esde_bytes

    total_copy_bytes = total_copy_rom_bytes + total_copy_esde_bytes
    print()
    if total_copy_bytes < total_bytes:
        savings = total_bytes - total_copy_bytes
        print(f"Total: {total_included} games  ({format_size(total_bytes)}; {format_size(total_copy_bytes)} to copy, {format_size(savings)} already on target)")
    else:
        print(f"Total: {total_included} games  ({format_size(total_bytes)})")
    print(f"       {total_skipped} games skipped (below rating threshold)")
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
    for idx, (system, games) in enumerate(systems_to_copy, start=1):
        print(f"Copying [{system}] ({idx}/{len(systems_to_copy)} systems)...")
        copy_system(system, games, target_roms_dir, target_esde_data_dir, overwrite=args.overwrite)

    print()
    if total_copy_bytes < total_bytes:
        print(f"Done. {total_included} games on target ({format_size(total_bytes)}; {format_size(total_copy_bytes)} written this run).")
    else:
        print(f"Done. {total_included} games copied ({format_size(total_bytes)}).")


if __name__ == "__main__":
    main()

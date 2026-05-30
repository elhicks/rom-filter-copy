import argparse
import sys
from pathlib import Path

from _config import CONFIG_FILE
from _copy import _wsl_to_windows
from _filters import expand_raw_genre_ratings, expand_raw_genres


def _build_parser(config: dict) -> argparse.ArgumentParser:
    """Build and return the argument parser pre-populated with config defaults."""
    parser = argparse.ArgumentParser(
        description="Filter ROMs by rating and copy to target drive.")
    parser.add_argument("--config",
                        default=str(CONFIG_FILE),
                        help="Path to TOML config file"
                             " (default: config.local.toml beside the script)")
    parser.add_argument("--target-roms-dir",
                        default=config.get("target_roms_dir"),
                        help="Where ROMs go."
                             " Files land at {target_roms_dir}/{system}/game.zip.")
    parser.add_argument("--target-esde-data-dir",
                        default=config.get("target_esde_data_dir"),
                        help="Where ES-DE data goes."
                             " Gamelists at {target_esde_data_dir}/gamelists/{system}/,"
                             " media at {target_esde_data_dir}/downloaded_media/{system}/...")
    parser.add_argument("--roms-dir",
                        default=config.get("roms_dir"),
                        help="Root of your ROM collection, e.g. /mnt/f/ROMs")
    parser.add_argument("--esde-data-dir",
                        default=config.get("esde_data_dir"),
                        help="ES-DE data directory containing gamelists/ and downloaded_media/")
    parser.add_argument("--rating",
                        type=float, default=config.get("rating", 7.0),
                        help="Minimum rating out of 10 (default: 7.0)")
    parser.add_argument("--systems",
                        nargs="*",
                        help="Limit to specific systems, e.g. --systems psx gc")
    parser.add_argument("--skip-systems",
                        nargs="*", default=config.get("skip_systems"),
                        help="Exclude specific systems, e.g. --skip-systems arcade mame")
    parser.add_argument("--include-unrated",
                        action="store_true",
                        default=config.get("include_unrated", False),
                        help="Include games with no rating data")
    parser.add_argument("--overwrite",
                        action="store_true", default=config.get("overwrite", False),
                        help="Force re-copy of files that already exist on target"
                             " with matching size. Default: skip existing.")
    parser.add_argument("--prune",
                        action="store_true", default=config.get("prune", False),
                        help="After copying, delete ROMs on the target that exist in"
                             " the source gamelist but no longer pass the current filter.")
    parser.add_argument("--yes", "-y",
                        action="store_true",
                        help="Skip confirmation prompt and proceed immediately")
    parser.add_argument("--dry-run",
                        action="store_true",
                        help="Preview only; do not copy any files (exits 0)")
    parser.add_argument("--verbose", "-v",
                        action="store_true", default=config.get("verbose", False),
                        help="List individual game titles during preview")
    parser.add_argument("--list-systems",
                        action="store_true",
                        help="Print available system names and exit (no copy)")
    parser.add_argument("--copy-all-systems",
                        nargs="*", metavar="SYSTEM",
                        help="Copy these systems in full regardless of rating"
                             " (overrides config value)")
    parser.add_argument("--system-ratings",
                        nargs="*", metavar="SYSTEM=RATING",
                        help="Per-system rating overrides,"
                             " e.g. --system-ratings n3ds=7.5 psx=6.0")
    parser.add_argument("--genre-ratings",
                        nargs="*", metavar="GENRE=RATING",
                        help="Per-genre rating overrides (stronger wins),"
                             " e.g. --genre-ratings Sports=9.0")
    parser.add_argument("--genres",
                        nargs="*", metavar="GENRE",
                        help="Limit to genres matching these substrings,"
                             " e.g. --genres Sports Shoot"
                             " (games with no genre are excluded)")
    parser.add_argument("--skip-genres",
                        nargs="*", default=config.get("skip_genres"),
                        metavar="GENRE",
                        help="Exclude genres matching these substrings,"
                             " e.g. --skip-genres Casino Sports")
    parser.add_argument("--bypass-keywords",
                        nargs="*", metavar="KEYWORD",
                        default=config.get("bypass_keywords"),
                        help="Always include games whose metadata contains any of these strings")
    parser.add_argument("--exclude-keywords",
                        nargs="*", metavar="KEYWORD",
                        default=config.get("exclude_keywords"),
                        help="Always exclude games whose metadata contains any of these strings")
    return parser


def _resolve_args(args, config: dict, parser: argparse.ArgumentParser) -> dict:
    """Validate and normalise parsed args; return a dict of resolved values.

    Raises parser.error (exits) on bad input.
    """
    copy_all_systems: set[str] = set(config.get("copy_all_systems", []))
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

    return {
        "copy_all_systems":    copy_all_systems,
        "target_roms_dir":     target_roms_dir,
        "target_esde_data_dir": target_esde_data_dir,
        "min_rating":          min_rating,
        "genres_include":      genres_include,
        "genres_skip":         genres_skip,
        "system_ratings":      system_ratings,
        "genre_ratings":       genre_ratings,
        "bypass_keywords":     set(args.bypass_keywords) if args.bypass_keywords else None,
        "exclude_keywords":    set(args.exclude_keywords) if args.exclude_keywords else None,
    }


def _validate_args(args, parser: argparse.ArgumentParser) -> None:
    """Check for mutually exclusive flags and required arguments; call parser.error on failure."""
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


def _validate_source_paths(
    roms_dir: Path,
    esde_data_dir: Path,
    gamelists_dir: Path,
) -> None:
    """Exit with an error message if any required source path is missing."""
    if not roms_dir.is_dir():
        sys.exit(f"ERROR: --roms-dir does not exist or is not a directory: {roms_dir}")
    if not esde_data_dir.is_dir():
        sys.exit(f"ERROR: --esde-data-dir does not exist or is not a directory: {esde_data_dir}")
    if not gamelists_dir.is_dir():
        sys.exit(
            f"ERROR: gamelists/ subdirectory not found under --esde-data-dir: {gamelists_dir}\n"
            f"       Make sure ES-DE has scraped your library at least once."
        )


def _resolve_systems(
    args,
    gamelists_dir: Path,
) -> list[str]:
    """Return the ordered list of system names to process, honouring --systems / --skip-systems."""
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
    return systems


def _confirm_or_abort(args) -> None:
    """Prompt for confirmation (unless --yes), exit 1 if declined."""
    if args.yes:
        return
    try:
        answer = input("Proceed with copy? [y/N] ").strip().lower()
    except EOFError:
        # Ctrl-D / closed stdin → treat as decline rather than crashing.
        print()
        answer = ""
    if answer != "y":
        print("Aborted.")
        sys.exit(1)

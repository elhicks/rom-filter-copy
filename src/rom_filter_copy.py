#!/usr/bin/env python3
"""
Filter ROMs by rating and copy to a target drive, preserving ES-DE structure.

Configuration is read from config.local.toml beside this script (created
automatically from config.toml on first run). CLI arguments override config.

Usage:
    python3 rom_filter_copy.py
    python3 rom_filter_copy.py --target-roms-dir /mnt/g/ROMs \
                               --target-esde-data-dir /mnt/g/ES-DE --rating 8.0

Output structure:
    {target_roms_dir}/{system}/game.zip
    {target_esde_data_dir}/gamelists/{system}/gamelist.xml
    {target_esde_data_dir}/downloaded_media/{system}/{media_type}/game.png

ROM and ES-DE destinations are independent — they don't need to share a root.
"""

import shutil
import sys
from pathlib import Path

from _cli import (
    _build_parser,
    _confirm_or_abort,
    _resolve_args,
    _resolve_systems,
    _validate_args,
    _validate_source_paths,
)
from _config import (
    DEFAULT_CONFIG,
    LOCAL_CONFIG,
    _preparse_config,
    _setup_logging,
)
from _copy import (
    _CopyParams,
    _check_capacity,
    _execute_copy,
    _wsl_to_windows,
)
from _preview import (
    _dry_run_exit,
    _print_run_header,
    _print_summary,
    _print_totals,
    _run_preview,
)

sys.stdout.reconfigure(  # type: ignore[union-attr]
    encoding='utf-8', errors='replace', line_buffering=True)
sys.stderr.reconfigure(  # type: ignore[union-attr]
    encoding='utf-8', errors='replace', line_buffering=True)


def main():
    if not LOCAL_CONFIG.exists() and DEFAULT_CONFIG.exists():
        shutil.copy(DEFAULT_CONFIG, LOCAL_CONFIG)

    config = _preparse_config()

    _setup_logging()
    parser = _build_parser(config)
    args   = parser.parse_args()
    _validate_args(args, parser)

    esde_data_dir = Path(_wsl_to_windows(args.esde_data_dir))
    gamelists_dir = esde_data_dir / "gamelists"

    # Validate source paths upfront so failures point at the actual problem instead
    # of bubbling up later as misleading "all games missing" output.
    _validate_source_paths(Path(_wsl_to_windows(args.roms_dir)), esde_data_dir, gamelists_dir)

    if args.list_systems:
        for s in sorted(p.name for p in gamelists_dir.iterdir() if p.is_dir()):
            print(s)
        return

    if not args.target_roms_dir:
        parser.error(
            "--target-roms-dir is required."
            " Pass --target-roms-dir /path, or set 'target_roms_dir' in config.toml.")
    if not args.target_esde_data_dir:
        parser.error(
            "--target-esde-data-dir is required."
            " Pass --target-esde-data-dir /path,"
            " or set 'target_esde_data_dir' in config.toml.")

    resolved = _resolve_args(args, config, parser)
    resolved["roms_dir"]      = Path(_wsl_to_windows(args.roms_dir))
    resolved["media_dir"]     = esde_data_dir / "downloaded_media"
    resolved["gamelists_dir"] = gamelists_dir

    _print_run_header(args, resolved, esde_data_dir)

    systems = _resolve_systems(args, gamelists_dir)
    print("Previewing selection...\n")

    plan, plan_skipped, totals = _run_preview(systems, args, resolved)

    total_copy_bytes = totals.total_copy_rom_bytes + totals.total_copy_esde_bytes
    print()
    _print_totals(totals, total_copy_bytes, args.prune)
    print()

    _check_capacity(resolved["target_roms_dir"], resolved["target_esde_data_dir"],
                    totals.total_copy_rom_bytes, totals.total_copy_esde_bytes)

    if args.dry_run:
        _dry_run_exit(args, totals)
        return

    _confirm_or_abort(args)

    cp = _CopyParams(
        target_roms_dir=resolved["target_roms_dir"],
        target_esde_data_dir=resolved["target_esde_data_dir"],
        overwrite=args.overwrite,
        do_prune=args.prune,
    )
    print()
    run_deleted_bytes = _execute_copy(plan, plan_skipped, cp)
    print()
    _print_summary(totals.total_included, totals.total_bytes,
                   total_copy_bytes, run_deleted_bytes)


if __name__ == "__main__":
    main()

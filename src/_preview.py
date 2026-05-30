import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from _copy import _dir_size, _size_matches
from _filters import (
    format_size,
    matches_any_keyword,
    parse_rating,
    parse_rom_path,
    should_include,
)
from _media import build_media_index, build_target_media_index, parse_m3u


@dataclass
class PreviewOptions:  # pylint: disable=too-many-instance-attributes
    """Bundled filter / behaviour parameters for preview_system."""
    min_rating: float
    include_unrated: bool
    copy_all: bool
    overwrite: bool = True
    genres: set[str] | None = None
    skip_genres: set[str] | None = None
    genre_ratings: dict[str, float] | None = field(default=None)
    target_roms_dir: Path | None = None
    target_esde_data_dir: Path | None = None
    bypass_keywords: set[str] | None = None
    exclude_keywords: set[str] | None = None


@dataclass
class _SystemPreview:  # pylint: disable=too-many-instance-attributes
    """Aggregated preview data for one system, passed to display helpers."""
    system: str
    copy_all: bool
    effective_min_rating: float
    system_ratings: dict
    games: list
    skipped: int
    missing: int
    sys_bytes: int
    sys_source_bytes: int
    sys_copy_bytes: int
    sys_prune_count: int
    sys_prune_bytes: int


@dataclass
class _PreviewTotals:  # pylint: disable=too-many-instance-attributes
    """Aggregated preview totals across all systems."""
    total_included: int = 0
    total_skipped: int = 0
    total_missing: int = 0
    total_bytes: int = 0
    total_source_bytes: int = 0
    total_copy_rom_bytes: int = 0
    total_copy_esde_bytes: int = 0
    total_prune_count: int = 0
    total_prune_bytes: int = 0


def _collect_m3u_discs(
    src_rom: Path,
    roms_sys_dir: Path,
) -> list[tuple[Path, Path, int]] | None:
    """Return disc list for a .m3u playlist, or None if any disc file is missing.

    Each entry is (abs_path, rel_path, size).
    """
    discs: list[tuple[Path, Path, int]] = []
    for disc_abs in parse_m3u(src_rom):
        try:
            disc_rel = disc_abs.relative_to(roms_sys_dir)
        except ValueError:
            continue  # disc outside system dir — skip
        try:
            discs.append((disc_abs, disc_rel, disc_abs.stat().st_size))
        except FileNotFoundError:
            return None
    return discs


def _effective_min_rating(opts: "PreviewOptions", genre: str) -> float:
    """Return the effective minimum rating for *genre*, applying genre_ratings overrides."""
    base = opts.min_rating
    if opts.genre_ratings and genre:
        override = opts.genre_ratings.get(genre)
        if override is not None:
            return max(base, override)
    return base


def _skipped_detail_for(
    game, rom_filename: Path, roms_sys_dir: Path, rating
) -> dict:
    """Build the skipped-detail dict for a game that failed rating/genre filters."""
    try:
        rom_size = (roms_sys_dir / rom_filename).stat().st_size
    except FileNotFoundError:
        rom_size = 0
    return {
        "name":         game.findtext("name") or rom_filename.stem,
        "rating":       rating,
        "rom_size":     rom_size,
        "rom_filename": rom_filename,
    }


def _build_game_entry(
    rom_filename: Path,
    src_rom: Path,
    src_file_size: int,
    m3u_discs: list,
    media_index: dict,
) -> dict:
    """Build the entry dict (without 'game' key) for a ROM that passed all filters."""
    media_entries = media_index.get(rom_filename.stem, [])
    rom_size      = src_file_size + sum(s for _, _, s in m3u_discs)
    media_size    = sum(size for _path, size in media_entries)
    return {
        "rom_filename":   rom_filename,
        "src_rom":        src_rom,
        "src_file_size":  src_file_size,
        "m3u_discs":      m3u_discs,
        "media_entries":  media_entries,   # temporary; stripped before returning
        "media_files":    [p for p, _size in media_entries],
        "rom_bytes":      rom_size,
        "media_bytes":    media_size,
        "bytes":          rom_size + media_size,
    }


def _parse_game_entry(  # pylint: disable=too-many-return-statements
    game,  # xml.etree.ElementTree.Element
    roms_dir: Path,
    system: str,
    media_index: dict,
    opts: "PreviewOptions",
) -> tuple[dict | None, bool, bool]:
    """Parse one <game> XML element.

    Returns (entry_dict, is_missing, is_skipped).
    - entry_dict is None when the game is filtered out or an error prevents inclusion.
    - is_missing=True when the ROM file is absent from disk (should not be filtered out).
    - is_skipped=True when the entry was added to skipped_details (caller should append).
    """
    rom_filename = parse_rom_path(game.findtext("path"))
    if rom_filename is None:
        return None, False, False

    rating        = parse_rating(game.findtext("rating"))
    genre         = (game.findtext("genre") or "").strip()
    effective_min = _effective_min_rating(opts, genre)
    roms_sys_dir  = roms_dir / system

    bypass = bool(opts.bypass_keywords and matches_any_keyword(game, opts.bypass_keywords))
    if not bypass and opts.exclude_keywords and matches_any_keyword(game, opts.exclude_keywords):
        return _skipped_detail_for(game, rom_filename, roms_sys_dir, rating), False, True

    if not bypass:
        if not should_include(rating, effective_min, opts.include_unrated, opts.copy_all):
            return _skipped_detail_for(game, rom_filename, roms_sys_dir, rating), False, True
        if (opts.genres is not None and genre not in opts.genres) or (
                opts.skip_genres is not None and genre in opts.skip_genres):
            return _skipped_detail_for(game, rom_filename, roms_sys_dir, rating), False, True

    src_rom = roms_sys_dir / rom_filename
    try:
        src_file_size = src_rom.stat().st_size
    except FileNotFoundError:
        return None, True, False

    # For m3u playlists, collect each referenced disc image.
    if rom_filename.suffix.lower() == '.m3u':
        m3u_discs = _collect_m3u_discs(src_rom, roms_sys_dir)
        if m3u_discs is None:
            return None, True, False
    else:
        m3u_discs = []

    entry = _build_game_entry(rom_filename, src_rom, src_file_size, m3u_discs, media_index)
    entry["game"] = game
    return entry, False, False


def _calc_target_sizes(
    entry: dict,
    target_roms_sys: Path,
    target_media_root: Path,
    system: str,
    target_media_index: dict,
) -> tuple[int, int]:
    """Calculate bytes still needed to copy for an entry (skip-existing logic).

    Returns (copy_rom_bytes, copy_media_bytes).
    """
    copy_rom_bytes = 0
    if not _size_matches(target_roms_sys / entry["rom_filename"], entry["src_file_size"]):
        copy_rom_bytes += entry["src_file_size"]
    for _, disc_rel, disc_size in entry["m3u_discs"]:
        if not _size_matches(target_roms_sys / disc_rel, disc_size):
            copy_rom_bytes += disc_size

    copy_media_bytes = 0
    for src_path, src_size in entry["media_entries"]:
        target_path = target_media_root / system / src_path.parent.name / src_path.name
        if target_media_index.get(target_path) != src_size:
            copy_media_bytes += src_size

    return copy_rom_bytes, copy_media_bytes


def _build_skip_ctx(
    system: str,
    opts: "PreviewOptions",
) -> tuple[Path | None, Path | None, dict | None, str]:
    """Return (target_roms_sys, target_media_root, target_media_index, system) when
    skip-existing is active, or (None, None, None, system) otherwise."""
    if opts.overwrite or opts.target_roms_dir is None or opts.target_esde_data_dir is None:
        return None, None, None, system
    trs  = opts.target_roms_dir / system
    tmr  = opts.target_esde_data_dir / "downloaded_media"
    return trs, tmr, build_target_media_index(system, tmr), system


def _count_skipped_files(roms_sys_dir: Path, games: list[dict]) -> int:
    """Count files in *roms_sys_dir* that are NOT part of any included entry."""
    included: set[Path] = {g["rom_filename"] for g in games}
    for g in games:
        for _, disc_rel, _ in g["m3u_discs"]:  # type: ignore[attr-defined]
            included.add(disc_rel)
    count = 0
    for dirpath, _, filenames in os.walk(roms_sys_dir):
        p = Path(dirpath)
        for name in filenames:
            if (p / name).relative_to(roms_sys_dir) not in included:
                count += 1
    return count


def _process_games(
    xml_root,
    roms_dir: Path,
    media_index: dict,
    opts: "PreviewOptions",
    skip_ctx: tuple,
) -> tuple[list[dict], int, list[dict]]:
    """Iterate over <game> elements; return (games, missing_count, skipped_details).

    skip_ctx is the 4-tuple from _build_skip_ctx; skip_ctx[3] carries the system name.
    """
    system = skip_ctx[3]
    games: list[dict] = []
    missing = 0
    skipped_details: list[dict] = []
    for game in xml_root.findall("game"):
        result, is_missing, is_skipped = _parse_game_entry(
            game, roms_dir, system, media_index, opts)
        if is_missing:
            missing += 1
            continue
        if is_skipped:
            assert isinstance(result, dict)
            skipped_details.append(result)
            continue
        if result is None:
            continue
        if skip_ctx[0] is not None:
            copy_rom_bytes, copy_media_bytes = _calc_target_sizes(
                result, skip_ctx[0], skip_ctx[1], system, skip_ctx[2])  # type: ignore[arg-type]
        else:
            copy_rom_bytes   = result["rom_bytes"]
            copy_media_bytes = result["media_bytes"]
        result["copy_rom_bytes"]   = copy_rom_bytes
        result["copy_media_bytes"] = copy_media_bytes
        del result["media_entries"]  # internal-only field
        games.append(result)
    return games, missing, skipped_details


def preview_system(
    system: str,
    gamelist_path: Path,
    roms_dir: Path,
    media_dir: Path,
    opts: "PreviewOptions",
) -> tuple[list[dict], int, int, list[dict]]:
    """Return (games, skipped_count, missing_count, skipped_details)."""
    root        = ET.parse(gamelist_path).getroot()
    media_index = build_media_index(system, media_dir)
    skip_ctx    = _build_skip_ctx(system, opts)
    games, missing, skipped_details = _process_games(
        root, roms_dir, media_index, opts, skip_ctx)
    skipped = _count_skipped_files(roms_dir / system, games)
    return games, skipped, missing, skipped_details


def _calc_prune_totals(
    do_prune: bool,
    skipped_details: list[dict],
    target_roms_dir: Path,
    system: str,
) -> tuple[int, int]:
    """Return (prune_count, prune_bytes) for files on target that would be deleted."""
    if not do_prune:
        return 0, 0
    count = 0
    total = 0
    for entry in skipped_details:
        rf = entry.get("rom_filename")
        if rf is None:
            continue
        try:
            total += (target_roms_dir / system / rf).stat().st_size
            count += 1
        except OSError:
            pass
    return count, total


def _print_system_preview(
    sp: "_SystemPreview",
    verbose: bool,
    skipped_details: list[dict],
) -> None:
    """Print the one-line summary for a single system during preview."""
    tag = " (copy all)" if sp.copy_all else (
        f" (rating: {sp.effective_min_rating * 10:g})"
        if sp.system in sp.system_ratings else ""
    )
    missing_str = f"  missing: {sp.missing}" if sp.missing else ""
    size_str = (f"{format_size(sp.sys_bytes)} / {format_size(sp.sys_source_bytes)}"
                if sp.skipped > 0 and sp.sys_source_bytes > 0 else format_size(sp.sys_bytes))
    copy_str = (
        f"  to copy: {format_size(sp.sys_copy_bytes)}"
        if sp.sys_copy_bytes < sp.sys_bytes else "")
    prune_str = (
        f"  to delete: {sp.sys_prune_count} ({format_size(sp.sys_prune_bytes)})"
        if sp.sys_prune_count else "")
    print(
        f"  [{sp.system}]{tag}  included: {len(sp.games)}"
        f"  skipped: {sp.skipped}{missing_str}  size: {size_str}{copy_str}{prune_str}"
    )
    if verbose:
        for g in sp.games:
            title = g["game"].findtext("name") or str(g["rom_filename"].stem)
            print(f"    + {title}")
        for s in sorted(skipped_details, key=lambda x: (x["rating"] is None, x["rating"] or 0)):
            rating_str = f"{s['rating'] * 10:.1f}" if s["rating"] is not None else "unrated"
            s_size_str = format_size(s["rom_size"]) if s["rom_size"] else "not on disk"
            print(f"    - {s['name']}  [{rating_str}]  {s_size_str}")


def _print_totals(
    totals: "_PreviewTotals",
    total_copy_bytes: int,
    do_prune: bool,
) -> None:
    """Print the aggregate totals block after the per-system preview lines."""
    source_str = (
        f" / {format_size(totals.total_source_bytes)}"
        if totals.total_skipped > 0 and totals.total_source_bytes > 0 else "")
    if total_copy_bytes < totals.total_bytes:
        savings = totals.total_bytes - total_copy_bytes
        print(
            f"Total: {totals.total_included} games"
            f"  ({format_size(totals.total_bytes)}{source_str};"
            f" {format_size(total_copy_bytes)} to copy,"
            f" {format_size(savings)} already on target)"
        )
    else:
        print(
            f"Total: {totals.total_included} games"
            f"  ({format_size(totals.total_bytes)}{source_str})")
    print(f"       {totals.total_skipped} games on disk, not included")
    if totals.total_missing:
        print(f"       {totals.total_missing} games skipped (ROM file not found on disk)")
    if do_prune and totals.total_prune_count:
        print(
            f"       {totals.total_prune_count} currently on target, would be deleted"
            f" ({format_size(totals.total_prune_bytes)} freed)"
        )


def _print_summary(
    total_included: int,
    total_bytes: int,
    total_copy_bytes: int,
    run_deleted_bytes: int,
) -> None:
    """Print the final 'Done.' line after all copies and prunes complete."""
    wrote_str   = f"{format_size(total_copy_bytes)} written"
    deleted_str = f"{format_size(run_deleted_bytes)} deleted" if run_deleted_bytes else ""
    net         = total_copy_bytes - run_deleted_bytes
    net_sign    = "+" if net >= 0 else "-"
    net_str     = f"net {net_sign}{format_size(abs(net))}" if run_deleted_bytes else ""
    run_detail  = "; ".join(s for s in [wrote_str, deleted_str, net_str] if s)
    if total_copy_bytes < total_bytes or run_deleted_bytes:
        print(f"Done. {total_included} games on target ({format_size(total_bytes)}; {run_detail}).")
    else:
        print(f"Done. {total_included} games copied ({format_size(total_bytes)}).")


def _print_run_header(args, resolved: dict, esde_data_dir: Path) -> None:
    """Print the run configuration summary to stdout."""
    print(f"ROMs dir:        {resolved['roms_dir']}")
    print(f"ES-DE data dir:  {esde_data_dir}")
    print(f"ROMs target:     {resolved['target_roms_dir']}")
    print(f"ES-DE target:    {resolved['target_esde_data_dir']}")
    print(f"Min rating:      {args.rating}/10")
    print(f"Include unrated: {args.include_unrated}")
    print(f"Mode:            {'overwrite' if args.overwrite else 'skip-existing'}")
    if resolved["copy_all_systems"]:
        print(f"Copy all:        {', '.join(sorted(resolved['copy_all_systems']))}")
    print()


def _dry_run_exit(args, totals: _PreviewTotals) -> None:
    """Print dry-run completion message and exit (returns normally, caller must return)."""
    if args.prune and totals.total_prune_count:
        print(
            f"Dry run complete. No files copied or deleted."
            f" ({totals.total_prune_count} would be deleted,"
            f" {format_size(totals.total_prune_bytes)} freed)"
        )
    else:
        print("Dry run complete. No files were copied.")


def _preview_one_system(  # pylint: disable=too-many-locals
    system: str,
    args,
    resolved: dict,
) -> tuple[list, list[dict], _SystemPreview] | None:
    """Preview a single system; return (games, skipped_details, SystemPreview) or None on error."""
    gamelist_path = resolved["gamelists_dir"] / system / "gamelist.xml"
    if not gamelist_path.exists():
        return None

    copy_all             = system in resolved["copy_all_systems"]
    effective_min_rating = resolved["system_ratings"].get(system, resolved["min_rating"])
    opts = PreviewOptions(
        min_rating=effective_min_rating,
        include_unrated=args.include_unrated,
        copy_all=copy_all,
        overwrite=args.overwrite,
        genres=resolved["genres_include"],
        skip_genres=resolved["genres_skip"],
        genre_ratings=resolved["genre_ratings"] or None,
        target_roms_dir=resolved["target_roms_dir"],
        target_esde_data_dir=resolved["target_esde_data_dir"],
        bypass_keywords=resolved.get("bypass_keywords"),
        exclude_keywords=resolved.get("exclude_keywords"),
    )
    try:
        games, skipped, missing, skipped_details = preview_system(
            system, gamelist_path, resolved["roms_dir"], resolved["media_dir"], opts,
        )
    except ET.ParseError as e:
        print(
            f"  [{system}]  WARNING: skipping — gamelist.xml is malformed ({e})",
            file=sys.stderr,
        )
        return None

    sys_copy_rom_bytes  = sum(g["copy_rom_bytes"]   for g in games)
    sys_copy_esde_bytes = sum(g["copy_media_bytes"] for g in games)
    sys_prune_count, sys_prune_bytes = _calc_prune_totals(
        args.prune, skipped_details, resolved["target_roms_dir"], system)
    sp = _SystemPreview(
        system=system,
        copy_all=copy_all,
        effective_min_rating=effective_min_rating,
        system_ratings=resolved["system_ratings"],
        games=games,
        skipped=skipped,
        missing=missing,
        sys_bytes=sum(g["bytes"] for g in games),
        sys_source_bytes=(
            _dir_size(resolved["roms_dir"] / system)
            + _dir_size(resolved["media_dir"] / system)
        ),
        sys_copy_bytes=sys_copy_rom_bytes + sys_copy_esde_bytes,
        sys_prune_count=sys_prune_count,
        sys_prune_bytes=sys_prune_bytes,
    )
    return games, skipped_details, sp


def _run_preview(
    systems: list[str],
    args,
    resolved: dict,
) -> tuple[dict, dict, _PreviewTotals]:
    """Run the preview loop over all systems; return (plan, plan_skipped, totals)."""
    plan: dict                          = {}
    plan_skipped: dict[str, list[dict]] = {}
    totals = _PreviewTotals()

    for system in systems:
        result = _preview_one_system(system, args, resolved)
        if result is None:
            continue
        games, skipped_details, sp = result
        _print_system_preview(sp, args.verbose, skipped_details)

        plan[system]         = games
        plan_skipped[system] = skipped_details
        totals.total_included        += len(games)
        totals.total_skipped         += sp.skipped
        totals.total_missing         += sp.missing
        totals.total_bytes           += sp.sys_bytes
        totals.total_source_bytes    += sp.sys_source_bytes
        totals.total_copy_rom_bytes  += sum(g["copy_rom_bytes"]   for g in games)
        totals.total_copy_esde_bytes += sum(g["copy_media_bytes"] for g in games)
        if args.prune:
            totals.total_prune_count += sp.sys_prune_count
            totals.total_prune_bytes += sp.sys_prune_bytes

    return plan, plan_skipped, totals

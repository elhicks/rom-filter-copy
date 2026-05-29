import logging
import os
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from _filters import format_size
from _media import parse_m3u


def _wsl_to_windows(path_str: str) -> str:
    """Convert /mnt/X/... to X:\\... when running under Windows Python."""
    if sys.platform != 'win32' or not path_str.startswith('/mnt/'):
        return path_str
    parts = path_str.split('/', 3)  # ['', 'mnt', 'X', 'rest...']
    if len(parts) < 3 or len(parts[2]) != 1 or not parts[2].isalpha():
        return path_str
    drive = parts[2].upper()
    rest = parts[3].replace('/', '\\') if len(parts) == 4 else ''
    return f"{drive}:\\{rest}"


def _copy2_retry(src: Path, dst: Path, retries: int = 5, delay: float = 0.5) -> None:
    for attempt in range(retries):
        try:
            if sys.platform == 'win32':
                # CopyFile2 (used by shutil.copy2) fails on WSL/9P filesystems;
                # use a plain read/write copy instead.
                try:
                    with open(src, 'rb') as fsrc, open(dst, 'wb') as fdst:
                        shutil.copyfileobj(fsrc, fdst)
                except BaseException:
                    try:
                        dst.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise
                try:
                    shutil.copystat(src, dst)
                except OSError:
                    pass
            else:
                try:
                    shutil.copy2(src, dst)
                except BaseException:
                    try:
                        dst.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise
            return
        except PermissionError:
            if attempt == retries - 1:
                raise
            time.sleep(delay)


def _size_matches(dst: Path, expected_size: int) -> bool:
    try:
        return dst.stat().st_size == expected_size
    except OSError:
        return False


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for entry in os.scandir(path):
            if entry.is_file(follow_symlinks=False):
                total += entry.stat().st_size
            elif entry.is_dir(follow_symlinks=False):
                total += _dir_size(Path(entry.path))
    except (FileNotFoundError, PermissionError):
        pass
    return total


def _free_space(path: Path) -> int:
    # Targets may not exist yet (first run on a freshly-formatted card); probe
    # the nearest existing ancestor so we still get a real disk-usage reading.
    check_at = path
    while not check_at.exists():
        if check_at.parent == check_at:
            sys.exit(f"ERROR: target path's parent does not exist: {path}")
        check_at = check_at.parent
    return shutil.disk_usage(check_at).free


def _copy_rom(entry: dict, target_roms: Path, overwrite: bool, _try_copy) -> None:
    """Copy the primary ROM file for an entry to target_roms."""
    src_rom = entry["src_rom"]
    if src_rom.exists():
        dst = target_roms / entry["rom_filename"]
        if overwrite or not _size_matches(dst, entry["src_file_size"]):
            dst.parent.mkdir(parents=True, exist_ok=True)
            _try_copy(src_rom, dst, f"ROM {src_rom.name}")


def _copy_discs(entry: dict, target_roms: Path, overwrite: bool, _try_copy) -> None:
    """Copy all disc files listed in an m3u entry to target_roms."""
    for disc_abs, disc_rel, disc_size in entry.get("m3u_discs", []):
        if disc_abs.exists():
            dst = target_roms / disc_rel
            if overwrite or not _size_matches(dst, disc_size):
                dst.parent.mkdir(parents=True, exist_ok=True)
                _try_copy(disc_abs, dst, f"disc {disc_abs.name}")


def _copy_media(entry: dict, target_media: Path, system: str, overwrite: bool, _try_copy) -> None:
    """Copy all media files for an entry to target_media."""
    for f in entry["media_files"]:
        dst = target_media / system / f.parent.name / f.name
        if overwrite or not _size_matches(dst, f.stat().st_size):
            dst.parent.mkdir(parents=True, exist_ok=True)
            _try_copy(f, dst, f"media {f.name}")


def _write_gamelist(games: list[dict], target_gl_dir: Path) -> None:
    """Write a gamelist.xml from the given game entries to target_gl_dir."""
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


def copy_system(system: str, games: list[dict],
                target_roms_dir: Path, target_esde_data_dir: Path,
                *, overwrite: bool = True) -> None:
    target_roms   = target_roms_dir / system
    target_media  = target_esde_data_dir / "downloaded_media"
    target_gl_dir = target_esde_data_dir / "gamelists" / system

    errors: list[str] = []

    def _try_copy(src: Path, dst: Path, label: str) -> None:
        try:
            _copy2_retry(src, dst)
        except OSError as e:
            msg = f"[{system}] {label}: {e}\n  src: {src}\n  dst: {dst}"
            logging.error(msg)
            errors.append(f"  {label}: {e}")

    total = len(games)
    for idx, entry in enumerate(games, start=1):
        title = entry["game"].findtext("name") or str(entry["rom_filename"].stem)
        print(f"  [{idx}/{total}] {title}")
        _copy_rom(entry, target_roms, overwrite, _try_copy)
        _copy_discs(entry, target_roms, overwrite, _try_copy)
        _copy_media(entry, target_media, system, overwrite, _try_copy)

    if errors:
        print(f"  WARNING: {len(errors)} file(s) could not be copied:")
        for msg in errors:
            print(msg)

    # gamelist.xml is the canonical metadata index — always rewrite so changes
    # to ratings/desc/etc. propagate even when no media bytes change.
    _write_gamelist(games, target_gl_dir)


def _delete_game_files(entry: dict, target_roms_sys: Path,
                       target_media_sys: Path, _try_del) -> bool:
    """Delete ROM, disc, and media files for a single pruned game entry.
    Returns False if rom_filename is missing (entry should be skipped)."""
    rom_filename: Path | None = entry.get("rom_filename")
    if rom_filename is None:
        return False

    dst_rom = target_roms_sys / rom_filename
    if dst_rom.exists():
        if dst_rom.suffix.lower() == '.m3u':
            for disc_path in parse_m3u(dst_rom):
                if disc_path.exists():
                    _try_del(disc_path)
        _try_del(dst_rom)

    rom_stem = rom_filename.stem
    try:
        type_dirs = list(os.scandir(target_media_sys))
    except OSError:
        return True
    for type_dir in type_dirs:
        if not type_dir.is_dir():
            continue
        try:
            with os.scandir(type_dir.path) as files:
                for f in files:
                    if f.is_file() and Path(f.path).stem == rom_stem:
                        _try_del(Path(f.path))
        except OSError:
            pass
    return True


def delete_pruned(system: str, pruned: list[dict],
                  target_roms_dir: Path, target_esde_data_dir: Path) -> tuple[int, int]:
    """Delete from target the ROM and media for each pruned (filtered-out) game.
    Only deletes files that appear in the source gamelist — never touches files
    that weren't put there by this tool. Returns (files_deleted, bytes_freed)."""
    target_roms_sys  = target_roms_dir / system
    target_media_sys = target_esde_data_dir / "downloaded_media" / system

    deleted = 0
    freed = 0
    errors: list[str] = []

    def _try_del(path: Path) -> None:
        nonlocal deleted, freed
        try:
            freed += path.stat().st_size
            path.unlink()
            deleted += 1
        except OSError as e:
            logging.error("[%s] delete %s: %s", system, path, e)
            errors.append(str(e))

    for entry in pruned:
        _delete_game_files(entry, target_roms_sys, target_media_sys, _try_del)

    if errors:
        print(f"  WARNING: {len(errors)} file(s) could not be deleted")

    return deleted, freed


@dataclass
class _CopyParams:
    """Parameters for _execute_copy, bundled to stay under the arg-count limit."""
    target_roms_dir: Path
    target_esde_data_dir: Path
    overwrite: bool
    do_prune: bool


def _check_capacity(
    target_roms_dir: Path,
    target_esde_data_dir: Path,
    total_copy_rom_bytes: int,
    total_copy_esde_bytes: int,
) -> None:
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


def _execute_copy(plan: dict, plan_skipped: dict, cp: _CopyParams) -> int:
    systems_to_copy   = [(s, g) for s, g in plan.items() if g]
    run_deleted_bytes = 0
    try:
        for idx, (system, games) in enumerate(systems_to_copy, start=1):
            print(f"Copying [{system}] ({idx}/{len(systems_to_copy)} systems)...")
            copy_system(
                system, games, cp.target_roms_dir, cp.target_esde_data_dir,
                overwrite=cp.overwrite,
            )
            if cp.do_prune:
                pruned = plan_skipped.get(system, [])
                if pruned:
                    d, freed = delete_pruned(
                        system, pruned, cp.target_roms_dir, cp.target_esde_data_dir)
                    if d:
                        run_deleted_bytes += freed
                        print(f"  Pruned: {d} file(s) deleted ({format_size(freed)} freed)")
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
    return run_deleted_bytes

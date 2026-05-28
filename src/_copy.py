import logging
import os
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path


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

        src_rom = entry["src_rom"]
        if src_rom.exists():
            dst = target_roms / entry["rom_filename"]
            if overwrite or not _size_matches(dst, entry["src_file_size"]):
                dst.parent.mkdir(parents=True, exist_ok=True)
                _try_copy(src_rom, dst, f"ROM {src_rom.name}")

        for disc_abs, disc_rel, disc_size in entry.get("m3u_discs", []):
            if disc_abs.exists():
                dst = target_roms / disc_rel
                if overwrite or not _size_matches(dst, disc_size):
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    _try_copy(disc_abs, dst, f"disc {disc_abs.name}")

        for f in entry["media_files"]:
            dst = target_media / system / f.parent.name / f.name
            if overwrite or not _size_matches(dst, f.stat().st_size):
                dst.parent.mkdir(parents=True, exist_ok=True)
                _try_copy(f, dst, f"media {f.name}")

    if errors:
        print(f"  WARNING: {len(errors)} file(s) could not be copied:")
        for msg in errors:
            print(msg)

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

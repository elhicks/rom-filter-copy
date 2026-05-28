import os
from pathlib import Path


def parse_m3u(m3u_path: Path) -> list[Path]:
    """Return paths to disc images listed in an m3u file (resolved relative to its directory)."""
    discs = []
    try:
        with open(m3u_path, encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    discs.append(m3u_path.parent / line)
    except OSError:
        pass
    return discs


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

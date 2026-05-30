import re
from pathlib import Path


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


def matches_any_keyword(game_elem, keywords: set[str]) -> bool:
    """Return True if any keyword matches as a whole word/phrase in any child element's text.

    A match requires the keyword to be bounded by whitespace, punctuation, or string
    edges on both sides — it must not adjoin other alphanumeric characters.
    """
    if not keywords:
        return False
    patterns = [re.compile(r'\b' + re.escape(kw) + r'\b', re.IGNORECASE) for kw in keywords]
    for child in game_elem:
        text = child.text or ""
        if any(p.search(text) for p in patterns):
            return True
    return False


def should_include(rating: float | None, min_rating: float,
                   include_unrated: bool, copy_all: bool) -> bool:
    if copy_all:
        return True
    if rating is None:
        return include_unrated
    return rating >= min_rating


def expand_raw_genres(canonical: set[str],
                      genre_map: dict[str, list[str]]) -> set[str]:
    """Expand canonical genre names to their raw ES-DE strings via genre_map.
    Any name not found in the map is kept as-is (allows raw strings from CLI)."""
    raw: set[str] = set()
    for name in canonical:
        if name in genre_map:
            raw.update(genre_map[name])
        else:
            raw.add(name)
    return raw


def expand_raw_genre_ratings(ratings: dict[str, float],
                              genre_map: dict[str, list[str]]) -> dict[str, float]:
    """Expand canonical genre keys in a ratings dict to raw ES-DE strings.
    When multiple canonicals expand to the same raw string, the stricter
    (higher) rating wins."""
    expanded: dict[str, float] = {}
    for key, rating in ratings.items():
        raw_strings = genre_map.get(key, [key])
        for raw in raw_strings:
            expanded[raw] = max(expanded.get(raw, rating), rating)
    return expanded

import argparse
import logging
import sys
import tomllib
from pathlib import Path

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


def _load_config(config_path: Path, config_explicit: bool) -> dict:
    """Load and merge config files; return the effective config dict."""
    if config_explicit and not config_path.exists():
        sys.exit(f"ERROR: --config file not found: {config_path}")
    raw_config = load_config(config_path)
    # Use DEFAULT_CONFIG as a base so that tables defined there (e.g. genre_map)
    # are available even when config.local.toml was created before they existed.
    # Nested tables are merged; local keys win on collision.
    if DEFAULT_CONFIG.exists() and config_path.resolve() != DEFAULT_CONFIG.resolve():
        return _merge_configs(load_config(DEFAULT_CONFIG), raw_config)
    return raw_config


def _preparse_config() -> dict:
    """Pre-parse argv to locate the config file, then load and return the config dict."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=str(CONFIG_FILE))
    pre_args, _ = pre.parse_known_args()
    config_explicit = pre_args.config != str(CONFIG_FILE)
    return _load_config(Path(pre_args.config), config_explicit)


def _setup_logging() -> None:
    """Configure file-based error logging."""
    log_path = PROJECT_ROOT / "rom_filter_copy.log"
    logging.basicConfig(
        filename=log_path,
        level=logging.ERROR,
        format="%(asctime)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        encoding="utf-8",
    )

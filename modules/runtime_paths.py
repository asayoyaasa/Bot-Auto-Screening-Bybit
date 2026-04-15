from __future__ import annotations

import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPO_ROOT / 'config.json'
CONFIG_EXAMPLE_PATH = REPO_ROOT / 'config.example.json'


def get_config_path() -> Path:
    return Path(os.getenv('BYBIT_BOT_CONFIG_PATH', DEFAULT_CONFIG_PATH)).expanduser().resolve()

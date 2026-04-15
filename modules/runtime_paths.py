from __future__ import annotations

import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPO_ROOT / 'config.json'
CONFIG_EXAMPLE_PATH = REPO_ROOT / 'config.example.json'


def get_config_path() -> Path:
    override = os.getenv('BYBIT_BOT_CONFIG_PATH')
    if override:
        return Path(override).expanduser().resolve()

    cwd_config = Path.cwd() / 'config.json'
    if cwd_config.exists():
        return cwd_config.resolve()

    return DEFAULT_CONFIG_PATH.resolve()

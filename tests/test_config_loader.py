import importlib
import json
import sys
from types import SimpleNamespace

import pytest


BASE_CONFIG = {
    "database": {
        "host": "localhost",
        "database": "bybit_bot",
        "user": "bot",
        "password": "secret",
        "port": 5432,
    },
    "system": {
        "timezone": "UTC",
        "max_threads": 2,
        "check_interval_hours": 1,
        "timeframes": ["15m", "1h"],
    },
    "setup": {
        "fib_entry_start": 0.2,
        "fib_entry_end": 0.4,
        "fib_sl": 0.15,
    },
    "strategy": {
        "min_tech_score": 3,
        "risk_reward_min": 2.0,
    },
    "indicators": {
        "min_rvol": 1.5,
    },
    "patterns": {
        "ascending_triangle": True,
    },
    "pattern_signals": {
        "ascending_triangle": "Long",
    },
    "notifications": {
        "telegram_enabled": True,
        "discord_enabled": False,
    },
    "execution": {
        "mode": "paper",
    },
}


def _install_dotenv_stub(monkeypatch):
    monkeypatch.setitem(sys.modules, "dotenv", SimpleNamespace(load_dotenv=lambda *a, **k: None))


def _import_config_loader(tmp_path, monkeypatch, config):
    config_path = tmp_path / "custom-config.json"
    config_path.write_text(json.dumps(config))
    monkeypatch.setenv("BYBIT_BOT_CONFIG_PATH", str(config_path))
    monkeypatch.chdir(tmp_path)
    _install_dotenv_stub(monkeypatch)
    sys.modules.pop("modules.config_loader", None)
    sys.modules.pop("modules.runtime_paths", None)
    module = importlib.import_module("modules.config_loader")
    return module


def test_validate_config_accepts_minimal_paper_config(tmp_path, monkeypatch):
    module = _import_config_loader(tmp_path, monkeypatch, BASE_CONFIG)

    validated = module.validate_config(BASE_CONFIG)

    assert validated["execution"]["mode"] == "paper"
    assert validated["system"]["timeframes"] == ["15m", "1h"]


def test_validate_config_rejects_live_mode_without_exchange_keys(tmp_path, monkeypatch):
    live_config = json.loads(json.dumps(BASE_CONFIG))
    live_config["execution"]["mode"] = "live"
    module = _import_config_loader(tmp_path, monkeypatch, live_config)

    with pytest.raises(ValueError, match="Live mode requires api.bybit_key"):
        module.validate_config(live_config)


def test_validate_config_rejects_blank_live_mode_env_keys(tmp_path, monkeypatch):
    live_config = json.loads(json.dumps(BASE_CONFIG))
    live_config["execution"]["mode"] = "live"
    _install_dotenv_stub(monkeypatch)
    monkeypatch.setenv("BYBIT_KEY", "   ")
    monkeypatch.setenv("BYBIT_SECRET", "")

    module = _import_config_loader(tmp_path, monkeypatch, live_config)

    with pytest.raises(ValueError, match="Live mode requires BYBIT_KEY and BYBIT_SECRET"):
        module.load_config()


def test_load_config_applies_env_overrides_and_testing_database(tmp_path, monkeypatch):
    config = json.loads(json.dumps(BASE_CONFIG))
    _install_dotenv_stub(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps(config))
    monkeypatch.setenv("BYBIT_KEY", "env-key")
    monkeypatch.setenv("BYBIT_SECRET", "env-secret")
    monkeypatch.setenv("TELEGRAM_TOKEN", "tg-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456")
    monkeypatch.setenv("DISCORD_WEBHOOK", "https://discord.example/webhook")
    monkeypatch.setenv("BOT_ENV", "testing")

    sys.modules.pop("modules.config_loader", None)
    reloaded = importlib.import_module("modules.config_loader")

    assert reloaded.CONFIG["api"]["bybit_key"] == "env-key"
    assert reloaded.CONFIG["api"]["bybit_secret"] == "env-secret"
    assert reloaded.CONFIG["api"]["telegram_bot_token"] == "tg-token"
    assert reloaded.CONFIG["api"]["telegram_chat_id"] == "123456"
    assert reloaded.CONFIG["api"]["discord_webhook"] == "https://discord.example/webhook"
    assert reloaded.CONFIG["database"]["database"] == "bybit_bot_test"

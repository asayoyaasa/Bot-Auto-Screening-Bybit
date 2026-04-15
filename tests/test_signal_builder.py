import json
import sys
import types

import pytest

BASE_CONFIG = {
    "database": {
        "host": "localhost",
        "database": "bybit_bot",
        "user": "bot",
        "password": "***",
        "port": 5432,
    },
    "system": {
        "timezone": "UTC",
        "max_threads": 2,
        "check_interval_hours": 1,
        "timeframes": ["15m", "1h"],
        "min_candles_analysis": 150,
    },
    "setup": {
        "fib_entry_start": 0.2,
        "fib_entry_end": 0.4,
        "fib_sl": 0.15,
    },
    "strategy": {
        "min_tech_score": 3,
        "risk_reward_min": 2.0,
        "min_smc_score": 0,
        "min_deriv_score": 0,
        "require_valid_smc": False,
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


@pytest.fixture()
def signal_builder_module(tmp_path, monkeypatch):
    config_path = tmp_path / "custom-config.json"
    config_path.write_text(json.dumps(BASE_CONFIG))
    monkeypatch.setenv("BYBIT_BOT_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("BOT_ENV", "testing")

    fake_modules = {
        "ccxt": types.SimpleNamespace(bybit=lambda *a, **k: object()),
        "schedule": types.SimpleNamespace(every=lambda *a, **k: types.SimpleNamespace(hours=types.SimpleNamespace(do=lambda *a, **k: None), minutes=types.SimpleNamespace(do=lambda *a, **k: None), do=lambda *a, **k: None), run_pending=lambda: None),
        "pandas": types.SimpleNamespace(DataFrame=lambda *a, **k: types.SimpleNamespace(iloc=types.SimpleNamespace(__getitem__=lambda self, idx: types.SimpleNamespace(**{"ema13": 0, "ema21": 0}))), to_datetime=lambda *a, **k: None),
        "pandas_ta_classic": types.SimpleNamespace(ema=lambda series, length=None: series),
        "numpy": types.SimpleNamespace(),
        "scipy": types.SimpleNamespace(),
        "scipy.stats": types.SimpleNamespace(linregress=lambda *a, **k: (0, 0, 0, 0, 0)),
        "scipy.signal": types.SimpleNamespace(argrelextrema=lambda *a, **k: ([],)),
        "scipy.special": types.SimpleNamespace(expit=lambda x: x),
        "pybit": types.SimpleNamespace(unified_trading=types.SimpleNamespace(HTTP=object, WebSocket=object)),
        "pybit.unified_trading": types.SimpleNamespace(HTTP=object, WebSocket=object),
        "pythonjsonlogger": types.SimpleNamespace(jsonlogger=types.SimpleNamespace(JsonFormatter=object)),
        "dotenv": types.SimpleNamespace(load_dotenv=lambda *a, **k: None),
    }
    for name, mod in fake_modules.items():
        monkeypatch.setitem(sys.modules, name, mod)

    sys.modules.pop("modules.config_loader", None)
    sys.modules.pop("scanner.signal_builder", None)
    import scanner.signal_builder as sb
    return sb


def _stub_analysis_flow(sb, monkeypatch, *, pattern="ascending_triangle", side="Long", smc_score=5, valid_smc=True, deriv_score=4, valid_deriv=True, tech_div=1, fakeout=True):
    monkeypatch.setattr(sb, "get_technicals", lambda frame: frame)
    monkeypatch.setattr(sb, "find_pattern", lambda frame: pattern)
    monkeypatch.setattr(sb, "analyze_smc", lambda frame, detected_side: (valid_smc, smc_score, ["smc ok" if valid_smc else "smc fail"]))
    monkeypatch.setattr(sb, "calculate_metrics", lambda frame, ticker: (frame, 1.0, 0.5, 0.2, 0.1, 4, ["quant ok"]))
    monkeypatch.setattr(sb, "analyze_derivatives", lambda frame, ticker, detected_side: (valid_deriv, deriv_score, ["deriv ok" if valid_deriv else "deriv fail"]))
    monkeypatch.setattr(sb, "detect_divergence", lambda frame: (tech_div, "bull div" if tech_div > 0 else "bear div"))
    monkeypatch.setattr(sb, "check_fakeout", lambda frame, min_rvol: (fakeout, None))


def _sample_bars():
    return [[i, 0, 100 + i, 90, 95, 1] for i in range(200)]


def test_symbol_filter_rejects_st_symbol(signal_builder_module):
    sb = signal_builder_module
    calls = []

    def ex_call(method, *args, **kwargs):
        calls.append((method, args, kwargs))
        if method == "fetch_ticker":
            return {"info": {"symbol": "ABCSTUSDT", "fundingRate": 0.0}}
        raise AssertionError("fetch_ohlcv should not be called")

    assert sb.analyze_ticker(ex_call, "ABC/USDT", "15m", "Sideways", set()) is None
    assert [c[0] for c in calls] == ["fetch_ticker"]


def test_symbol_filter_rejects_active_signal(signal_builder_module):
    sb = signal_builder_module

    def ex_call(method, *args, **kwargs):
        raise AssertionError("exchange calls should not happen for active signals")

    assert sb.analyze_ticker(ex_call, "BTC/USDT", "15m", "Sideways", {("BTC/USDT", "15m")}) is None


def test_btc_bias_gates_long_and_short(signal_builder_module, monkeypatch):
    sb = signal_builder_module
    _stub_analysis_flow(sb, monkeypatch)

    def ex_call(method, *args, **kwargs):
        if method == "fetch_ticker":
            return {"info": {"symbol": "BTCUSDT", "fundingRate": 0.0}}
        return _sample_bars()

    assert sb.analyze_ticker(ex_call, "ETH/USDT", "15m", "Bearish", set()) is None
    assert sb.analyze_ticker(ex_call, "ETH/USDT", "15m", "Bullish", set()) is None


def test_major_rejection_paths_return_none(signal_builder_module, monkeypatch):
    sb = signal_builder_module
    _stub_analysis_flow(sb, monkeypatch, smc_score=-1, valid_smc=False)

    def ex_call(method, *args, **kwargs):
        if method == "fetch_ticker":
            return {"info": {"symbol": "BTCUSDT", "fundingRate": 0.0}}
        return _sample_bars()

    sb.CONFIG["strategy"]["min_smc_score"] = 10
    assert sb.analyze_ticker(ex_call, "ETH/USDT", "15m", "Sideways", set()) is None


def test_risk_reward_rejection(signal_builder_module, monkeypatch):
    sb = signal_builder_module
    _stub_analysis_flow(sb, monkeypatch, tech_div=1)
    sb.CONFIG["strategy"]["min_smc_score"] = 0
    sb.CONFIG["strategy"]["risk_reward_min"] = 999

    def ex_call(method, *args, **kwargs):
        if method == "fetch_ticker":
            return {"info": {"symbol": "BTCUSDT", "fundingRate": 0.0}}
        return _sample_bars()

    assert sb.analyze_ticker(ex_call, "ETH/USDT", "15m", "Sideways", set()) is None


def test_get_btc_bias_sideways_on_missing_data(signal_builder_module):
    sb = signal_builder_module
    assert sb.get_btc_bias(lambda *args, **kwargs: []) == "Sideways"

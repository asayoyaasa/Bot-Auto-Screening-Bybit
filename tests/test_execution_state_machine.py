import importlib
import json
import sys
import types


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


class FakeCursor:
    def __init__(self):
        self.statements = []
        self.fetchone_result = None
        self.fetchall_result = []

    def execute(self, query, params=None):
        self.statements.append((query, params))

    def fetchone(self):
        return self.fetchone_result

    def fetchall(self):
        return self.fetchall_result


class FakeConn:
    def __init__(self):
        self.cursor_obj = FakeCursor()
        self.committed = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed += 1


class DummyPool:
    def __init__(self, *args, **kwargs):
        self.conn = FakeConn()

    def getconn(self):
        return self.conn

    def putconn(self, conn):
        pass


def _install_import_stubs(monkeypatch):
    fake_pool_mod = types.SimpleNamespace(ThreadedConnectionPool=DummyPool)
    fake_extras = types.SimpleNamespace(RealDictCursor=object)
    fake_psycopg2 = types.SimpleNamespace(pool=fake_pool_mod, extras=fake_extras)
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)
    monkeypatch.setitem(sys.modules, "psycopg2.pool", fake_pool_mod)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", fake_extras)
    monkeypatch.setitem(sys.modules, "ccxt", types.SimpleNamespace(bybit=lambda *a, **k: object()))
    monkeypatch.setitem(sys.modules, "schedule", types.SimpleNamespace(every=lambda *a, **k: types.SimpleNamespace(hours=types.SimpleNamespace(do=lambda *a, **k: None), minutes=types.SimpleNamespace(do=lambda *a, **k: None), day=types.SimpleNamespace(at=lambda *a, **k: types.SimpleNamespace(do=lambda *a, **k: None)), do=lambda *a, **k: None), run_pending=lambda: None))
    monkeypatch.setitem(sys.modules, "pandas", types.SimpleNamespace(DataFrame=lambda *a, **k: None, to_datetime=lambda *a, **k: None))
    monkeypatch.setitem(sys.modules, "pandas_ta", types.SimpleNamespace(ema=lambda *a, **k: None))
    monkeypatch.setitem(sys.modules, "numpy", types.SimpleNamespace())
    monkeypatch.setitem(sys.modules, "scipy", types.SimpleNamespace())
    monkeypatch.setitem(sys.modules, "scipy.stats", types.SimpleNamespace(linregress=lambda *a, **k: (0, 0, 0, 0, 0)))
    monkeypatch.setitem(sys.modules, "scipy.signal", types.SimpleNamespace(argrelextrema=lambda *a, **k: ([],)))
    pybit_unified = types.SimpleNamespace(HTTP=object, WebSocket=object)
    monkeypatch.setitem(sys.modules, "pybit", types.SimpleNamespace(unified_trading=pybit_unified))
    monkeypatch.setitem(sys.modules, "pybit.unified_trading", pybit_unified)
    monkeypatch.setitem(sys.modules, "pythonjsonlogger", types.SimpleNamespace(jsonlogger=types.SimpleNamespace(JsonFormatter=object)))
    monkeypatch.setitem(sys.modules, "pytz", types.SimpleNamespace(timezone=lambda tz: None))
    monkeypatch.setitem(sys.modules, "mplfinance", types.SimpleNamespace(make_marketcolors=lambda *a, **k: None, make_mpf_style=lambda *a, **k: None, make_addplot=lambda *a, **k: None, plot=lambda *a, **k: None))
    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=lambda *a, **k: None, get=lambda *a, **k: None))


def _install_dotenv_stub(monkeypatch):
    monkeypatch.setitem(sys.modules, "dotenv", types.SimpleNamespace(load_dotenv=lambda *a, **k: None))


def _import_auto_trades(tmp_path, monkeypatch):
    config_path = tmp_path / "custom-config.json"
    config_path.write_text(json.dumps(BASE_CONFIG))
    monkeypatch.setenv("BYBIT_BOT_CONFIG_PATH", str(config_path))
    monkeypatch.chdir(tmp_path)
    _install_dotenv_stub(monkeypatch)
    _install_import_stubs(monkeypatch)
    sys.modules.pop("modules.config_loader", None)
    sys.modules.pop("modules.runtime_paths", None)
    sys.modules.pop("modules.database", None)
    sys.modules.pop("auto_trades", None)
    return importlib.import_module("auto_trades")


def test_update_active_trade_mode_preserves_order_id(tmp_path, monkeypatch):
    mod = _import_auto_trades(tmp_path, monkeypatch)
    cur = FakeCursor()

    mod.update_active_trade_mode(cur, 42)

    assert cur.statements == [("UPDATE active_trades SET execution_mode = %s WHERE id = %s", (mod.EXECUTION_MODE, 42))]


def test_take_partial_profit_marks_tp_and_updates_close_state(tmp_path, monkeypatch):
    mod = _import_auto_trades(tmp_path, monkeypatch)
    cur = FakeCursor()
    trade = (
        1, 10, "BTC/USDT", "Long", 100.0, 95.0, 110.0, 120.0, 130.0,
        3.0, 10, "OPEN_TPS_SET", 0.0, False,
        "paper", 3.0, 0.0,
        100.0, None,
        0.0, 0.0, 0.0,
        False, False, False,
    )

    mod.take_partial_profit(cur, trade, 1, 110.0)

    update_queries = [q for q, _ in cur.statements if "UPDATE active_trades" in q]
    assert update_queries
    assert any("tp1_hit = TRUE" in q for q in update_queries)
    assert any("is_sl_moved = TRUE" in q for q, _ in cur.statements)

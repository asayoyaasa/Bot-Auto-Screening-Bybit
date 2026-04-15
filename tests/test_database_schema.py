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
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class FakePool:
    def __init__(self, *args, **kwargs):
        self.conn = FakeConn()

    def getconn(self):
        return self.conn

    def putconn(self, conn):
        self.conn = conn


def _install_fake_psycopg2(monkeypatch):
    fake_pool_mod = types.SimpleNamespace(ThreadedConnectionPool=FakePool)
    fake_extras = types.SimpleNamespace(RealDictCursor=object)
    fake_psycopg2 = types.SimpleNamespace(pool=fake_pool_mod, extras=fake_extras)
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)
    monkeypatch.setitem(sys.modules, "psycopg2.pool", fake_pool_mod)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", fake_extras)


def _install_dotenv_stub(monkeypatch):
    monkeypatch.setitem(sys.modules, "dotenv", types.SimpleNamespace(load_dotenv=lambda *a, **k: None))


def _import_database(tmp_path, monkeypatch):
    config_path = tmp_path / "custom-config.json"
    config_path.write_text(json.dumps(BASE_CONFIG))
    monkeypatch.setenv("BYBIT_BOT_CONFIG_PATH", str(config_path))
    monkeypatch.chdir(tmp_path)
    _install_dotenv_stub(monkeypatch)
    _install_fake_psycopg2(monkeypatch)
    sys.modules.pop("modules.config_loader", None)
    sys.modules.pop("modules.runtime_paths", None)
    sys.modules.pop("modules.database", None)
    return importlib.import_module("modules.database")


def test_migrate_schema_creates_expected_tables_and_indexes(tmp_path, monkeypatch):
    db = _import_database(tmp_path, monkeypatch)
    conn = FakeConn()

    db.migrate_schema(conn)

    queries = [q for q, _ in conn.cursor_obj.statements]
    assert any("CREATE TABLE trades" in q for q in queries)
    assert any("CREATE TABLE IF NOT EXISTS bot_state" in q for q in queries)
    assert any("CREATE INDEX IF NOT EXISTS idx_trades_status" in q for q in queries)
    assert any("CREATE INDEX IF NOT EXISTS idx_trades_symbol_timeframe" in q for q in queries)
    assert any("CREATE INDEX IF NOT EXISTS idx_trades_created_at" in q for q in queries)
    assert conn.committed is True


def test_init_db_uses_threaded_pool_and_runs_migration(tmp_path, monkeypatch):
    db = _import_database(tmp_path, monkeypatch)

    db.init_db()

    assert db.DB_POOL is not None
    assert db.DB_POOL.conn.committed is True

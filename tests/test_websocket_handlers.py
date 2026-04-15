import importlib
import json
import sys
import types


BASE_CONFIG = {
    "database": {
        "host": "localhost",
        "database": "bybit_bot",
        "user": "bot",
        "password": "***",
        "port": 5432,
    },
    "api": {
        "bybit_key": "key",
        "bybit_secret": "secret",
    },
    "system": {
        "timezone": "UTC",
        "max_threads": 2,
        "check_interval_hours": 1,
        "timeframes": ["15m"],
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
        "mode": "live",
    },
}


class FakeCursor:
    def __init__(self, state):
        self.state = state
        self.statements = []
        self.fetchone_result = None
        self.rowcount = 0

    def execute(self, query, params=None):
        self.statements.append((query, params))
        self.rowcount = 0
        normalized = " ".join(str(query).split())
        params = params or ()

        if "SELECT id, signal_id, tp1, tp2, tp3, status, execution_mode FROM active_trades WHERE order_id = %s AND execution_mode = %s LIMIT 1 FOR UPDATE" in normalized:
            trade = self.state["trades_by_order"].get(params[0])
            if trade and trade["execution_mode"] == params[1]:
                self.fetchone_result = (
                    trade["id"], trade["signal_id"], trade["tp1"], trade["tp2"], trade["tp3"], trade["status"], trade["execution_mode"],
                )
            else:
                self.fetchone_result = None
            return

        if "SELECT id, signal_id, entry_price, tp1, is_sl_moved, status, execution_mode FROM active_trades" in normalized:
            symbol, side, execution_mode = params
            trade = None
            for candidate in reversed(self.state["trades"]):
                if (
                    candidate["symbol"] == symbol
                    and candidate["side"] == side
                    and candidate["status"] == "OPEN_TPS_SET"
                    and candidate["execution_mode"] == execution_mode
                ):
                    trade = candidate
                    break
            self.fetchone_result = None if trade is None else (
                trade["id"], trade["signal_id"], trade["entry_price"], trade["tp1"], trade["is_sl_moved"], trade["status"], trade["execution_mode"],
            )
            return

        if normalized.startswith("UPDATE active_trades SET status = 'OPEN_TPS_SET'"):
            trade_id, execution_mode = params
            trade = self.state["trades_by_id"].get(trade_id)
            if trade and trade["execution_mode"] == execution_mode and trade["status"] in {"PENDING", "OPEN"}:
                trade["status"] = "OPEN_TPS_SET"
                self.rowcount = 1
            return

        if normalized.startswith("UPDATE active_trades SET status = 'CLOSED', pnl = %s"):
            pnl, trade_id, execution_mode = params
            trade = self.state["trades_by_id"].get(trade_id)
            if trade and trade["execution_mode"] == execution_mode and trade["status"] == "OPEN_TPS_SET":
                trade["status"] = "CLOSED"
                trade["pnl"] = pnl
                self.rowcount = 1
            return

        if normalized.startswith("UPDATE active_trades SET status = 'CLOSED', updated_at = NOW()"):
            trade_id, execution_mode = params
            trade = self.state["trades_by_id"].get(trade_id)
            if trade and trade["execution_mode"] == execution_mode and trade["status"] == "OPEN_TPS_SET":
                trade["status"] = "CLOSED"
                self.rowcount = 1
            return

        if normalized.startswith("UPDATE active_trades SET is_sl_moved = TRUE"):
            trade_id, execution_mode = params
            trade = self.state["trades_by_id"].get(trade_id)
            if trade and trade["execution_mode"] == execution_mode and trade["status"] == "OPEN_TPS_SET" and not trade["is_sl_moved"]:
                trade["is_sl_moved"] = True
                self.rowcount = 1
            return

        if normalized.startswith("UPDATE trades SET"):
            self.state["trade_updates"].append((query, params))
            self.rowcount = 1
            return

    def fetchone(self):
        return self.fetchone_result


class FakeConn:
    def __init__(self, state):
        self.state = state
        self.cursor_obj = FakeCursor(state)
        self.committed = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed += 1


class DummyPool:
    def __init__(self, state):
        self.conn = FakeConn(state)

    def getconn(self):
        return self.conn

    def putconn(self, conn):
        pass


class NoopHTTP:
    def __init__(self, *args, **kwargs):
        pass


class NoopWebSocket:
    def __init__(self, *args, **kwargs):
        pass


class NoopLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


def _install_import_stubs(monkeypatch, state):
    fake_pool_mod = types.SimpleNamespace(ThreadedConnectionPool=lambda *a, **k: DummyPool(state))
    fake_extras = types.SimpleNamespace(RealDictCursor=object)
    fake_psycopg2 = types.SimpleNamespace(pool=fake_pool_mod, extras=fake_extras)
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)
    monkeypatch.setitem(sys.modules, "psycopg2.pool", fake_pool_mod)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", fake_extras)
    monkeypatch.setitem(sys.modules, "ccxt", types.SimpleNamespace(bybit=lambda *a, **k: types.SimpleNamespace()))
    monkeypatch.setitem(sys.modules, "schedule", types.SimpleNamespace(every=lambda *a, **k: types.SimpleNamespace(minutes=types.SimpleNamespace(do=lambda *a, **k: None), seconds=types.SimpleNamespace(do=lambda *a, **k: None), day=types.SimpleNamespace(at=lambda *a, **k: types.SimpleNamespace(do=lambda *a, **k: None)), do=lambda *a, **k: None), run_pending=lambda: None))
    pybit_unified = types.SimpleNamespace(HTTP=NoopHTTP, WebSocket=NoopWebSocket)
    monkeypatch.setitem(sys.modules, "pybit", types.SimpleNamespace(unified_trading=pybit_unified))
    monkeypatch.setitem(sys.modules, "pybit.unified_trading", pybit_unified)
    monkeypatch.setitem(sys.modules, "pythonjsonlogger", types.SimpleNamespace(jsonlogger=types.SimpleNamespace(JsonFormatter=object)))
    monkeypatch.setitem(sys.modules, "dotenv", types.SimpleNamespace(load_dotenv=lambda *a, **k: None))

    fake_logging = types.SimpleNamespace(build_component_logger=lambda *a, **k: NoopLogger())
    monkeypatch.setitem(sys.modules, "modules.logging_setup", fake_logging)
    monkeypatch.setitem(sys.modules, "modules.notifications", types.SimpleNamespace(send_event_message=lambda *a, **k: None))
    fake_database = types.SimpleNamespace(
        get_conn=lambda: FakeConn(state),
        release_conn=lambda conn: None,
        ACTIVE_SIGNAL_STATUSES=('Waiting Entry', 'Queued', 'Order Placed', 'Active'),
    )
    monkeypatch.setitem(sys.modules, "modules.database", fake_database)


def _import_handlers(tmp_path, monkeypatch, state):
    config_path = tmp_path / "custom-config.json"
    config_path.write_text(json.dumps(BASE_CONFIG))
    monkeypatch.setenv("BYBIT_BOT_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("BYBIT_KEY", "key")
    monkeypatch.setenv("BYBIT_SECRET", "secret")
    monkeypatch.chdir(tmp_path)
    _install_import_stubs(monkeypatch, state)
    sys.modules.pop("modules.config_loader", None)
    sys.modules.pop("modules.runtime_paths", None)
    sys.modules.pop("execution.order_manager", None)
    sys.modules.pop("execution.websocket_handlers", None)
    return importlib.import_module("execution.websocket_handlers")


def _make_trade(*, trade_id, order_id, signal_id, symbol="BTC/USDT", side="Buy", status="PENDING", execution_mode="live"):
    return {
        "id": trade_id,
        "order_id": order_id,
        "signal_id": signal_id,
        "symbol": symbol,
        "side": side,
        "tp1": 110.0,
        "tp2": 120.0,
        "tp3": 130.0,
        "entry_price": 100.0,
        "is_sl_moved": False,
        "status": status,
        "execution_mode": execution_mode,
        "pnl": 0.0,
    }


def test_execution_update_is_idempotent_for_duplicate_fill_events(tmp_path, monkeypatch):
    state = {
        "trades": [],
        "trades_by_order": {},
        "trades_by_id": {},
        "trade_updates": [],
    }
    trade = _make_trade(trade_id=1, order_id="oid-1", signal_id=10, status="PENDING", execution_mode="live")
    state["trades"].append(trade)
    state["trades_by_order"]["oid-1"] = trade
    state["trades_by_id"][1] = trade
    mod = _import_handlers(tmp_path, monkeypatch, state)

    call_count = {"tp": 0}

    def fake_fetch_position_safe(symbol):
        return {"contracts": 3}

    def fake_place_split_tps(*args, **kwargs):
        call_count["tp"] += 1
        return True

    monkeypatch.setattr(mod, "fetch_position_safe", fake_fetch_position_safe)
    monkeypatch.setattr(mod, "place_split_tps", fake_place_split_tps)

    message = {"data": [{"symbol": "BTCUSDT", "side": "Buy", "execType": "Trade", "orderId": "oid-1"}]}
    mod.on_execution_update(message)
    mod.on_execution_update(message)

    assert call_count["tp"] == 1
    assert trade["status"] == "OPEN_TPS_SET"


def test_execution_update_skips_non_live_modes(tmp_path, monkeypatch):
    state = {
        "trades": [],
        "trades_by_order": {},
        "trades_by_id": {},
        "trade_updates": [],
    }
    trade = _make_trade(trade_id=2, order_id="oid-2", signal_id=11, status="PENDING", execution_mode="paper")
    state["trades"].append(trade)
    state["trades_by_order"]["oid-2"] = trade
    state["trades_by_id"][2] = trade
    mod = _import_handlers(tmp_path, monkeypatch, state)

    call_count = {"tp": 0}
    monkeypatch.setattr(mod, "fetch_position_safe", lambda symbol: {"contracts": 3})
    monkeypatch.setattr(mod, "place_split_tps", lambda *a, **k: call_count.__setitem__("tp", call_count["tp"] + 1) or True)

    message = {"data": [{"symbol": "BTCUSDT", "side": "Buy", "execType": "Trade", "orderId": "oid-2"}]}
    mod.on_execution_update(message)

    assert call_count["tp"] == 0
    assert trade["status"] == "PENDING"


def test_position_update_is_idempotent_and_mode_scoped(tmp_path, monkeypatch):
    state = {
        "trades": [],
        "trades_by_order": {},
        "trades_by_id": {},
        "trade_updates": [],
    }
    live_trade = _make_trade(trade_id=3, order_id="oid-3", signal_id=12, symbol="BTCUSDT", side="Buy", status="OPEN_TPS_SET", execution_mode="live")
    paper_trade = _make_trade(trade_id=4, order_id="oid-4", signal_id=13, symbol="BTCUSDT", side="Buy", status="OPEN_TPS_SET", execution_mode="paper")
    state["trades"].extend([live_trade, paper_trade])
    state["trades_by_order"]["oid-3"] = live_trade
    state["trades_by_order"]["oid-4"] = paper_trade
    state["trades_by_id"][3] = live_trade
    state["trades_by_id"][4] = paper_trade
    mod = _import_handlers(tmp_path, monkeypatch, state)

    message = {"data": [{"symbol": "BTCUSDT", "side": "Buy", "size": "0", "markPrice": "111.0"}]}
    mod.on_position_update(message)
    mod.on_position_update(message)

    assert live_trade["status"] == "CLOSED"
    assert paper_trade["status"] == "OPEN_TPS_SET"
    assert len([q for q, _ in state["trade_updates"] if "UPDATE trades SET" in q]) == 1


def test_execution_update_ignores_duplicate_after_tps_already_set(tmp_path, monkeypatch):
    state = {
        "trades": [],
        "trades_by_order": {},
        "trades_by_id": {},
        "trade_updates": [],
    }
    trade = _make_trade(trade_id=5, order_id="oid-5", signal_id=14, status="OPEN_TPS_SET", execution_mode="live")
    state["trades"].append(trade)
    state["trades_by_order"]["oid-5"] = trade
    state["trades_by_id"][5] = trade
    mod = _import_handlers(tmp_path, monkeypatch, state)

    call_count = {"tp": 0}
    monkeypatch.setattr(mod, "fetch_position_safe", lambda symbol: {"contracts": 3})
    monkeypatch.setattr(mod, "place_split_tps", lambda *a, **k: call_count.__setitem__("tp", call_count["tp"] + 1) or True)

    message = {"data": [{"symbol": "BTCUSDT", "side": "Buy", "execType": "Trade", "orderId": "oid-5"}]}
    mod.on_execution_update(message)

    assert call_count["tp"] == 0
    assert trade["status"] == "OPEN_TPS_SET"

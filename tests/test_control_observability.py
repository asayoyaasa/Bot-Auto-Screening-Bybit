import importlib
import json
import sys
from datetime import datetime, timezone
from types import SimpleNamespace


class FakeCursor:
    def __init__(self, state):
        self.state = state
        self.last_query = None
        self.last_params = None
        self._fetchone = None

    def execute(self, query, params=None):
        self.last_query = query
        self.last_params = params
        normalized = query.strip()
        if normalized.startswith("SELECT value_text FROM bot_state WHERE key_name = %s"):
            key = params[0]
            self._fetchone = (self.state.get(key),) if key in self.state else None
        elif normalized.startswith("SELECT COUNT(*) FROM trades"):
            self._fetchone = (3,)
        elif normalized.startswith("SELECT COUNT(*) FROM active_trades"):
            self._fetchone = (1,)
        elif normalized.startswith("INSERT INTO bot_state"):
            key = params[0]
            value = params[1]
            self.state[key] = value
            self._fetchone = None
        else:
            self._fetchone = None

    def fetchone(self):
        return self._fetchone


class FakeConn:
    def __init__(self, state):
        self.state = state
        self.cursor_obj = FakeCursor(state)
        self.commits = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1


def _install_import_stubs(monkeypatch, state):
    fake_database = SimpleNamespace(
        ACTIVE_SIGNAL_STATUSES=("Waiting Entry", "Queued", "Order Placed", "Active"),
        get_conn=lambda: FakeConn(state),
        release_conn=lambda conn: None,
    )
    fake_config = {
        "execution": {"mode": "paper"},
        "system": {"timezone": "UTC"},
    }
    monkeypatch.setitem(sys.modules, "dotenv", SimpleNamespace(load_dotenv=lambda *a, **k: None))
    monkeypatch.setitem(sys.modules, "modules.config_loader", SimpleNamespace(CONFIG=fake_config))
    monkeypatch.setitem(sys.modules, "modules.database", fake_database)
    sys.modules.pop("modules.control", None)


def _import_control(monkeypatch, tmp_path):
    state = {}
    _install_import_stubs(monkeypatch, state)
    module = importlib.import_module("modules.control")
    monkeypatch.setattr(module, "HEALTH_FILE_PATH", tmp_path / "logs" / "health_status.json", raising=False)
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path, raising=False)
    return module, state


def test_update_heartbeat_includes_pause_and_mode_metadata(tmp_path, monkeypatch):
    control, state = _import_control(monkeypatch, tmp_path)

    control.set_paused(True, "manual maintenance")
    control.update_heartbeat("scanner", {"signals": 7})

    heartbeat = json.loads(state["heartbeat:scanner"])
    assert heartbeat["service"] == "scanner"
    assert heartbeat["details"]["mode"] == "paper"
    assert heartbeat["details"]["paused"] is True
    assert heartbeat["details"]["signals"] == 7

    health_path = tmp_path / "logs" / "health_status.json"
    assert health_path.exists()
    health = json.loads(health_path.read_text())
    assert health["mode"] == "paper"
    assert health["paused"]["paused"] is True
    assert health["overall_healthy"] is False
    assert health["scanner"]["healthy"] is True
    assert "age_seconds" in health["scanner"]


def test_update_heartbeat_does_not_allow_details_to_override_control_metadata(tmp_path, monkeypatch):
    control, state = _import_control(monkeypatch, tmp_path)

    control.set_paused(False, "")
    control.update_heartbeat("autotrader", {"mode": "live", "paused": True, "signals": 3})

    heartbeat = json.loads(state["heartbeat:autotrader"])
    assert heartbeat["details"]["mode"] == "paper"
    assert heartbeat["details"]["paused"] is False
    assert heartbeat["details"]["signals"] == 3


def test_get_status_snapshot_exposes_health_metadata(tmp_path, monkeypatch):
    control, state = _import_control(monkeypatch, tmp_path)
    state["bot_paused"] = json.dumps({"paused": False, "reason": "", "updated_at": None})
    now = datetime.now(timezone.utc)
    ts = (now.replace(microsecond=0)).isoformat()
    state["heartbeat:scanner"] = json.dumps(
        {
            "service": "scanner",
            "ts": ts,
            "details": {"mode": "paper", "paused": False},
        }
    )
    state["heartbeat:autotrader"] = json.dumps(
        {
            "service": "autotrader",
            "ts": ts,
            "details": {"mode": "paper", "paused": False},
        }
    )

    snap = control.get_status_snapshot()

    assert snap["paused"]["paused"] is False
    assert snap["scanner_healthy"] is True
    assert snap["autotrader_healthy"] is True
    assert snap["overall_healthy"] is True
    assert snap["scanner_heartbeat_age_seconds"] is not None
    assert snap["autotrader_heartbeat_age_seconds"] is not None

import json
import os
from datetime import datetime, timezone

from modules.config_loader import CONFIG
from modules.database import ACTIVE_SIGNAL_STATUSES, get_conn, release_conn


HEARTBEAT_PREFIX = 'heartbeat:'
PAUSE_KEY = 'bot_paused'
LAST_TELEGRAM_UPDATE_ID_KEY = 'telegram_last_update_id'
HEALTH_FILE_PATH = os.path.join('logs', 'health_status.json')


def _set_state(key, value):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO bot_state (key_name, value_text)
            VALUES (%s, %s)
            ON CONFLICT (key_name) DO UPDATE SET value_text = EXCLUDED.value_text
            """,
            (key, value),
        )
        conn.commit()
    finally:
        release_conn(conn)


def _get_state(key, default=None):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT value_text FROM bot_state WHERE key_name = %s", (key,))
        row = cur.fetchone()
        return row[0] if row else default
    finally:
        release_conn(conn)


def set_paused(paused, reason=""):
    payload = {
        'paused': bool(paused),
        'reason': reason or '',
        'updated_at': datetime.now(timezone.utc).isoformat(),
    }
    _set_state(PAUSE_KEY, json.dumps(payload))
    write_health_snapshot()


def get_pause_state():
    raw = _get_state(PAUSE_KEY)
    if not raw:
        return {'paused': False, 'reason': '', 'updated_at': None}
    try:
        return json.loads(raw)
    except Exception:
        return {'paused': False, 'reason': '', 'updated_at': None}


def is_paused():
    return bool(get_pause_state().get('paused'))


def update_heartbeat(service_name, details=None):
    payload = {
        'service': service_name,
        'ts': datetime.now(timezone.utc).isoformat(),
        'details': details or {},
    }
    _set_state(f"{HEARTBEAT_PREFIX}{service_name}", json.dumps(payload))
    write_health_snapshot()


def _heartbeat_is_fresh(heartbeat, stale_after_seconds=180):
    if not heartbeat or not heartbeat.get('ts'):
        return False
    try:
        ts = datetime.fromisoformat(heartbeat['ts'].replace('Z', '+00:00'))
        return (datetime.now(timezone.utc) - ts).total_seconds() <= stale_after_seconds
    except Exception:
        return False


def write_health_snapshot(stale_after_seconds=180):
    try:
        os.makedirs('logs', exist_ok=True)
        snapshot = get_status_snapshot()
        health = {
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'paused': snapshot.get('paused', {}),
            'active_signals': snapshot.get('active_signals', 0),
            'active_positions': snapshot.get('active_positions', 0),
            'scanner': {
                'heartbeat': snapshot.get('scanner_heartbeat'),
                'healthy': _heartbeat_is_fresh(snapshot.get('scanner_heartbeat'), stale_after_seconds),
            },
            'autotrader': {
                'heartbeat': snapshot.get('autotrader_heartbeat'),
                'healthy': _heartbeat_is_fresh(snapshot.get('autotrader_heartbeat'), stale_after_seconds),
            },
        }
        health['overall_healthy'] = bool(
            not health['paused'].get('paused')
            and health['scanner']['healthy']
            and health['autotrader']['healthy']
        )
        with open(HEALTH_FILE_PATH, 'w') as f:
            json.dump(health, f, indent=2)
    except Exception:
        pass


def get_heartbeat(service_name):
    raw = _get_state(f"{HEARTBEAT_PREFIX}{service_name}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def get_last_telegram_update_id(default=0):
    raw = _get_state(LAST_TELEGRAM_UPDATE_ID_KEY, str(default))
    try:
        return int(raw)
    except Exception:
        return default


def set_last_telegram_update_id(update_id):
    _set_state(LAST_TELEGRAM_UPDATE_ID_KEY, str(update_id))


def get_status_snapshot():
    conn = get_conn()
    try:
        cur = conn.cursor()
        current_mode = str(CONFIG.get('execution', {}).get('mode', 'paper')).strip().lower()
        cur.execute("SELECT COUNT(*) FROM trades WHERE status = ANY(%s) AND execution_mode = %s", (list(ACTIVE_SIGNAL_STATUSES), current_mode))
        active_signals = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM active_trades WHERE status IN ('PENDING', 'OPEN', 'OPEN_TPS_SET') AND execution_mode = %s", (current_mode,))
        active_positions = cur.fetchone()[0]
    except Exception:
        active_signals = 0
        active_positions = 0
    finally:
        release_conn(conn)

    return {
        'mode': CONFIG.get('execution', {}).get('mode', 'paper'),
        'paused': get_pause_state(),
        'scanner_heartbeat': get_heartbeat('scanner'),
        'autotrader_heartbeat': get_heartbeat('autotrader'),
        'active_signals': active_signals,
        'active_positions': active_positions,
    }

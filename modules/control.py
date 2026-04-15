import json
import logging
from datetime import datetime, timezone

from modules.config_loader import CONFIG
from modules.database import ACTIVE_SIGNAL_STATUSES, get_conn, release_conn
from modules.runtime_paths import REPO_ROOT


logger = logging.getLogger(__name__)
HEARTBEAT_PREFIX = 'heartbeat:'
PAUSE_KEY = 'bot_paused'
LAST_TELEGRAM_UPDATE_ID_KEY = 'telegram_last_update_id'
HEALTH_FILE_PATH = REPO_ROOT / 'logs' / 'health_status.json'


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


def _heartbeat_age_seconds(heartbeat):
    if not heartbeat or not heartbeat.get('ts'):
        return None
    try:
        ts = datetime.fromisoformat(heartbeat['ts'].replace('Z', '+00:00'))
        return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())
    except Exception:
        return None


def _normalize_heartbeat(heartbeat, stale_after_seconds=180):
    if not heartbeat or not isinstance(heartbeat, dict):
        return {'heartbeat': None, 'healthy': False, 'age_seconds': None, 'details': {}}
    details = heartbeat.get('details') if isinstance(heartbeat.get('details'), dict) else {}
    return {
        'heartbeat': heartbeat,
        'healthy': _heartbeat_is_fresh(heartbeat, stale_after_seconds),
        'age_seconds': _heartbeat_age_seconds(heartbeat),
        'details': details,
    }


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
        state = json.loads(raw)
        if not isinstance(state, dict):
            raise ValueError('pause state must be an object')
        state.setdefault('paused', False)
        state.setdefault('reason', '')
        state.setdefault('updated_at', None)
        return state
    except Exception:
        return {'paused': False, 'reason': '', 'updated_at': None}


def is_paused():
    return bool(get_pause_state().get('paused'))


def update_heartbeat(service_name, details=None):
    current_pause = get_pause_state()
    payload_details = dict(details or {})
    payload_details['mode'] = str(CONFIG.get('execution', {}).get('mode', 'paper')).strip().lower()
    payload_details['paused'] = bool(current_pause.get('paused', False))
    payload = {
        'service': service_name,
        'ts': datetime.now(timezone.utc).isoformat(),
        'details': payload_details,
    }
    _set_state(f"{HEARTBEAT_PREFIX}{service_name}", json.dumps(payload))
    write_health_snapshot()


def _heartbeat_is_fresh(heartbeat, stale_after_seconds=180):
    age = _heartbeat_age_seconds(heartbeat)
    return age is not None and age <= stale_after_seconds


def write_health_snapshot(stale_after_seconds=180):
    try:
        REPO_ROOT.joinpath('logs').mkdir(parents=True, exist_ok=True)
        snapshot = get_status_snapshot()
        scanner = _normalize_heartbeat(snapshot.get('scanner_heartbeat'), stale_after_seconds)
        autotrader = _normalize_heartbeat(snapshot.get('autotrader_heartbeat'), stale_after_seconds)
        paused = snapshot.get('paused', {}) if isinstance(snapshot.get('paused', {}), dict) else {}
        health = {
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'mode': snapshot.get('mode', CONFIG.get('execution', {}).get('mode', 'paper')),
            'paused': paused,
            'active_signals': snapshot.get('active_signals', 0),
            'active_positions': snapshot.get('active_positions', 0),
            'scanner': scanner,
            'autotrader': autotrader,
        }
        health['overall_healthy'] = bool(
            not paused.get('paused')
            and scanner['healthy']
            and autotrader['healthy']
        )
        with open(HEALTH_FILE_PATH, 'w', encoding='utf-8') as f:
            json.dump(health, f, indent=2)
    except Exception as exc:
        logger.error('Failed to write health snapshot: %s', exc, exc_info=True)
        try:
            REPO_ROOT.joinpath('logs').mkdir(parents=True, exist_ok=True)
            fallback = {
                'generated_at': datetime.now(timezone.utc).isoformat(),
                'mode': str(CONFIG.get('execution', {}).get('mode', 'paper')).strip().lower(),
                'paused': get_pause_state(),
                'active_signals': 0,
                'active_positions': 0,
                'scanner': {'heartbeat': None, 'healthy': False, 'age_seconds': None, 'details': {}},
                'autotrader': {'heartbeat': None, 'healthy': False, 'age_seconds': None, 'details': {}},
                'overall_healthy': False,
                'error': str(exc),
            }
            with open(HEALTH_FILE_PATH, 'w', encoding='utf-8') as f:
                json.dump(fallback, f, indent=2)
        except Exception:
            logger.error('Failed to write fallback health snapshot', exc_info=True)


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

    paused = get_pause_state()
    scanner_heartbeat = get_heartbeat('scanner')
    autotrader_heartbeat = get_heartbeat('autotrader')
    scanner_health = _normalize_heartbeat(scanner_heartbeat)
    autotrader_health = _normalize_heartbeat(autotrader_heartbeat)
    overall_healthy = bool(
        not paused.get('paused')
        and scanner_health['healthy']
        and autotrader_health['healthy']
    )

    return {
        'mode': CONFIG.get('execution', {}).get('mode', 'paper'),
        'paused': paused,
        'scanner_heartbeat': scanner_heartbeat,
        'autotrader_heartbeat': autotrader_heartbeat,
        'scanner_heartbeat_age_seconds': scanner_health['age_seconds'],
        'autotrader_heartbeat_age_seconds': autotrader_health['age_seconds'],
        'scanner_healthy': scanner_health['healthy'],
        'autotrader_healthy': autotrader_health['healthy'],
        'overall_healthy': overall_healthy,
        'active_signals': active_signals,
        'active_positions': active_positions,
    }

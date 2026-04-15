import json
import logging
import os

from dotenv import load_dotenv

from modules.runtime_paths import get_config_path

load_dotenv()
logger = logging.getLogger(__name__)

SUPPORTED_EXECUTION_MODES = ('paper', 'live')

REQUIRED_PATHS = [
    ('database', 'host'),
    ('database', 'database'),
    ('database', 'user'),
    ('database', 'password'),
    ('database', 'port'),
    ('system', 'timezone'),
    ('system', 'max_threads'),
    ('system', 'check_interval_hours'),
    ('system', 'timeframes'),
    ('setup', 'fib_entry_start'),
    ('setup', 'fib_entry_end'),
    ('setup', 'fib_sl'),
    ('strategy', 'min_tech_score'),
    ('strategy', 'risk_reward_min'),
    ('indicators', 'min_rvol'),
    ('patterns',),
    ('pattern_signals',),
]


def _get_nested(config, path):
    current = config
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _is_blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _normalize_api_aliases(config):
    api = config.setdefault('api', {})
    aliases = {
        'bybit_key': ('bybit_key', 'BYBIT_KEY'),
        'bybit_secret': ('bybit_secret', 'BYBIT_SECRET'),
        'telegram_bot_token': ('telegram_bot_token', 'TELEGRAM_TOKEN'),
        'telegram_chat_id': ('telegram_chat_id', 'TELEGRAM_CHAT_ID'),
        'discord_webhook': ('discord_webhook', 'DISCORD_WEBHOOK'),
    }

    for canonical, keys in aliases.items():
        current = api.get(canonical, '')
        if not _is_blank(current):
            api[canonical] = current
            continue

        for key in keys:
            candidate = api.get(key, '')
            if not _is_blank(candidate):
                api[canonical] = candidate
                break
        else:
            api[canonical] = ''

    return config


def validate_config(config):
    missing = []
    for path in REQUIRED_PATHS:
        value = _get_nested(config, path)
        if _is_blank(value):
            missing.append('.'.join(path))

    if missing:
        raise ValueError('Missing required config values in config.json: ' + ', '.join(missing))

    execution = config.get('execution', {})
    mode = str(execution.get('mode', '')).strip().lower()
    if mode not in SUPPORTED_EXECUTION_MODES:
        raise ValueError(f"execution.mode must be one of: {', '.join(SUPPORTED_EXECUTION_MODES)}")

    if not isinstance(config['system']['timeframes'], list) or not config['system']['timeframes']:
        raise ValueError('system.timeframes must be a non-empty list')
    if config['system']['max_threads'] <= 0:
        raise ValueError('system.max_threads must be > 0')
    if config['strategy']['risk_reward_min'] <= 0:
        raise ValueError('strategy.risk_reward_min must be > 0')
    if config['indicators']['min_rvol'] <= 0:
        raise ValueError('indicators.min_rvol must be > 0')

    if mode == 'live':
        api = config.get('api', {})
        env_key = os.getenv('BYBIT_KEY', '').strip()
        env_secret = os.getenv('BYBIT_SECRET', '').strip()
        if _is_blank(api.get('bybit_key', '')) and _is_blank(env_key):
            raise ValueError('Live mode requires api.bybit_key and api.bybit_secret to be set')
        if _is_blank(api.get('bybit_secret', '')) and _is_blank(env_secret):
            raise ValueError('Live mode requires api.bybit_key and api.bybit_secret to be set')

    return config


def _read_json_config(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _override_if_env_present(config, key_name, env_name):
    env_value = os.getenv(env_name, '')
    if env_value.strip():
        config['api'][key_name] = env_value
    else:
        config['api'][key_name] = config['api'].get(key_name, '')


def load_config():
    config_path = get_config_path()
    if not config_path.exists():
        raise FileNotFoundError(
            f'config.json not found at {config_path}. Copy config.example.json to config.json and fill in your credentials.'
        )

    config = _read_json_config(config_path)
    config.setdefault('api', {})
    config.setdefault('notifications', {})
    config.setdefault('execution', {'mode': 'paper'})

    _override_if_env_present(config, 'bybit_key', 'BYBIT_KEY')
    _override_if_env_present(config, 'bybit_secret', 'BYBIT_SECRET')
    _override_if_env_present(config, 'telegram_bot_token', 'TELEGRAM_TOKEN')
    _override_if_env_present(config, 'telegram_chat_id', 'TELEGRAM_CHAT_ID')
    _override_if_env_present(config, 'discord_webhook', 'DISCORD_WEBHOOK')

    _normalize_api_aliases(config)

    if os.getenv('BOT_ENV') == 'testing':
        logger.warning('RUNNING IN TEST MODE')
        config['database']['database'] = 'bybit_bot_test'

    validated = validate_config(config)
    return validated


CONFIG = load_config()

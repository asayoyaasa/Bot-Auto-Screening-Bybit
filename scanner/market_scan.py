from modules.config_loader import CONFIG
from modules.control import is_paused, update_heartbeat
from modules.database import get_active_signals
from modules.logging_setup import build_component_logger
from modules.notifications import send_alert, send_scan_completion
from modules.runtime_utils import retry_call
from scanner.signal_builder import analyze_ticker, get_btc_bias

import ccxt
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = build_component_logger('Scanner', 'scanner.log')
EXECUTION_MODE = str(CONFIG.get('execution', {}).get('mode', 'paper')).strip().lower()
MODE_TAG = f"[{EXECUTION_MODE.upper()}]"

exchange = ccxt.bybit({
    'apiKey': CONFIG['api']['bybit_key'],
    'secret': CONFIG['api']['bybit_secret'],
    'options': {'defaultType': 'swap', 'adjustForTimeDifference': True},
    'enableRateLimit': True,
})


def ex_call(method_name, *args, context="", **kwargs):
    method = getattr(exchange, method_name)
    return retry_call(method, *args, retries=3, base_delay=1.0, logger=logger, context=context or method_name, **kwargs)


def scan():
    if is_paused():
        logger.info("Scanner paused; skipping scan cycle")
        update_heartbeat('scanner', {'paused': True})
        return

    start_time = time.time()
    logger.info(f"{MODE_TAG} 🔭 Scanning... Env: {os.getenv('BOT_ENV', 'PROD')}")
    btc_bias = get_btc_bias(ex_call)
    logger.info(f"📊 BTC Bias: {btc_bias}")

    active_signals = get_active_signals()
    logger.info(f"🛡️ Active Signals Ignored: {len(active_signals)}")
    signal_count = 0

    try:
        mkts = ex_call('load_markets', context='load markets')
        stablecoins = ['USDC', 'USDT', 'DAI', 'FDUSD', 'USDD', 'USDE', 'TUSD', 'BUSD', 'PYUSD', 'USDS', 'EUR', 'USD']
        syms = [
            s for s in mkts
            if mkts[s].get('swap') and mkts[s]['quote'] == 'USDT' and mkts[s].get('active') and mkts[s]['base'] not in stablecoins
        ]
        random.shuffle(syms)
        logger.info(f"🔍 Scanning {len(syms)} valid pairs (Stables removed)...")

        for tf in reversed(CONFIG['system']['timeframes']):
            with ThreadPoolExecutor(max_workers=CONFIG['system']['max_threads']) as ex:
                futures = [ex.submit(analyze_ticker, s, tf, btc_bias, active_signals) for s in syms]
                for f in as_completed(futures):
                    res = f.result()
                    if res and send_alert(res):
                        signal_count += 1
    except Exception as e:
        logger.error(f"Scan Error: {e}")
    finally:
        duration = time.time() - start_time
        update_heartbeat('scanner', {'signals': signal_count, 'duration': duration, 'bias': btc_bias, 'mode': EXECUTION_MODE})
        logger.info(f"✅ Scan Finished in {duration:.2f}s. Signals: {signal_count}")
        send_scan_completion(signal_count, duration, btc_bias)

import time

import schedule

from modules.config_loader import CONFIG
from modules.logging_setup import build_component_logger
from modules.notifications import run_fast_update
from scanner.market_scan import EXECUTION_MODE, scan

logger = build_component_logger('ScannerScheduler', 'scanner.log')


def run_scanner():
    logger.info("🚀 Scanner starting.")
    scan()
    schedule.every(CONFIG['system']['check_interval_hours']).hours.do(scan)
    schedule.every(1).minutes.do(run_fast_update)
    logger.info("🚀 Bot Started.")

    while True:
        schedule.run_pending()
        time.sleep(1)

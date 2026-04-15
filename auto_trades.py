import logging
import os
import time
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import ccxt
import schedule
from pybit.unified_trading import HTTP, WebSocket
from pythonjsonlogger import jsonlogger

from modules.config_loader import CONFIG
from modules.control import is_paused, update_heartbeat
from modules.database import get_conn, release_conn
from modules.logging_setup import build_component_logger
from modules.notifications import send_event_message
from modules.paper_trade_utils import (
    apply_slippage,
    build_paper_event_sequence,
    gross_pnl_for_exit,
    merge_paper_settings,
    normalize_execution_mode,
    normalize_side,
    touch_triggered,
    trade_fee,
    validate_quantity,
)
from modules.runtime_utils import retry_call
from execution.paper_engine import (
    DEFAULT_PAPER_SETTINGS,
    build_event_sequence as paper_build_event_sequence,
    close_paper_trade as paper_close_paper_trade,
    compute_paper_equity as paper_compute_paper_equity,
    fetch_active_trade as paper_fetch_active_trade,
    paper_settings,
    process_paper_trades as paper_process_paper_trades,
    take_partial_profit as paper_take_partial_profit,
)
from execution.reporting import generate_daily_report as reporting_generate_daily_report
from execution.websocket_handlers import (
    on_execution_update as ws_on_execution_update,
    on_position_update as ws_on_position_update,
)
from execution.order_manager import (
    build_entry_order_link_id,
    fetch_position_safe as fetch_position_safe_manager,
    move_stop_to_entry as move_stop_to_entry_manager,
    place_entry_order,
    place_split_tps as place_split_tps_manager,
    resolve_position_contracts,
)

TARGET_LEVERAGE = 25
RISK_PERCENT = 0.01
MAX_POSITIONS = 40
TP_SPLIT = [0.30, 0.30, 0.40]
EXECUTION_MODE = normalize_execution_mode(CONFIG.get('execution', {}).get('mode', 'paper'))
IS_PAPER = EXECUTION_MODE == 'paper'
MODE_TAG = f"[{EXECUTION_MODE.upper()}]"

logger = build_component_logger('AutoTrader', 'auto_trades.log', json_format=True, pii_mask=True)

exchange = ccxt.bybit({
    'apiKey': CONFIG['api'].get('bybit_key', ''),
    'secret': CONFIG['api'].get('bybit_secret', ''),
    'options': {'defaultType': 'swap', 'adjustForTimeDifference': True},
    'enableRateLimit': True,
})

bybit_http = HTTP(
    testnet=False,
    api_key=CONFIG['api'].get('bybit_key', ''),
    api_secret=CONFIG['api'].get('bybit_secret', ''),
)


def ex_call(method_name, *args, context="", **kwargs):
    method = getattr(exchange, method_name)
    return retry_call(method, *args, retries=3, base_delay=1.0, logger=logger, context=context or method_name, **kwargs)


def fetch_position_safe(symbol):
    return fetch_position_safe_manager(exchange, symbol, logger=logger, retry_call=retry_call)


def get_position_contracts(position):
    return resolve_position_contracts(position)


def fetch_closed_pnl_safe(symbol):
    try:
        trades = ex_call('fetch_my_trades', symbol, limit=20, context=f'fetch closed pnl {symbol}')
        for trade in reversed(trades or []):
            info = trade.get('info', {}) if isinstance(trade, dict) else {}
            closed_pnl = info.get('closedPnl')
            if closed_pnl not in (None, ''):
                return float(closed_pnl)
    except Exception as exc:
        logger.warning(f"fetch_my_trades PnL lookup failed for {symbol}: {exc}")
    return 0.0


def _bybit_symbol(symbol):
    return symbol.replace('/', '')




def update_signal_status(cur, signal_id, status, *, entry_hit=False, closed=False, exit_price=None):
    if not signal_id:
        return
    fields = ["status = %s"]
    params = [status]
    if entry_hit:
        fields.append("entry_hit_at = COALESCE(entry_hit_at, NOW())")
    if closed:
        fields.append("closed_at = NOW()")
    if exit_price is not None:
        fields.append("exit_price = %s")
        params.append(exit_price)
    params.append(signal_id)
    cur.execute(f"UPDATE trades SET {', '.join(fields)} WHERE id = %s", tuple(params))


def update_active_trade_mode(cur, active_trade_id):
    if not active_trade_id:
        return
    cur.execute(
        "UPDATE active_trades SET execution_mode = %s WHERE id = %s",
        (EXECUTION_MODE, active_trade_id),
    )


def fetch_last_price(symbol):
    ticker = ex_call('fetch_ticker', symbol, context=f'fetch ticker {symbol} paper')
    return float(ticker['last'])


def fetch_latest_candle(symbol, timeframe='1m'):
    candles = ex_call('fetch_ohlcv', symbol, timeframe, limit=1, context=f'fetch latest candle {symbol} {timeframe}')
    if not candles:
        return None
    ts, o, h, l, c, v = candles[-1]
    return {
        'timestamp': ts,
        'open': float(o),
        'high': float(h),
        'low': float(l),
        'close': float(c),
        'volume': float(v),
    }


def build_event_sequence(side, low_price, high_price, stop_loss, targets):
    return paper_build_event_sequence(side, low_price, high_price, stop_loss, targets)


def fetch_active_trade(cur, trade_id):
    return paper_fetch_active_trade(cur, trade_id)


def compute_paper_equity(cur):
    return paper_compute_paper_equity(cur)


def close_paper_trade(cur, trade, exit_price, reason):
    return paper_close_paper_trade(cur, trade, exit_price, reason)


def take_partial_profit(cur, trade, target_no, target_price):
    return paper_take_partial_profit(cur, trade, target_no, target_price)


def process_paper_trades():
    return paper_process_paper_trades()


def init_execution_db():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS active_trades (
                id SERIAL PRIMARY KEY,
                signal_id INT,
                symbol VARCHAR(20),
                side VARCHAR(10),
                entry_price DECIMAL,
                sl_price DECIMAL,
                tp1 DECIMAL,
                tp2 DECIMAL,
                tp3 DECIMAL,
                quantity DECIMAL,
                leverage INT,
                order_id VARCHAR(50),
                status VARCHAR(20) DEFAULT 'PENDING',
                pnl DECIMAL DEFAULT 0,
                is_sl_moved BOOLEAN DEFAULT FALSE,
                execution_mode VARCHAR(20) DEFAULT 'paper',
                remaining_quantity DECIMAL DEFAULT 0,
                filled_quantity DECIMAL DEFAULT 0,
                entry_fill_price DECIMAL,
                exit_fill_price DECIMAL,
                realized_fees DECIMAL DEFAULT 0,
                realized_pnl_gross DECIMAL DEFAULT 0,
                realized_pnl_net DECIMAL DEFAULT 0,
                tp1_hit BOOLEAN DEFAULT FALSE,
                tp2_hit BOOLEAN DEFAULT FALSE,
                tp3_hit BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        required_active_trade_columns = {
            'execution_mode': "VARCHAR(20) DEFAULT 'paper'",
            'remaining_quantity': "DECIMAL DEFAULT 0",
            'filled_quantity': "DECIMAL DEFAULT 0",
            'entry_fill_price': "DECIMAL",
            'exit_fill_price': "DECIMAL",
            'realized_fees': "DECIMAL DEFAULT 0",
            'realized_pnl_gross': "DECIMAL DEFAULT 0",
            'realized_pnl_net': "DECIMAL DEFAULT 0",
            'tp1_hit': "BOOLEAN DEFAULT FALSE",
            'tp2_hit': "BOOLEAN DEFAULT FALSE",
            'tp3_hit': "BOOLEAN DEFAULT FALSE",
        }
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'active_trades';")
        active_cols = {row[0] for row in cur.fetchall()}
        for col_name, col_type in required_active_trade_columns.items():
            if col_name not in active_cols:
                cur.execute(f"ALTER TABLE active_trades ADD COLUMN IF NOT EXISTS {col_name} {col_type}")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_reports (
                report_date DATE,
                total_pnl DECIMAL DEFAULT 0,
                win_rate DECIMAL DEFAULT 0,
                total_wins INT DEFAULT 0,
                total_losses INT DEFAULT 0,
                total_trades INT DEFAULT 0,
                best_trade_symbol VARCHAR(20),
                best_trade_pnl DECIMAL,
                worst_trade_symbol VARCHAR(20),
                worst_trade_pnl DECIMAL,
                execution_mode VARCHAR(20) DEFAULT 'paper',
                generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_daily_reports_date_mode ON daily_reports (report_date, execution_mode);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_active_trades_status ON active_trades(status);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_active_trades_signal_id ON active_trades(signal_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_active_trades_symbol_status ON active_trades(symbol, status);")
        conn.commit()
        logger.info("✅ Execution Database Tables Sync Complete.")
    except Exception as e:
        logger.error(f"❌ DB Init Error: {e}")
    finally:
        release_conn(conn)




def on_execution_update(message):
    return ws_on_execution_update(message)


def on_position_update(message):
    return ws_on_position_update(message)


def ingest_fresh_signals():
    if is_paused():
        logger.info("Autotrader paused; skipping signal ingestion")
        update_heartbeat('autotrader', {'paused': True})
        return

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM active_trades WHERE status IN ('OPEN', 'OPEN_TPS_SET') AND execution_mode = %s",
            (EXECUTION_MODE,),
        )
        current_active = cur.fetchone()[0]
        if current_active >= MAX_POSITIONS:
            update_heartbeat('autotrader', {'active_positions': current_active, 'max_positions_reached': True})
            return

        if IS_PAPER:
            total_equity = paper_settings().get('initial_balance', DEFAULT_PAPER_SETTINGS['initial_balance'])
        else:
            balance = ex_call('fetch_balance', context='fetch balance')
            total_equity = float(balance['total']['USDT'])
        markets = ex_call('load_markets', context='load markets autotrader')

        query = """
            SELECT t.id, t.symbol, t.side, t.entry_price, t.sl_price, t.tp1, t.tp2, t.tp3
            FROM trades t
            LEFT JOIN active_trades a ON t.id = a.signal_id
            WHERE t.status = 'Waiting Entry'
            AND t.created_at >= NOW() - INTERVAL '12 hours'
            AND a.id IS NULL
        """
        cur.execute(query)
        signals = cur.fetchall()

        for sig in signals:
            if current_active >= MAX_POSITIONS:
                break
            sig_id, sym, side, entry, sl, tp1, tp2, tp3 = sig
            entry, sl = float(entry), float(sl)
            market = markets.get(sym)
            max_lev = 25
            if market and 'limits' in market:
                leverage_limits = market['limits'].get('leverage') if market.get('limits') else None
                if leverage_limits and leverage_limits.get('max'):
                    max_lev = float(leverage_limits['max'])
            final_leverage = min(TARGET_LEVERAGE, int(max_lev))
            margin_cost = total_equity * RISK_PERCENT
            position_value = margin_cost * final_leverage
            qty_coins = position_value / entry

            if position_value < 6.0:
                logger.warning(f"⚠️ Signal {sym} skipped: Position value ${position_value:.2f} is below Bybit min ($6).")
                continue

            cur.execute(
                "INSERT INTO active_trades (signal_id, symbol, side, entry_price, sl_price, tp1, tp2, tp3, quantity, leverage, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'PENDING') RETURNING id",
                (sig_id, sym, side, entry, sl, tp1, tp2, tp3, qty_coins, final_leverage),
            )
            active_trade_id = cur.fetchone()[0]
            update_active_trade_mode(cur, active_trade_id)
            update_signal_status(cur, sig_id, 'Queued')
            logger.info(f"{MODE_TAG} 📥 Signal Ingested: {sym} | Lev: {final_leverage}x | Cost: ${margin_cost:.2f}")
            current_active += 1

        conn.commit()
        update_heartbeat('autotrader', {'active_positions': current_active, 'equity': total_equity})
    except Exception as e:
        logger.error(f"Ingest Error: {e}")
    finally:
        release_conn(conn)


def execute_pending_orders():
    if is_paused():
        return
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, signal_id, symbol, side, entry_price, sl_price, quantity, leverage "
            "FROM active_trades WHERE status = 'PENDING' AND execution_mode = %s",
            (EXECUTION_MODE,),
        )
        orders = cur.fetchall()
        if not orders:
            return

        for order in orders:
            oid, signal_id, sym, side, entry, sl, qty, lev = order
            try:
                if IS_PAPER:
                    cur.execute("UPDATE active_trades SET status = 'OPEN', updated_at = NOW() WHERE id = %s", (oid,))
                    update_signal_status(cur, signal_id, 'Order Placed')
                    conn.commit()
                    logger.info(f"{MODE_TAG} 📝 Virtual order queued for {sym} at entry {entry}")
                    continue

                try:
                    ex_call('set_leverage', int(lev), sym, context=f'set leverage {sym}')
                except Exception as e:
                    logger.warning(f"Could not set leverage for {sym}: {e}")

                ticker = ex_call('fetch_ticker', sym, context=f'fetch ticker {sym} execute')
                current_price = float(ticker['last'])
                order_result = place_entry_order(
                    exchange,
                    sym,
                    side,
                    qty,
                    float(entry),
                    float(sl),
                    current_price,
                    logger=logger,
                    retry_call=retry_call,
                    order_link_id=build_entry_order_link_id(sym, oid),
                )
                response = order_result.response if isinstance(order_result.response, dict) else {}
                if response and response.get('id'):
                    cur.execute("UPDATE active_trades SET order_id = %s, status = 'OPEN' WHERE id = %s", (response['id'], oid))
                    update_signal_status(cur, signal_id, 'Order Placed')
                    conn.commit()
                    logger.info(f"{MODE_TAG} ✅ Order Placed for {sym} (ID: {response['id']})")
            except Exception as e:
                logger.error(f"{MODE_TAG} ❌ Execution Failed {sym}: {e}")
                cur.execute("UPDATE active_trades SET status = 'FAILED' WHERE id = %s", (oid,))
                update_signal_status(cur, signal_id, 'Cancelled', closed=True)
        conn.commit()
    except Exception as e:
        logger.error(f"{MODE_TAG} Exec Loop Error: {e}")
    finally:
        release_conn(conn)


def check_missed_tps():
    if IS_PAPER:
        return
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, signal_id, symbol, side, order_id, tp1, tp2, tp3 FROM active_trades WHERE status = 'OPEN' AND order_id IS NOT NULL")
        stuck_trades = cur.fetchall()
        for trade in stuck_trades:
            t_id, signal_id, sym, side, oid, tp1, tp2, tp3 = trade
            try:
                order_status = None
                try:
                    order = ex_call('fetch_order', oid, sym, params={'acknowledged': True}, context=f'fetch order {sym}')
                    order_status = order['status']
                except Exception:
                    closed_orders = ex_call('fetch_closed_orders', sym, limit=50, context=f'fetch closed orders {sym}')
                    for o in closed_orders:
                        if str(o['id']) == str(oid):
                            order_status = o['status']
                            break

                if order_status == 'closed':
                    logger.warning(f"⚠️ Safety Net: Found filled entry for {sym} (ID: {oid}). Placing TPs...")
                    pos = fetch_position_safe(sym)
                    size = resolve_position_contracts(pos)
                    if size > 0 and place_split_tps_manager(exchange, sym, side, size, tp1, tp2, tp3, logger=logger, retry_call=retry_call, trade_id=t_id):
                        cur.execute("UPDATE active_trades SET status = 'OPEN_TPS_SET', updated_at = NOW() WHERE id = %s", (t_id,))
                        update_signal_status(cur, signal_id, 'Active', entry_hit=True)
                        conn.commit()
                        logger.info(f"✅ Safety Net: TPs recovered for {sym}")
                        send_event_message(f"Recovered Missing TPs: {sym}", [f"Order ID: {oid}", f"Recovered size: {size}"])
                elif order_status == 'canceled':
                    cur.execute("UPDATE active_trades SET status = 'CANCELLED' WHERE id = %s", (t_id,))
                    update_signal_status(cur, signal_id, 'Cancelled', closed=True)
                    conn.commit()
                    logger.info(f"🗑️ Safety Net: Marked {sym} as CANCELLED.")
            except Exception as e:
                logger.error(f"Safety Check Error {sym}: {e}")
    except Exception as e:
        logger.error(f"Global Safety Loop Error: {e}")
    finally:
        release_conn(conn)


def generate_daily_report():
    return reporting_generate_daily_report()


if __name__ == "__main__":
    logger.info(f"🟢 Starting Auto-Trader {MODE_TAG} (Hybrid Architecture)...")
    init_execution_db()
    if not IS_PAPER:
        ws = WebSocket(
            testnet=False,
            channel_type="private",
            api_key=CONFIG['api']['bybit_key'],
            api_secret=CONFIG['api']['bybit_secret'],
        )
        ws.execution_stream(callback=on_execution_update)
        ws.position_stream(callback=on_position_update)
        logger.info("🔌 WebSocket Connected.")
    else:
        logger.info(f"{MODE_TAG} Paper mode active: exchange order placement and private websocket handlers are disabled.")
    logger.info(f"🚀 Bot is running in {EXECUTION_MODE.upper()} mode. Monitoring {MAX_POSITIONS} Max Positions.")
    
    executor = ThreadPoolExecutor(max_workers=5)
    job_lock = threading.Lock()
    
    def run_threaded(job_func):
        if not job_lock.acquire(blocking=False):
            return
        def wrapped():
            try:
                job_func()
            finally:
                job_lock.release()
        try:
            executor.submit(wrapped)
        except Exception:
            job_lock.release()
            raise

    schedule.every(1).minutes.do(run_threaded, ingest_fresh_signals)
    schedule.every(5).seconds.do(run_threaded, execute_pending_orders)
    schedule.every(10).seconds.do(run_threaded, process_paper_trades)
    schedule.every(10).seconds.do(run_threaded, check_missed_tps)
    schedule.every().day.at("00:00").do(run_threaded, generate_daily_report)
    
    while True:
        update_heartbeat('autotrader', {'paused': is_paused(), 'mode': EXECUTION_MODE})
        schedule.run_pending()
        time.sleep(1)

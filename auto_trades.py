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
from modules.runtime_utils import retry_call

TARGET_LEVERAGE = 25
RISK_PERCENT = 0.01
MAX_POSITIONS = 40
TP_SPLIT = [0.30, 0.30, 0.40]
EXECUTION_MODE = str(CONFIG.get('execution', {}).get('mode', 'paper')).strip().lower()
IS_PAPER = EXECUTION_MODE == 'paper'
MODE_TAG = f"[{EXECUTION_MODE.upper()}]"
DEFAULT_PAPER_SETTINGS = {
    'initial_balance': 10000.0,
    'fee_rate': 0.0006,
    'slippage_bps': 5.0,
    'fill_on_touch': True,
    'conservative_intrabar': True,
}


def paper_settings():
    cfg = CONFIG.get('execution', {}).get('paper', {})
    merged = dict(DEFAULT_PAPER_SETTINGS)
    if isinstance(cfg, dict):
        merged.update(cfg)
    merged['initial_balance'] = float(merged.get('initial_balance', DEFAULT_PAPER_SETTINGS['initial_balance']))
    merged['fee_rate'] = float(merged.get('fee_rate', DEFAULT_PAPER_SETTINGS['fee_rate']))
    merged['slippage_bps'] = float(merged.get('slippage_bps', DEFAULT_PAPER_SETTINGS['slippage_bps']))
    merged['fill_on_touch'] = bool(merged.get('fill_on_touch', True))
    merged['conservative_intrabar'] = bool(merged.get('conservative_intrabar', True))
    return merged


def slippage_multiplier(is_entry=True):
    bps = paper_settings().get('slippage_bps', 0.0) / 10000.0
    return 1.0 + bps if is_entry else 1.0 - bps

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
    try:
        pos = ex_call('fetch_position', symbol, context=f'fetch position {symbol}')
        if isinstance(pos, dict):
            return pos
    except Exception as exc:
        logger.warning(f"fetch_position primary method failed for {symbol}: {exc}")

    try:
        positions = ex_call('fetch_positions', [symbol], context=f'fetch positions {symbol}')
        if isinstance(positions, list):
            for pos in positions:
                if pos.get('symbol') == symbol or pos.get('info', {}).get('symbol') == _bybit_symbol(symbol):
                    return pos
    except Exception as exc:
        logger.warning(f"fetch_positions fallback failed for {symbol}: {exc}")

    return {'contracts': 0, 'info': {}}


def get_position_contracts(position):
    if not isinstance(position, dict):
        return 0.0
    for key in ('contracts', 'contractSize', 'size'):
        value = position.get(key)
        if value not in (None, ''):
            try:
                return abs(float(value))
            except Exception:
                pass
    info = position.get('info', {}) if isinstance(position.get('info', {}), dict) else {}
    for key in ('size', 'qty', 'positionValue'):
        value = info.get(key)
        if value not in (None, ''):
            try:
                return abs(float(value))
            except Exception:
                pass
    return 0.0


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


def move_stop_to_entry(symbol, side, entry_price):
    positions = ex_call('fetch_positions', [symbol], context=f'fetch positions {symbol}')
    position_idx = None
    for pos in positions or []:
        if pos.get('symbol') == _bybit_symbol(symbol) and pos.get('side') == side:
            position_idx = int(pos.get('positionIdx', 0))
            break
    if position_idx is None:
        raise ValueError(f"No matching position found for {symbol} {side}")

    return retry_call(
        bybit_http.set_trading_stop,
        category="linear",
        symbol=_bybit_symbol(symbol),
        stopLoss=str(entry_price),
        positionIdx=position_idx,
        retries=3,
        base_delay=1.0,
        logger=logger,
        context=f"move stop to entry {symbol}",
    )


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


def trade_fee(notional):
    return float(notional) * paper_settings().get('fee_rate', 0.0)


def normalize_side(side):
    return str(side).strip().lower()


def apply_slippage(price, side, is_entry=True):
    multiplier = slippage_multiplier(is_entry=is_entry)
    side_norm = normalize_side(side)
    if is_entry:
        return float(price) * multiplier if side_norm == 'long' else float(price) * (2.0 - multiplier)
    return float(price) * multiplier if side_norm == 'long' else float(price) * (2.0 - multiplier)


def gross_pnl_for_exit(side, entry_price, exit_price, quantity):
    if normalize_side(side) == 'long':
        return (float(exit_price) - float(entry_price)) * float(quantity)
    return (float(entry_price) - float(exit_price)) * float(quantity)


def touch_triggered(side, low_price, high_price, target_price):
    return float(low_price) <= float(target_price) <= float(high_price)


def build_event_sequence(side, low_price, high_price, stop_loss, targets):
    target_events = [(idx + 1, target) for idx, target in enumerate(targets) if touch_triggered(side, low_price, high_price, target)]
    sl_hit = touch_triggered(side, low_price, high_price, stop_loss)
    if sl_hit and target_events and paper_settings().get('conservative_intrabar', True):
        return [('sl', stop_loss)]
    events = [('tp', idx, target) for idx, target in target_events]
    if sl_hit:
        events.append(('sl', stop_loss))
    return events


def fetch_active_trade(cur, trade_id):
    cur.execute(
        """
        SELECT id, signal_id, symbol, side, entry_price, sl_price, tp1, tp2, tp3,
               quantity, leverage, status, pnl, is_sl_moved,
               execution_mode, remaining_quantity, filled_quantity,
               entry_fill_price, exit_fill_price,
               realized_fees, realized_pnl_gross, realized_pnl_net,
               tp1_hit, tp2_hit, tp3_hit
        FROM active_trades
        WHERE id = %s
        """,
        (trade_id,),
    )
    return cur.fetchone()


def compute_paper_equity(cur):
    cur.execute("SELECT COALESCE(SUM(realized_pnl_net), 0) FROM active_trades WHERE execution_mode = 'paper' AND status = 'CLOSED'")
    realized = float(cur.fetchone()[0] or 0.0)
    return paper_settings().get('initial_balance', DEFAULT_PAPER_SETTINGS['initial_balance']) + realized


def close_paper_trade(cur, trade, exit_price, reason):
    (
        trade_id, signal_id, symbol, side, entry_price, sl_price, tp1, tp2, tp3,
        quantity, leverage, status, pnl, is_sl_moved,
        execution_mode_value, remaining_quantity, filled_quantity,
        entry_fill_price, exit_fill_price,
        realized_fees, realized_pnl_gross, realized_pnl_net,
        tp1_hit, tp2_hit, tp3_hit,
    ) = trade

    exit_exec_price = apply_slippage(exit_price, side, is_entry=False)
    remaining_qty = float(remaining_quantity or 0.0)
    entry_exec_price = float(entry_fill_price or entry_price)
    gross_add = gross_pnl_for_exit(side, entry_exec_price, exit_exec_price, remaining_qty)
    fee_add = trade_fee(abs(exit_exec_price * remaining_qty))
    total_fees = float(realized_fees or 0.0) + fee_add
    total_gross = float(realized_pnl_gross or 0.0) + gross_add
    total_net = total_gross - total_fees
    cur.execute(
        """
        UPDATE active_trades
        SET status = 'CLOSED',
            pnl = %s,
            realized_pnl_gross = %s,
            realized_pnl_net = %s,
            realized_fees = %s,
            exit_fill_price = %s,
            remaining_quantity = 0,
            updated_at = NOW()
        WHERE id = %s
        """,
        (total_net, total_gross, total_net, total_fees, exit_exec_price, trade_id),
    )
    update_signal_status(cur, signal_id, 'Closed', closed=True, exit_price=exit_exec_price)
    send_event_message(
        f"Paper Trade Closed: {symbol}",
        [
            f"Reason: {reason}",
            f"Exit: {exit_exec_price:.6f}",
            f"Gross PnL: {total_gross:.4f}",
            f"Fees: {total_fees:.4f}",
            f"Net PnL: {total_net:.4f}",
        ],
    )


def take_partial_profit(cur, trade, target_no, target_price):
    (
        trade_id, signal_id, symbol, side, entry_price, sl_price, tp1, tp2, tp3,
        quantity, leverage, status, pnl, is_sl_moved,
        execution_mode_value, remaining_quantity, filled_quantity,
        entry_fill_price, exit_fill_price,
        realized_fees, realized_pnl_gross, realized_pnl_net,
        tp1_hit, tp2_hit, tp3_hit,
    ) = trade

    remaining_qty = float(remaining_quantity or 0.0)
    split = TP_SPLIT[target_no - 1]
    qty_close = remaining_qty if target_no == 3 else min(remaining_qty, float(quantity) * split)
    if qty_close <= 0:
        return
    exec_price = apply_slippage(target_price, side, is_entry=False)
    entry_exec_price = float(entry_fill_price or entry_price)
    gross_add = gross_pnl_for_exit(side, entry_exec_price, exec_price, qty_close)
    fee_add = trade_fee(abs(exec_price * qty_close))
    total_fees = float(realized_fees or 0.0) + fee_add
    total_gross = float(realized_pnl_gross or 0.0) + gross_add
    total_net = total_gross - total_fees
    new_remaining = max(0.0, remaining_qty - qty_close)
    status_value = 'CLOSED' if new_remaining <= 1e-12 else 'OPEN_TPS_SET'
    fields = {
        1: ('tp1_hit', tp1_hit),
        2: ('tp2_hit', tp2_hit),
        3: ('tp3_hit', tp3_hit),
    }
    hit_field, hit_value = fields[target_no]
    if hit_value:
        return
    cur.execute(
        f"""
        UPDATE active_trades
        SET {hit_field} = TRUE,
            status = %s,
            pnl = %s,
            realized_pnl_gross = %s,
            realized_pnl_net = %s,
            realized_fees = %s,
            exit_fill_price = %s,
            remaining_quantity = %s,
            updated_at = NOW()
        WHERE id = %s
        """,
        (status_value, total_net, total_gross, total_net, total_fees, exec_price, new_remaining, trade_id),
    )
    send_event_message(
        f"Paper TP{target_no} Hit: {symbol}",
        [
            f"Target: {target_price}",
            f"Executed: {exec_price:.6f}",
            f"Qty Closed: {qty_close:.6f}",
            f"Net PnL: {total_net:.4f}",
        ],
    )
    if target_no == 1 and not is_sl_moved:
        cur.execute("UPDATE active_trades SET is_sl_moved = TRUE, sl_price = entry_price WHERE id = %s", (trade_id,))
        send_event_message(
            f"Paper SL Moved to Breakeven: {symbol}",
            [f"Entry: {entry_exec_price:.6f}", f"New SL: {entry_exec_price:.6f}"],
        )
    if new_remaining <= 1e-12:
        update_signal_status(cur, signal_id, 'Closed', closed=True, exit_price=exec_price)


def process_paper_trades():
    if not IS_PAPER or is_paused():
        return
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, signal_id, symbol, side, entry_price, sl_price, tp1, tp2, tp3,
                   quantity, leverage, status, pnl, is_sl_moved,
                   execution_mode, remaining_quantity, filled_quantity,
                   entry_fill_price, exit_fill_price,
                   realized_fees, realized_pnl_gross, realized_pnl_net,
                   tp1_hit, tp2_hit, tp3_hit
            FROM active_trades
            WHERE execution_mode = 'paper' AND status IN ('OPEN', 'OPEN_TPS_SET')
            ORDER BY created_at ASC
            """
        )
        trades = cur.fetchall()
        if not trades:
            update_heartbeat('autotrader', {'paused': False, 'mode': EXECUTION_MODE, 'paper_equity': compute_paper_equity(cur)})
            conn.commit()
            return

        for trade in trades:
            symbol = trade[2]
            side = trade[3]
            entry_price = float(trade[4])
            sl_price = float(trade[5])
            tp1 = float(trade[6])
            tp2 = float(trade[7])
            tp3 = float(trade[8])
            status = trade[11]
            candle = fetch_latest_candle(symbol, '1m')
            if not candle:
                continue
            low_price = candle['low']
            high_price = candle['high']

            if status == 'OPEN':
                if touch_triggered(side, low_price, high_price, entry_price):
                    filled_price = apply_slippage(entry_price, side, is_entry=True)
                    entry_fee = trade_fee(abs(filled_price * float(trade[9])))
                    cur.execute(
                        """
                        UPDATE active_trades
                        SET status = 'OPEN_TPS_SET',
                            entry_fill_price = %s,
                            filled_quantity = quantity,
                            remaining_quantity = quantity,
                            realized_fees = %s,
                            updated_at = NOW()
                        WHERE id = %s
                        """,
                        (filled_price, entry_fee, trade[0]),
                    )
                    update_signal_status(cur, trade[1], 'Active', entry_hit=True)
                    send_event_message(
                        f"Paper Entry Filled: {symbol}",
                        [f"Side: {side}", f"Entry: {filled_price:.6f}", f"Fee: {entry_fee:.4f}"],
                    )
                    conn.commit()
                    trade = fetch_active_trade(cur, trade[0])
                    if not trade:
                        continue
                    status = trade[11]

            if status != 'OPEN_TPS_SET':
                continue

            event_sequence = build_event_sequence(side, low_price, high_price, float(trade[5]), [float(trade[6]), float(trade[7]), float(trade[8])])
            for event in event_sequence:
                current_trade = fetch_active_trade(cur, trade[0])
                if not current_trade:
                    break
                if current_trade[11] == 'CLOSED':
                    break
                if event[0] == 'sl':
                    close_paper_trade(cur, current_trade, event[1], 'Stop Loss')
                    conn.commit()
                    break
                _, target_no, target_price = event
                hit_flags = {1: current_trade[22], 2: current_trade[23], 3: current_trade[24]}
                if not hit_flags[target_no]:
                    take_partial_profit(cur, current_trade, target_no, target_price)
                    conn.commit()

        update_heartbeat('autotrader', {'paused': False, 'mode': EXECUTION_MODE, 'paper_equity': compute_paper_equity(cur)})
        conn.commit()
    except Exception as exc:
        logger.error(f"{MODE_TAG} Paper processing error: {exc}")
    finally:
        release_conn(conn)


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


def place_split_tps(symbol, side, total_qty, tp1, tp2, tp3):
    try:
        side_str = str(side).lower()
        tp_side = 'sell' if side_str in ['buy', 'long'] else 'buy'
        
        tp1 = float(exchange.price_to_precision(symbol, tp1))
        tp2 = float(exchange.price_to_precision(symbol, tp2))
        tp3 = float(exchange.price_to_precision(symbol, tp3))

        qtys = [
            float(exchange.amount_to_precision(symbol, total_qty * TP_SPLIT[0])),
            float(exchange.amount_to_precision(symbol, total_qty * TP_SPLIT[1])),
            float(exchange.amount_to_precision(symbol, total_qty * TP_SPLIT[2])),
        ]
        current_sum = sum(qtys)
        if abs(current_sum - total_qty) > 1e-8:
            qtys[2] = float(exchange.amount_to_precision(symbol, max(0.0, qtys[2] + (total_qty - current_sum))))
            
        q1, q2, q3 = qtys
        params = {'reduceOnly': True}
        logger.info(f"⚡ Placing TPs for {symbol} ({tp_side.upper()}): {q1} | {q2} | {q3}")
        ex_call('create_order', symbol, 'limit', tp_side, q1, tp1, params, context=f'place tp1 {symbol}')
        ex_call('create_order', symbol, 'limit', tp_side, q2, tp2, params, context=f'place tp2 {symbol}')
        ex_call('create_order', symbol, 'limit', tp_side, q3, tp3, params, context=f'place tp3 {symbol}')
        return True
    except Exception as e:
        logger.error(f"⚠️ TP Placement Failed {symbol}: {e}")
        return False


def on_execution_update(message):
    try:
        data = message.get('data', [])
        for exec_item in data:
            symbol = exec_item['symbol']
            side = exec_item['side']
            exec_type = exec_item.get('execType')
            if exec_type != 'Trade':
                continue

            conn = get_conn()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT id, signal_id, tp1, tp2, tp3 FROM active_trades WHERE order_id = %s LIMIT 1",
                    (exec_item.get("orderId"),),
                )
                row = cur.fetchone()
                if not row:
                    continue

                t_id, signal_id, tp1, tp2, tp3 = row
                logger.info(f"⚡ WS: Entry Filled for {symbol}! Placing TPs...")
                pos = fetch_position_safe(symbol)
                current_size = get_position_contracts(pos)
                if current_size > 0 and place_split_tps(symbol, side, current_size, tp1, tp2, tp3):
                    cur.execute("UPDATE active_trades SET status = 'OPEN_TPS_SET', updated_at = NOW() WHERE id = %s", (t_id,))
                    update_signal_status(cur, signal_id, 'Active', entry_hit=True)
                    conn.commit()
                    send_event_message(f"Entry Filled: {symbol}", [f"Side: {side}", f"TPs placed for size {current_size}"])
            except Exception as e:
                logger.error(f"WS Exec Logic Error: {e}")
            finally:
                release_conn(conn)
    except Exception as e:
        logger.error(f"WS Payload Error: {e}")


def on_position_update(message):
    try:
        data = message.get('data', [])
        for pos in data:
            symbol = pos['symbol']
            size = float(pos['size'])
            mark_price = float(pos['markPrice'])
            side = pos['side']

            conn = get_conn()
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT id, signal_id, entry_price, tp1, is_sl_moved, status 
                    FROM active_trades 
                    WHERE symbol = %s AND side = %s AND status = 'OPEN_TPS_SET' 
                    ORDER BY id DESC 
                    LIMIT 1
                    """,
                    (symbol, side),
                )
                row = cur.fetchone()
                if not row:
                    continue

                t_id, signal_id, entry, tp1, sl_moved, status = row
                if size == 0:
                    logger.info(f"🏁 WS: {symbol} Position Closed. Fetching PnL...")
                    time.sleep(1)
                    try:
                        real_pnl = fetch_closed_pnl_safe(symbol)
                        cur.execute("UPDATE active_trades SET status = 'CLOSED', pnl = %s, updated_at = NOW() WHERE id = %s", (real_pnl, t_id))
                        update_signal_status(cur, signal_id, 'Closed', closed=True, exit_price=mark_price)
                        send_event_message(f"Position Closed: {symbol}", [f"Side: {side}", f"PnL: {real_pnl}"])
                    except Exception as e:
                        logger.warning(f"Could not fetch exact PnL for {symbol}: {e}")
                        cur.execute("UPDATE active_trades SET status = 'CLOSED', updated_at = NOW() WHERE id = %s", (t_id,))
                        update_signal_status(cur, signal_id, 'Closed', closed=True, exit_price=mark_price)
                    conn.commit()
                    continue

                hit_tp1 = (side == 'Buy' and mark_price >= float(tp1)) or (side == 'Sell' and mark_price <= float(tp1))
                if hit_tp1 and not sl_moved:
                    logger.info(f"♻️ WS: {symbol} hit TP1. Moving SL to Entry...")
                    try:
                        move_stop_to_entry(symbol, side, float(entry))
                        cur.execute("UPDATE active_trades SET is_sl_moved = TRUE WHERE id = %s", (t_id,))
                        conn.commit()
                        send_event_message(f"SL Moved to Breakeven: {symbol}", [f"Entry: {entry}", f"Current mark: {mark_price}"])
                    except Exception as sl_err:
                        logger.error(f"⚠️ Failed to move SL for {symbol}: {sl_err}")
            except Exception as e:
                logger.error(f"WS Position Logic Error for {symbol}: {e}")
            finally:
                release_conn(conn)
    except Exception as e:
        logger.error(f"WS Position Payload Error: {e}")


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
                entry = float(entry)
                is_better_price = (side == 'Long' and current_price <= entry) or (side == 'Short' and current_price >= entry)
                type_side = 'buy' if side == 'Long' else 'sell'
                idempotency_key = f"{sym}:{oid}:entry"
                params = {'stopLoss': float(exchange.price_to_precision(sym, sl)), 'orderLinkId': idempotency_key}
                qty = float(exchange.amount_to_precision(sym, qty))
                if qty <= 0:
                    raise ValueError(f"Rounded quantity too small for {sym}")

                if is_better_price:
                    logger.info(f"{MODE_TAG} ⚡ {sym}: Price Better ({current_price} vs {entry}). Executing MARKET...")
                    res = ex_call('create_order', sym, 'market', type_side, qty, None, params, context=f'market order {sym}')
                else:
                    logger.info(f"{MODE_TAG} ⏳ {sym}: Waiting ({current_price} vs {entry}). Placing LIMIT...")
                    res = ex_call('create_order', sym, 'limit', type_side, qty, float(exchange.price_to_precision(sym, entry)), params, context=f'limit order {sym}')

                if res and 'id' in res:
                    cur.execute("UPDATE active_trades SET order_id = %s, status = 'OPEN' WHERE id = %s", (res['id'], oid))
                    update_signal_status(cur, signal_id, 'Order Placed')
                    conn.commit()
                    logger.info(f"{MODE_TAG} ✅ Order Placed for {sym} (ID: {res['id']})")
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
                    size = get_position_contracts(pos)
                    if size > 0 and place_split_tps(sym, side, size, tp1, tp2, tp3):
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
    logger.info("📊 Generating Daily Report...")
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*), SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END),
                   SUM(pnl), MAX(pnl), MIN(pnl)
            FROM active_trades
            WHERE status = 'CLOSED' AND updated_at >= NOW() - INTERVAL '24 hours' AND execution_mode = %s
        """,
            (EXECUTION_MODE,),
        )
        row = cur.fetchone()
        if row and row[0] > 0:
            total, wins, losses, pnl, best, worst = row
            pnl = pnl if pnl else 0
            win_rate = (wins / total) * 100
            cur.execute("SELECT symbol FROM active_trades WHERE pnl = %s AND execution_mode = %s LIMIT 1", (best, EXECUTION_MODE))
            b_sym = cur.fetchone()
            best_sym = b_sym[0] if b_sym else '-'
            cur.execute("SELECT symbol FROM active_trades WHERE pnl = %s AND execution_mode = %s LIMIT 1", (worst, EXECUTION_MODE))
            w_sym = cur.fetchone()
            worst_sym = w_sym[0] if w_sym else '-'
            cur.execute(
                """
                INSERT INTO daily_reports (report_date, total_pnl, win_rate, total_wins, total_losses, total_trades, best_trade_symbol, best_trade_pnl, worst_trade_symbol, worst_trade_pnl, execution_mode)
                VALUES (CURRENT_DATE, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (report_date, execution_mode) DO UPDATE SET
                    total_pnl = EXCLUDED.total_pnl,
                    win_rate = EXCLUDED.win_rate,
                    total_wins = EXCLUDED.total_wins,
                    total_losses = EXCLUDED.total_losses,
                    total_trades = EXCLUDED.total_trades,
                    best_trade_symbol = EXCLUDED.best_trade_symbol,
                    best_trade_pnl = EXCLUDED.best_trade_pnl,
                    worst_trade_symbol = EXCLUDED.worst_trade_symbol,
                    worst_trade_pnl = EXCLUDED.worst_trade_pnl,
                    generated_at = CURRENT_TIMESTAMP
                """,
                (pnl, win_rate, wins, losses, total, best_sym, best, worst_sym, worst, EXECUTION_MODE),
            )
            conn.commit()
            logger.info(f"✅ Report Generated: ${pnl:.2f} ({wins}W/{losses}L)")
            report_lines = [f'Mode: {EXECUTION_MODE.upper()}', f'Trades: {total}', f'Wins: {wins}', f'Losses: {losses}', f'Win rate: {win_rate:.2f}%', f'PnL: {pnl}']
            if IS_PAPER:
                report_lines.append(f'Paper equity: {compute_paper_equity(cur):.2f}')
            send_event_message('Daily Trading Report', report_lines)
    except Exception as e:
        logger.error(f"Report Error: {e}")
    finally:
        release_conn(conn)


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

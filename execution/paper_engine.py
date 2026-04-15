import ccxt

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
    normalize_side,
    touch_triggered,
    trade_fee,
)

DEFAULT_PAPER_SETTINGS = {
    'initial_balance': 10000.0,
    'fee_rate': 0.0006,
    'slippage_bps': 5.0,
    'fill_on_touch': True,
    'conservative_intrabar': True,
}

EXECUTION_MODE = 'paper'
IS_PAPER = True
MODE_TAG = '[PAPER]'
logger = build_component_logger('PaperEngine', 'auto_trades.log', json_format=True, pii_mask=True)

exchange = ccxt.bybit({
    'apiKey': CONFIG['api'].get('bybit_key', ''),
    'secret': CONFIG['api'].get('bybit_secret', ''),
    'options': {'defaultType': 'swap', 'adjustForTimeDifference': True},
    'enableRateLimit': True,
})


def paper_settings():
    return merge_paper_settings(CONFIG.get('execution', {}).get('paper', {}))


def update_signal_status(cur, signal_id, status, *, entry_hit=False, closed=False, exit_price=None, execution_mode=EXECUTION_MODE):
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
    query = f"UPDATE trades SET {', '.join(fields)} WHERE id = %s"
    if execution_mode:
        query += " AND execution_mode = %s"
        params.append(execution_mode)
    cur.execute(query, tuple(params))


def fetch_latest_candle(symbol, timeframe='1m'):
    candles = exchange.fetch_ohlcv(symbol, timeframe, limit=1)
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
    _ = normalize_side(side)
    target_events = [
        (idx + 1, target)
        for idx, target in enumerate(targets)
        if touch_triggered(low_price, high_price, target)
    ]
    sl_hit = touch_triggered(low_price, high_price, stop_loss)
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
    split = [0.30, 0.30, 0.40][target_no - 1]
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

import ccxt
from pybit.unified_trading import HTTP

from modules.config_loader import CONFIG
from modules.database import get_conn, release_conn
from modules.logging_setup import build_component_logger
from modules.notifications import send_event_message

from modules.paper_trade_utils import normalize_side

TARGET_LEVERAGE = 25
RISK_PERCENT = 0.01
MAX_POSITIONS = 40
TP_SPLIT = [0.30, 0.30, 0.40]
MODE_TAG = "[LIVE]"

logger = build_component_logger('AutoTraderWS', 'auto_trades.log', json_format=True, pii_mask=True)

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
    return method(*args, **kwargs)


def _bybit_symbol(symbol):
    return symbol.replace('/', '')


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


def move_stop_to_entry(symbol, side, entry_price):
    positions = ex_call('fetch_positions', [symbol], context=f'fetch positions {symbol}')
    position_idx = None
    for pos in positions or []:
        if pos.get('symbol') == _bybit_symbol(symbol) and pos.get('side') == side:
            position_idx = int(pos.get('positionIdx', 0))
            break
    if position_idx is None:
        raise ValueError(f"No matching position found for {symbol} {side}")

    return bybit_http.set_trading_stop(
        category="linear",
        symbol=_bybit_symbol(symbol),
        stopLoss=str(entry_price),
        positionIdx=position_idx,
    )


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

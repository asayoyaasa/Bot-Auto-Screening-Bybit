from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from modules.paper_trade_utils import normalize_side, validate_quantity

TP_SPLIT = (0.30, 0.30, 0.40)
ORDER_SEARCH_LIMIT = 50


def _extract_order_link_id(order: Mapping[str, Any] | None) -> str | None:
    if not isinstance(order, Mapping):
        return None
    for key in ("orderLinkId", "clientOrderId", "clientOrderID"):
        value = order.get(key)
        if value not in (None, ""):
            return str(value)
    info = order.get("info", {}) if isinstance(order.get("info", {}), Mapping) else {}
    for key in ("orderLinkId", "clientOrderId", "clientOrderID"):
        value = info.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _resolve_order_by_link_id(exchange: Any, order_link_id: str, symbol: str | None = None) -> dict[str, Any] | None:
    for method_name in ("fetch_open_orders", "fetch_closed_orders", "fetch_orders"):
        method = getattr(exchange, method_name, None)
        if not callable(method):
            continue
        try:
            orders = method(symbol, ORDER_SEARCH_LIMIT) if symbol is not None else method(ORDER_SEARCH_LIMIT)
        except TypeError:
            try:
                orders = method(symbol) if symbol is not None else method()
            except Exception:
                continue
        except Exception:
            continue
        if not isinstance(orders, list):
            continue
        for order in orders:
            if _extract_order_link_id(order) == order_link_id:
                return order if isinstance(order, dict) else dict(order)
    return None


@dataclass(frozen=True, slots=True)
class OrderPlacementResult:
    symbol: str
    side: str
    quantity: float
    order_type: str
    price: float | None
    response: Any


def _symbol_to_bybit(symbol: str) -> str:
    text = str(symbol or "").strip()
    if not text:
        raise ValueError("symbol must not be empty")
    return text.replace("/", "").upper()


def _normalize_execution_side(side: str) -> str:
    normalized = normalize_side(side)
    if normalized not in {"long", "short", "buy", "sell"}:
        raise ValueError("side must be one of: long, short, buy, sell")
    return normalized


def _tp_exec_side(side: str) -> str:
    normalized = _normalize_execution_side(side)
    return "sell" if normalized in {"long", "buy"} else "buy"


def _safe_precision(exchange: Any, method_name: str, symbol: str, value: float) -> float:
    method = getattr(exchange, method_name)
    return float(method(symbol, value))


def build_entry_order_link_id(symbol: str, trade_id: Any) -> str:
    return f"{_symbol_to_bybit(symbol)}:{trade_id}:entry"


def build_tp_order_link_id(symbol: str, trade_id: Any, target_no: int) -> str:
    if target_no not in (1, 2, 3):
        raise ValueError("target_no must be one of: 1, 2, 3")
    return f"{_symbol_to_bybit(symbol)}:{trade_id}:tp{target_no}"


def resolve_position_contracts(position: Mapping[str, Any] | None) -> float:
    if not isinstance(position, Mapping):
        return 0.0
    for key in ("contracts", "contractSize", "size"):
        value = position.get(key)
        if value not in (None, ""):
            try:
                return abs(float(value))
            except Exception:
                pass
    info = position.get("info", {}) if isinstance(position.get("info", {}), Mapping) else {}
    for key in ("size", "qty", "positionValue"):
        value = info.get(key)
        if value not in (None, ""):
            try:
                return abs(float(value))
            except Exception:
                pass
    return 0.0


def _build_tp_quantities(exchange: Any, symbol: str, total_qty: float) -> list[float]:
    qty = validate_quantity(total_qty, field_name="total_qty")
    raw_qtys = [qty * split for split in TP_SPLIT]
    qtys = [float(exchange.amount_to_precision(symbol, item)) for item in raw_qtys]
    current_sum = sum(qtys)
    if abs(current_sum - qty) > 1e-8:
        qtys[2] = float(exchange.amount_to_precision(symbol, max(0.0, qtys[2] + (qty - current_sum))))
    if any(item <= 0 for item in qtys):
        raise ValueError("rounded TP quantities must be positive")
    return qtys


def place_split_tps(
    exchange: Any,
    symbol: str,
    side: str,
    total_qty: float,
    tp1: float,
    tp2: float,
    tp3: float,
    *,
    logger: Any,
    retry_call: Callable[..., Any],
    create_order: Callable[..., Any] | None = None,
    trade_id: Any | None = None,
) -> bool:
    try:
        symbol = _symbol_to_bybit(symbol)
        tp_side = _tp_exec_side(side)
        order_fn = create_order or getattr(exchange, "create_order")

        tp1 = _safe_precision(exchange, "price_to_precision", symbol, validate_quantity(tp1, field_name="tp1"))
        tp2 = _safe_precision(exchange, "price_to_precision", symbol, validate_quantity(tp2, field_name="tp2"))
        tp3 = _safe_precision(exchange, "price_to_precision", symbol, validate_quantity(tp3, field_name="tp3"))
        q1, q2, q3 = _build_tp_quantities(exchange, symbol, total_qty)

        params = {"reduceOnly": True}
        if trade_id is not None:
            params = dict(params)
        logger.info(f"⚡ Placing TPs for {symbol} ({tp_side.upper()}): {q1} | {q2} | {q3}")

        tp1_link_id = build_tp_order_link_id(symbol, trade_id, 1) if trade_id is not None else None
        tp2_link_id = build_tp_order_link_id(symbol, trade_id, 2) if trade_id is not None else None
        tp3_link_id = build_tp_order_link_id(symbol, trade_id, 3) if trade_id is not None else None

        retry_call(
            order_fn,
            symbol,
            "limit",
            tp_side,
            q1,
            tp1,
            {**params, "orderLinkId": tp1_link_id} if tp1_link_id is not None else params,
            retries=3,
            base_delay=1.0,
            logger=logger,
            context=f"place tp1 {symbol}",
            idempotency_key=tp1_link_id,
            resolve_idempotency_conflict=(lambda: _resolve_order_by_link_id(exchange, tp1_link_id, symbol)) if tp1_link_id is not None else None,
        )
        retry_call(
            order_fn,
            symbol,
            "limit",
            tp_side,
            q2,
            tp2,
            {**params, "orderLinkId": tp2_link_id} if tp2_link_id is not None else params,
            retries=3,
            base_delay=1.0,
            logger=logger,
            context=f"place tp2 {symbol}",
            idempotency_key=tp2_link_id,
            resolve_idempotency_conflict=(lambda: _resolve_order_by_link_id(exchange, tp2_link_id, symbol)) if tp2_link_id is not None else None,
        )
        retry_call(
            order_fn,
            symbol,
            "limit",
            tp_side,
            q3,
            tp3,
            {**params, "orderLinkId": tp3_link_id} if tp3_link_id is not None else params,
            retries=3,
            base_delay=1.0,
            logger=logger,
            context=f"place tp3 {symbol}",
            idempotency_key=tp3_link_id,
            resolve_idempotency_conflict=(lambda: _resolve_order_by_link_id(exchange, tp3_link_id, symbol)) if tp3_link_id is not None else None,
        )
        return True
    except Exception as exc:
        logger.error(f"⚠️ TP Placement Failed {symbol}: {exc}")
        return False


def move_stop_to_entry(
    exchange: Any,
    bybit_http: Any,
    symbol: str,
    side: str,
    entry_price: float,
    *,
    logger: Any,
    retry_call: Callable[..., Any],
    fetch_positions: Callable[..., Any] | None = None,
) -> Any:
    symbol = _symbol_to_bybit(symbol)
    positions_fn = fetch_positions or getattr(exchange, "fetch_positions")
    positions = positions_fn([symbol])
    position_idx = None
    side_candidates = {str(side).strip(), str(side).strip().lower(), str(side).strip().title()}
    for pos in positions or []:
        pos_symbol = str(pos.get("symbol") or pos.get("info", {}).get("symbol") or "").upper()
        pos_side = str(pos.get("side") or "").strip()
        if pos_symbol == symbol and (pos_side in side_candidates or pos_side.lower() in side_candidates):
            position_idx = int(pos.get("positionIdx", 0))
            break
    if position_idx is None:
        raise ValueError(f"No matching position found for {symbol} {side}")

    return retry_call(
        bybit_http.set_trading_stop,
        category="linear",
        symbol=symbol,
        stopLoss=str(validate_quantity(entry_price, field_name="entry_price")),
        positionIdx=position_idx,
        retries=3,
        base_delay=1.0,
        logger=logger,
        context=f"move stop to entry {symbol}",
    )


def place_entry_order(
    exchange: Any,
    symbol: str,
    side: str,
    quantity: float,
    entry_price: float,
    sl_price: float,
    current_price: float,
    *,
    logger: Any,
    retry_call: Callable[..., Any],
    order_link_id: str,
    create_order: Callable[..., Any] | None = None,
) -> OrderPlacementResult:
    symbol = _symbol_to_bybit(symbol)
    side_normalized = _normalize_execution_side(side)
    exec_side = "buy" if side_normalized in {"long", "buy"} else "sell"
    quantity = validate_quantity(quantity, field_name="quantity")
    entry_price = validate_quantity(entry_price, field_name="entry_price")
    sl_price = validate_quantity(sl_price, field_name="sl_price")
    current_price = validate_quantity(current_price, field_name="current_price")
    order_fn = create_order or getattr(exchange, "create_order")

    is_better_price = (side_normalized in {"long", "buy"} and current_price <= entry_price) or (
        side_normalized in {"short", "sell"} and current_price >= entry_price
    )
    price = None if is_better_price else _safe_precision(exchange, "price_to_precision", symbol, entry_price)
    qty = float(exchange.amount_to_precision(symbol, quantity))
    if qty <= 0:
        raise ValueError(f"Rounded quantity too small for {symbol}")

    params = {
        "stopLoss": _safe_precision(exchange, "price_to_precision", symbol, sl_price),
        "orderLinkId": order_link_id,
    }

    def resolve_existing_order() -> dict[str, Any] | None:
        return _resolve_order_by_link_id(exchange, order_link_id, symbol)

    if is_better_price:
        logger.info(f"⚡ {symbol}: Price Better ({current_price} vs {entry_price}). Executing MARKET...")
        response = retry_call(
            order_fn,
            symbol,
            "market",
            exec_side,
            qty,
            None,
            params,
            retries=3,
            base_delay=1.0,
            logger=logger,
            context=f"market order {symbol}",
            idempotency_key=order_link_id,
            resolve_idempotency_conflict=resolve_existing_order,
        )
        order_type = "market"
    else:
        logger.info(f"⏳ {symbol}: Waiting ({current_price} vs {entry_price}). Placing LIMIT...")
        response = retry_call(
            order_fn,
            symbol,
            "limit",
            exec_side,
            qty,
            price,
            params,
            retries=3,
            base_delay=1.0,
            logger=logger,
            context=f"limit order {symbol}",
            idempotency_key=order_link_id,
            resolve_idempotency_conflict=resolve_existing_order,
        )
        order_type = "limit"

    return OrderPlacementResult(
        symbol=symbol,
        side=exec_side,
        quantity=qty,
        order_type=order_type,
        price=price,
        response=response,
    )


def fetch_position_safe(exchange: Any, symbol: str, *, logger: Any, retry_call: Callable[..., Any]) -> dict[str, Any]:
    symbol = _symbol_to_bybit(symbol)
    try:
        pos = retry_call(exchange.fetch_position, symbol, retries=3, base_delay=1.0, logger=logger, context=f"fetch position {symbol}")
        if isinstance(pos, dict):
            return pos
    except Exception as exc:
        logger.warning(f"fetch_position primary method failed for {symbol}: {exc}")

    try:
        positions = retry_call(exchange.fetch_positions, [symbol], retries=3, base_delay=1.0, logger=logger, context=f"fetch positions {symbol}")
        if isinstance(positions, list):
            for pos in positions:
                if str(pos.get("symbol")) == symbol or str(pos.get("info", {}).get("symbol")) == symbol:
                    return pos
    except Exception as exc:
        logger.warning(f"fetch_positions fallback failed for {symbol}: {exc}")

    return {"contracts": 0, "info": {}}


__all__ = [
    "OrderPlacementResult",
    "build_entry_order_link_id",
    "build_tp_order_link_id",
    "fetch_position_safe",
    "move_stop_to_entry",
    "place_entry_order",
    "place_split_tps",
    "resolve_position_contracts",
]

import pytest

from execution.order_manager import (
    build_entry_order_link_id,
    build_tp_order_link_id,
    move_stop_to_entry,
    place_entry_order,
    place_split_tps,
    resolve_position_contracts,
)


class FakeExchange:
    def __init__(self):
        self.orders = []
        self.positions = []
        self.open_orders = []
        self.closed_orders = []

    def amount_to_precision(self, symbol, value):
        return f"{float(value):.4f}"

    def price_to_precision(self, symbol, value):
        return f"{float(value):.2f}"

    def create_order(self, symbol, order_type, side, amount, price=None, params=None):
        payload = {
            "id": f"{symbol}-{order_type}-{side}-{len(self.orders)+1}",
            "symbol": symbol,
            "type": order_type,
            "side": side,
            "amount": amount,
            "price": price,
            "params": params or {},
        }
        self.orders.append(payload)
        return payload

    def fetch_positions(self, symbols):
        return self.positions

    def fetch_position(self, symbol):
        return self.positions[0] if self.positions else {"contracts": 0}

    def fetch_open_orders(self, symbol=None, limit=None):
        return self.open_orders

    def fetch_closed_orders(self, symbol=None, limit=None):
        return self.closed_orders

    def fetch_orders(self, symbol=None, limit=None):
        return list(self.open_orders) + list(self.closed_orders)


class FakeBybitHTTP:
    def __init__(self):
        self.calls = []

    def set_trading_stop(self, **kwargs):
        self.calls.append(kwargs)
        return {"retCode": 0, "result": kwargs}


class Logger:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(("info", message))

    def warning(self, message):
        self.messages.append(("warning", message))

    def error(self, message):
        self.messages.append(("error", message))


def retry_call(func, *args, **kwargs):
    resolve = kwargs.pop("resolve_idempotency_conflict", None)
    idempotency_key = kwargs.pop("idempotency_key", None)
    kwargs.pop("retries", None)
    kwargs.pop("base_delay", None)
    kwargs.pop("logger", None)
    kwargs.pop("context", None)
    try:
        return func(*args, **kwargs)
    except Exception as exc:
        text = " ".join(str(part) for part in getattr(exc, "args", ()) if part is not None).lower()
        if idempotency_key is not None and any(marker in text for marker in ("already exists", "duplicate", "orderlinkid", "order link id", "client order id", "conflict")):
            if callable(resolve):
                resolved = resolve()
                if resolved is not None:
                    return resolved
        raise


def test_order_link_ids_are_idempotent_and_symbol_normalized():
    assert build_entry_order_link_id("btc/usdt", 17) == "BTCUSDT:17:entry"
    assert build_tp_order_link_id("btc/usdt", 17, 2) == "BTCUSDT:17:tp2"


def test_place_entry_order_uses_market_when_price_is_better_for_long():
    exchange = FakeExchange()
    logger = Logger()

    result = place_entry_order(
        exchange,
        "BTC/USDT",
        "Long",
        1.23456,
        100.0,
        95.0,
        99.0,
        logger=logger,
        retry_call=retry_call,
        order_link_id="BTCUSDT:1:entry",
    )

    assert result.order_type == "market"
    assert result.side == "buy"
    assert result.price is None
    assert exchange.orders[0]["params"]["orderLinkId"] == "BTCUSDT:1:entry"
    assert exchange.orders[0]["params"]["stopLoss"] == 95.0


def test_place_split_tps_places_three_reduce_only_orders_with_trade_id():
    exchange = FakeExchange()
    logger = Logger()

    ok = place_split_tps(
        exchange,
        "BTC/USDT",
        "Long",
        10.0,
        110.0,
        120.0,
        130.0,
        logger=logger,
        retry_call=retry_call,
        trade_id=99,
    )

    assert ok is True
    assert len(exchange.orders) == 3
    assert exchange.orders[0]["params"]["reduceOnly"] is True
    assert exchange.orders[0]["params"]["orderLinkId"] == "BTCUSDT:99:tp1"
    assert exchange.orders[1]["params"]["orderLinkId"] == "BTCUSDT:99:tp2"
    assert exchange.orders[2]["params"]["orderLinkId"] == "BTCUSDT:99:tp3"


def test_place_split_tps_without_trade_id_still_places_orders():
    exchange = FakeExchange()
    logger = Logger()

    ok = place_split_tps(
        exchange,
        "BTC/USDT",
        "Long",
        10.0,
        110.0,
        120.0,
        130.0,
        logger=logger,
        retry_call=retry_call,
    )

    assert ok is True
    assert len(exchange.orders) == 3
    assert all("orderLinkId" not in order["params"] for order in exchange.orders)


def test_place_entry_order_resolves_existing_order_on_idempotency_conflict_without_duplicate_create():
    exchange = FakeExchange()
    exchange.open_orders = [{"id": "existing", "orderLinkId": "BTCUSDT:7:entry", "status": "open"}]
    logger = Logger()
    create_calls = {"count": 0}

    def create_order(*args, **kwargs):
        create_calls["count"] += 1
        raise ValueError("OrderLinkId already exists")

    result = place_entry_order(
        exchange,
        "BTC/USDT",
        "long",
        1.0,
        100.0,
        95.0,
        101.0,
        logger=logger,
        retry_call=retry_call,
        order_link_id="BTCUSDT:7:entry",
        create_order=create_order,
    )

    assert create_calls["count"] == 1
    assert result.response["id"] == "existing"
    assert exchange.orders == []


def test_place_split_tps_rejects_rounding_to_zero_quantities():
    class TinyQtyExchange(FakeExchange):
        def amount_to_precision(self, symbol, value):
            return f"{float(value):.2f}"

    exchange = TinyQtyExchange()
    logger = Logger()

    ok = place_split_tps(
        exchange,
        "BTC/USDT",
        "long",
        0.01,
        110.0,
        120.0,
        130.0,
        logger=logger,
        retry_call=retry_call,
        trade_id=1,
    )

    assert ok is False
    assert exchange.orders == []


def test_move_stop_to_entry_targets_matching_position_idx():
    exchange = FakeExchange()
    exchange.positions = [
        {"symbol": "ETHUSDT", "side": "Buy", "positionIdx": 1},
        {"symbol": "BTCUSDT", "side": "Buy", "positionIdx": 2},
    ]
    http = FakeBybitHTTP()
    logger = Logger()

    res = move_stop_to_entry(exchange, http, "BTC/USDT", "Buy", 105.5, logger=logger, retry_call=retry_call)

    assert res["retCode"] == 0
    assert http.calls[0]["symbol"] == "BTCUSDT"
    assert http.calls[0]["stopLoss"] == "105.5"
    assert http.calls[0]["positionIdx"] == 2


def test_resolve_position_contracts_handles_multiple_shapes():
    assert resolve_position_contracts({"contracts": "3.5"}) == pytest.approx(3.5)
    assert resolve_position_contracts({"info": {"size": "7"}}) == pytest.approx(7.0)

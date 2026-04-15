import pytest

from modules.execution_types import ExecutionEvent, OrderIntent


def test_order_intent_normalizes_and_serializes_round_trip():
    intent = OrderIntent(
        symbol="btc/usdt",
        side=" Long ",
        quantity="2",
        order_type="Limit",
        price="101.5",
        execution_mode=" PAPER ",
        client_order_id="  abc-123  ",
        metadata={"note": "test"},
    )

    assert intent.symbol == "BTC/USDT"
    assert intent.side == "long"
    assert intent.quantity == 2.0
    assert intent.order_type == "limit"
    assert intent.price == 101.5
    assert intent.execution_mode == "paper"
    assert intent.client_order_id == "abc-123"
    assert intent.metadata == {"note": "test"}

    payload = intent.to_dict()
    assert payload["symbol"] == "BTC/USDT"
    assert OrderIntent.from_dict(payload) == intent


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"symbol": "", "side": "long", "quantity": 1}, "symbol must not be empty"),
        ({"symbol": "BTC/USDT", "side": "sideways", "quantity": 1}, "side must be one of: long, short"),
        ({"symbol": "BTC/USDT", "side": "long", "quantity": 0}, "quantity must be > 0"),
        ({"symbol": "BTC/USDT", "side": "long", "quantity": 1, "order_type": "stop"}, "order_type must be one of"),
        ({"symbol": "BTC/USDT", "side": "long", "quantity": 1, "execution_mode": "demo"}, "execution.mode must be one of"),
    ],
)
def test_order_intent_validation_errors(kwargs, match):
    with pytest.raises(ValueError, match=match):
        OrderIntent(**kwargs)


def test_execution_event_normalizes_and_serializes_round_trip():
    intent = OrderIntent(symbol="ETH/USDT", side="short", quantity=3, price=2500, order_type="limit")
    event = ExecutionEvent(
        intent=intent,
        status=" Filled ",
        filled_quantity="3",
        average_fill_price="2498.5",
        fees="1.25",
        message="ok",
        raw={"exchange": "bybit"},
    )

    assert event.status == "filled"
    assert event.filled_quantity == 3.0
    assert event.average_fill_price == 2498.5
    assert event.fees == 1.25
    assert event.message == "ok"
    assert event.raw == {"exchange": "bybit"}

    payload = event.to_dict()
    assert payload["intent"]["symbol"] == "ETH/USDT"
    assert ExecutionEvent.from_dict(payload) == event


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"intent": "not-an-intent", "status": "filled"}, "intent must be an OrderIntent"),
        ({"intent": OrderIntent(symbol="BTC/USDT", side="long", quantity=1), "status": "unknown"}, "status must be one of"),
        ({"intent": OrderIntent(symbol="BTC/USDT", side="long", quantity=1), "status": "filled", "filled_quantity": 2}, "filled_quantity cannot exceed intent.quantity"),
        ({"intent": OrderIntent(symbol="BTC/USDT", side="long", quantity=1), "status": "filled", "fees": -1}, "fees must be >= 0"),
    ],
)
def test_execution_event_validation_errors(kwargs, match):
    with pytest.raises(ValueError, match=match):
        ExecutionEvent(**kwargs)

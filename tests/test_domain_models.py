import pytest

from modules.domain import ActiveTrade, TradeSignal


def test_trade_signal_normalizes_symbol_side_and_execution_mode():
    signal = TradeSignal(
        symbol="  btc/usdt  ",
        side=" Long ",
        entry_price="100",
        sl_price="95",
        tp1=105,
        tp2=110,
        tp3=120,
        quantity="2.5",
        execution_mode=" PAPER ",
    )

    assert signal.symbol == "BTC/USDT"
    assert signal.side == "long"
    assert signal.execution_mode == "paper"
    assert signal.entry_price == 100.0
    assert signal.quantity == 2.5


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"symbol": "", "side": "long", "entry_price": 100, "sl_price": 95, "tp1": 105, "tp2": 110, "tp3": 120}, "symbol must not be empty"),
        ({"symbol": "BTCUSDT", "side": "flat", "entry_price": 100, "sl_price": 95, "tp1": 105, "tp2": 110, "tp3": 120}, "side must be 'long' or 'short'"),
        ({"symbol": "BTCUSDT", "side": "long", "entry_price": 100, "sl_price": 101, "tp1": 105, "tp2": 110, "tp3": 120}, "long signals must satisfy sl < entry < tp1 < tp2 < tp3"),
        ({"symbol": "BTCUSDT", "side": "short", "entry_price": 100, "sl_price": 99, "tp1": 95, "tp2": 90, "tp3": 85}, "short signals must satisfy sl > entry > tp1 > tp2 > tp3"),
        ({"symbol": "BTCUSDT", "side": "long", "entry_price": 100, "sl_price": 95, "tp1": 105, "tp2": 110, "tp3": 120, "execution_mode": "demo"}, "execution.mode must be one of: paper, live"),
    ],
)

def test_trade_signal_validation_errors(kwargs, message):
    with pytest.raises(ValueError, match=message):
        TradeSignal(**kwargs)


def test_active_trade_from_signal_preserves_normalized_fields_and_defaults():
    signal = TradeSignal(
        symbol="ethusdt",
        side="short",
        entry_price=200,
        sl_price=210,
        tp1=190,
        tp2=180,
        tp3=170,
        quantity=3,
    )

    trade = ActiveTrade.from_signal(signal, trade_id=7, signal_id=11)

    assert trade.id == 7
    assert trade.signal_id == 11
    assert trade.symbol == "ETHUSDT"
    assert trade.side == "short"
    assert trade.status == "PENDING"
    assert trade.execution_mode == "paper"
    assert trade.quantity == 3.0
    assert trade.remaining_quantity == 3.0
    assert trade.filled_quantity == 0.0


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"symbol": "BTCUSDT", "side": "long", "entry_price": 100, "sl_price": 95, "tp1": 105, "tp2": 110, "tp3": 120, "quantity": 0}, "quantity must be > 0"),
        ({"symbol": "BTCUSDT", "side": "long", "entry_price": 100, "sl_price": 95, "tp1": 105, "tp2": 110, "tp3": 120, "quantity": 1, "remaining_quantity": 2}, "remaining_quantity cannot exceed quantity"),
        ({"symbol": "BTCUSDT", "side": "long", "entry_price": 100, "sl_price": 95, "tp1": 105, "tp2": 110, "tp3": 120, "quantity": 1, "filled_quantity": 2}, "filled_quantity cannot exceed quantity"),
        ({"symbol": "BTCUSDT", "side": "long", "entry_price": 100, "sl_price": 95, "tp1": 105, "tp2": 110, "tp3": 120, "quantity": 1, "leverage": 0}, "leverage must be > 0"),
    ],
)

def test_active_trade_validation_errors(kwargs, message):
    with pytest.raises(ValueError, match=message):
        ActiveTrade(**kwargs)

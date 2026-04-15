import pytest

from modules.paper_trade_utils import (
    DEFAULT_PAPER_SETTINGS,
    apply_slippage,
    build_paper_event_sequence,
    gross_pnl_for_exit,
    merge_paper_settings,
    trade_fee,
)


def test_merge_paper_settings_uses_defaults_and_overrides_selected_values():
    merged = merge_paper_settings({"fee_rate": 0.001, "slippage_bps": 12, "fill_on_touch": False})

    assert merged["initial_balance"] == DEFAULT_PAPER_SETTINGS["initial_balance"]
    assert merged["fee_rate"] == 0.001
    assert merged["slippage_bps"] == 12.0
    assert merged["fill_on_touch"] is False
    assert merged["conservative_intrabar"] is True


def test_build_paper_event_sequence_conservative_long_prefers_stop_loss_when_sl_and_tp_hit_same_candle():
    events = build_paper_event_sequence(
        side="Long",
        low_price=94,
        high_price=106,
        stop_loss=95,
        targets=[105, 110, 115],
        conservative=True,
    )

    assert events == ["sl"]


def test_build_paper_event_sequence_conservative_short_prefers_stop_loss_when_sl_and_tp_hit_same_candle():
    events = build_paper_event_sequence(
        side="Short",
        low_price=94,
        high_price=106,
        stop_loss=105,
        targets=[95, 90, 85],
        conservative=True,
    )

    assert events == ["sl"]


def test_build_paper_event_sequence_orders_profit_targets_when_no_stop_loss_conflict():
    events = build_paper_event_sequence(
        side="Long",
        low_price=100,
        high_price=120,
        stop_loss=95,
        targets=[105, 110, 115],
        conservative=True,
    )

    assert events == ["tp1", "tp2", "tp3"]


def test_apply_slippage_is_adverse_for_long_entries_and_long_exits():
    settings = merge_paper_settings({"slippage_bps": 10})

    assert apply_slippage(100.0, "Long", is_entry=True, settings=settings) == pytest.approx(100.1)
    assert apply_slippage(100.0, "Long", is_entry=False, settings=settings) == pytest.approx(99.9)


def test_apply_slippage_is_adverse_for_short_entries_and_short_exits():
    settings = merge_paper_settings({"slippage_bps": 10})

    assert apply_slippage(100.0, "Short", is_entry=True, settings=settings) == pytest.approx(99.9)
    assert apply_slippage(100.0, "Short", is_entry=False, settings=settings) == pytest.approx(100.1)


def test_fee_and_gross_pnl_math_for_long_trade_are_correct():
    assert trade_fee(1000, fee_rate=0.0006) == pytest.approx(0.6)
    assert gross_pnl_for_exit("Long", 100, 110, 2) == pytest.approx(20.0)


def test_fee_and_gross_pnl_math_for_short_trade_are_correct():
    assert trade_fee(1000, fee_rate=0.0006) == pytest.approx(0.6)
    assert gross_pnl_for_exit("Short", 100, 90, 2) == pytest.approx(20.0)

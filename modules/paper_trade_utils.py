from math import isfinite

DEFAULT_PAPER_SETTINGS = {
    'initial_balance': 10000.0,
    'fee_rate': 0.0006,
    'slippage_bps': 5.0,
    'fill_on_touch': True,
    'conservative_intrabar': True,
}


def normalize_side(side):
    return str(side).strip().lower()


def normalize_execution_mode(mode, *, field_name='execution.mode', supported=('paper', 'live')):
    normalized = str(mode or '').strip().lower()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    if normalized not in supported:
        allowed = ', '.join(supported)
        raise ValueError(f"{field_name} must be one of: {allowed}")
    return normalized


def validate_quantity(quantity, *, field_name='quantity', allow_zero=False):
    try:
        value = float(quantity)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number, got {quantity!r}") from exc
    if not isfinite(value):
        raise ValueError(f"{field_name} must be finite, got {quantity!r}")
    if allow_zero:
        if value < 0:
            raise ValueError(f"{field_name} must be >= 0, got {value}")
    else:
        if value <= 0:
            raise ValueError(f"{field_name} must be > 0, got {value}")
    return value


def merge_paper_settings(cfg=None):
    merged = dict(DEFAULT_PAPER_SETTINGS)
    if isinstance(cfg, dict):
        merged.update(cfg)
    merged['initial_balance'] = float(merged.get('initial_balance', DEFAULT_PAPER_SETTINGS['initial_balance']))
    merged['fee_rate'] = float(merged.get('fee_rate', DEFAULT_PAPER_SETTINGS['fee_rate']))
    merged['slippage_bps'] = float(merged.get('slippage_bps', DEFAULT_PAPER_SETTINGS['slippage_bps']))
    merged['fill_on_touch'] = bool(merged.get('fill_on_touch', DEFAULT_PAPER_SETTINGS['fill_on_touch']))
    merged['conservative_intrabar'] = bool(merged.get('conservative_intrabar', DEFAULT_PAPER_SETTINGS['conservative_intrabar']))
    return merged


def slippage_multiplier(settings=None, is_entry=True):
    merged = merge_paper_settings(settings)
    bps = merged.get('slippage_bps', 0.0) / 10000.0
    return 1.0 + bps if is_entry else 1.0 - bps


def apply_slippage(price, side, is_entry=True, settings=None):
    multiplier = slippage_multiplier(settings=settings, is_entry=is_entry)
    side_norm = normalize_side(side)
    if is_entry:
        return float(price) * multiplier if side_norm == 'long' else float(price) * (2.0 - multiplier)
    return float(price) * multiplier if side_norm == 'long' else float(price) * (2.0 - multiplier)


def trade_fee(notional, fee_rate=None):
    rate = merge_paper_settings({'fee_rate': fee_rate})['fee_rate'] if fee_rate is not None else DEFAULT_PAPER_SETTINGS['fee_rate']
    return float(notional) * float(rate)


def gross_pnl_for_exit(side, entry_price, exit_price, quantity):
    if normalize_side(side) == 'long':
        return (float(exit_price) - float(entry_price)) * float(quantity)
    return (float(entry_price) - float(exit_price)) * float(quantity)


def touch_triggered(low_price, high_price, target_price):
    return float(low_price) <= float(target_price) <= float(high_price)


def build_paper_event_sequence(side, low_price, high_price, stop_loss, targets, conservative=True):
    touched_targets = [f'tp{i+1}' for i, target in enumerate(targets) if touch_triggered(low_price, high_price, target)]
    sl_hit = touch_triggered(low_price, high_price, stop_loss)
    if sl_hit and touched_targets and conservative:
        return ['sl']
    events = []
    if normalize_side(side) == 'long':
        events.extend(touched_targets)
    else:
        events.extend(touched_targets)
    if sl_hit:
        events.append('sl')
    return events

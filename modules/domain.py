from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from modules.paper_trade_utils import normalize_execution_mode, normalize_side, validate_quantity


def normalize_symbol(symbol: str) -> str:
    normalized = str(symbol or "").strip().upper().replace(" ", "")
    if not normalized:
        raise ValueError("symbol must not be empty")
    return normalized


def _normalize_price(value, *, field_name: str) -> float:
    try:
        price = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number, got {value!r}") from exc
    if price <= 0:
        raise ValueError(f"{field_name} must be > 0, got {price}")
    return price


@dataclass(frozen=True)
class TradeSignal:
    symbol: str
    side: str
    entry_price: float
    sl_price: float
    tp1: float
    tp2: float
    tp3: float
    quantity: Optional[float] = None
    execution_mode: str = "paper"

    def __post_init__(self):
        symbol = normalize_symbol(self.symbol)
        side = normalize_side(self.side)
        execution_mode = normalize_execution_mode(self.execution_mode)
        entry_price = _normalize_price(self.entry_price, field_name="entry_price")
        sl_price = _normalize_price(self.sl_price, field_name="sl_price")
        tp1 = _normalize_price(self.tp1, field_name="tp1")
        tp2 = _normalize_price(self.tp2, field_name="tp2")
        tp3 = _normalize_price(self.tp3, field_name="tp3")
        quantity = None if self.quantity is None else validate_quantity(self.quantity, field_name="quantity")

        if side not in {"long", "short"}:
            raise ValueError("side must be 'long' or 'short'")

        if side == "long":
            if not (sl_price < entry_price < tp1 < tp2 < tp3):
                raise ValueError("long signals must satisfy sl < entry < tp1 < tp2 < tp3")
        else:
            if not (sl_price > entry_price > tp1 > tp2 > tp3):
                raise ValueError("short signals must satisfy sl > entry > tp1 > tp2 > tp3")

        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "entry_price", entry_price)
        object.__setattr__(self, "sl_price", sl_price)
        object.__setattr__(self, "tp1", tp1)
        object.__setattr__(self, "tp2", tp2)
        object.__setattr__(self, "tp3", tp3)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "execution_mode", execution_mode)


@dataclass(frozen=True)
class ActiveTrade:
    symbol: str
    side: str
    entry_price: float
    sl_price: float
    tp1: float
    tp2: float
    tp3: float
    quantity: float
    leverage: int = 1
    status: str = "PENDING"
    execution_mode: str = "paper"
    remaining_quantity: Optional[float] = None
    filled_quantity: float = 0.0
    signal_id: Optional[int] = None
    id: Optional[int] = None

    def __post_init__(self):
        symbol = normalize_symbol(self.symbol)
        side = normalize_side(self.side)
        execution_mode = normalize_execution_mode(self.execution_mode)
        entry_price = _normalize_price(self.entry_price, field_name="entry_price")
        sl_price = _normalize_price(self.sl_price, field_name="sl_price")
        tp1 = _normalize_price(self.tp1, field_name="tp1")
        tp2 = _normalize_price(self.tp2, field_name="tp2")
        tp3 = _normalize_price(self.tp3, field_name="tp3")
        quantity = validate_quantity(self.quantity, field_name="quantity")
        filled_quantity = validate_quantity(self.filled_quantity, field_name="filled_quantity", allow_zero=True)
        remaining_quantity = (
            quantity if self.remaining_quantity is None else validate_quantity(self.remaining_quantity, field_name="remaining_quantity", allow_zero=True)
        )

        leverage = int(self.leverage)
        if leverage <= 0:
            raise ValueError("leverage must be > 0")
        if side not in {"long", "short"}:
            raise ValueError("side must be 'long' or 'short'")

        if side == "long":
            if not (sl_price < entry_price < tp1 < tp2 < tp3):
                raise ValueError("long trades must satisfy sl < entry < tp1 < tp2 < tp3")
        else:
            if not (sl_price > entry_price > tp1 > tp2 > tp3):
                raise ValueError("short trades must satisfy sl > entry > tp1 > tp2 > tp3")

        if remaining_quantity > quantity:
            raise ValueError("remaining_quantity cannot exceed quantity")
        if filled_quantity > quantity:
            raise ValueError("filled_quantity cannot exceed quantity")

        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "entry_price", entry_price)
        object.__setattr__(self, "sl_price", sl_price)
        object.__setattr__(self, "tp1", tp1)
        object.__setattr__(self, "tp2", tp2)
        object.__setattr__(self, "tp3", tp3)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "leverage", leverage)
        object.__setattr__(self, "execution_mode", execution_mode)
        object.__setattr__(self, "remaining_quantity", remaining_quantity)
        object.__setattr__(self, "filled_quantity", filled_quantity)

    @classmethod
    def from_signal(
        cls,
        signal: TradeSignal,
        *,
        signal_id: Optional[int] = None,
        trade_id: Optional[int] = None,
        status: str = "PENDING",
        remaining_quantity: Optional[float] = None,
        filled_quantity: float = 0.0,
        leverage: int = 1,
    ) -> "ActiveTrade":
        quantity = signal.quantity if signal.quantity is not None else remaining_quantity
        if quantity is None:
            raise ValueError("signal quantity is required to create an ActiveTrade")
        return cls(
            id=trade_id,
            signal_id=signal_id,
            symbol=signal.symbol,
            side=signal.side,
            entry_price=signal.entry_price,
            sl_price=signal.sl_price,
            tp1=signal.tp1,
            tp2=signal.tp2,
            tp3=signal.tp3,
            quantity=quantity,
            leverage=leverage,
            status=status,
            execution_mode=signal.execution_mode,
            remaining_quantity=remaining_quantity,
            filled_quantity=filled_quantity,
        )

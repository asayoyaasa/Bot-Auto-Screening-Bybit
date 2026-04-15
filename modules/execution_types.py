from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from modules.paper_trade_utils import normalize_execution_mode, normalize_side, validate_quantity

ALLOWED_ORDER_TYPES = {"market", "limit"}
ALLOWED_EVENT_STATUSES = {"submitted", "filled", "partially_filled", "canceled", "rejected"}


def _coerce_str(value: Any, *, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} must not be empty")
    return text


@dataclass(frozen=True, slots=True)
class OrderIntent:
    symbol: str
    side: str
    quantity: float
    order_type: str = "market"
    price: float | None = None
    execution_mode: str = "paper"
    client_order_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        symbol = _coerce_str(self.symbol, field_name="symbol").upper()
        side = normalize_side(self.side)
        if side not in {"long", "short"}:
            raise ValueError("side must be one of: long, short")
        quantity = validate_quantity(self.quantity, field_name="quantity")
        order_type = _coerce_str(self.order_type, field_name="order_type").lower()
        if order_type not in ALLOWED_ORDER_TYPES:
            allowed = ", ".join(sorted(ALLOWED_ORDER_TYPES))
            raise ValueError(f"order_type must be one of: {allowed}")
        price = None if self.price is None else validate_quantity(self.price, field_name="price")
        execution_mode = normalize_execution_mode(self.execution_mode)
        client_order_id = None if self.client_order_id is None else _coerce_str(self.client_order_id, field_name="client_order_id")
        metadata = dict(self.metadata) if self.metadata is not None else {}

        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "order_type", order_type)
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "execution_mode", execution_mode)
        object.__setattr__(self, "client_order_id", client_order_id)
        object.__setattr__(self, "metadata", metadata)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "order_type": self.order_type,
            "price": self.price,
            "execution_mode": self.execution_mode,
            "client_order_id": self.client_order_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OrderIntent":
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be a mapping")
        return cls(
            symbol=payload.get("symbol"),
            side=payload.get("side"),
            quantity=payload.get("quantity"),
            order_type=payload.get("order_type", "market"),
            price=payload.get("price"),
            execution_mode=payload.get("execution_mode", "paper"),
            client_order_id=payload.get("client_order_id"),
            metadata=payload.get("metadata", {}),
        )


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    intent: OrderIntent
    status: str
    filled_quantity: float = 0.0
    average_fill_price: float | None = None
    fees: float = 0.0
    message: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.intent, OrderIntent):
            raise ValueError("intent must be an OrderIntent")
        status = _coerce_str(self.status, field_name="status").lower()
        if status not in ALLOWED_EVENT_STATUSES:
            allowed = ", ".join(sorted(ALLOWED_EVENT_STATUSES))
            raise ValueError(f"status must be one of: {allowed}")
        filled_quantity = validate_quantity(self.filled_quantity, field_name="filled_quantity", allow_zero=True)
        if filled_quantity > self.intent.quantity:
            raise ValueError("filled_quantity cannot exceed intent.quantity")
        average_fill_price = (
            None if self.average_fill_price is None else validate_quantity(self.average_fill_price, field_name="average_fill_price")
        )
        fees = validate_quantity(self.fees, field_name="fees", allow_zero=True)
        message = None if self.message is None else str(self.message)
        raw = dict(self.raw) if self.raw is not None else {}

        object.__setattr__(self, "status", status)
        object.__setattr__(self, "filled_quantity", filled_quantity)
        object.__setattr__(self, "average_fill_price", average_fill_price)
        object.__setattr__(self, "fees", fees)
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "raw", raw)

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.to_dict(),
            "status": self.status,
            "filled_quantity": self.filled_quantity,
            "average_fill_price": self.average_fill_price,
            "fees": self.fees,
            "message": self.message,
            "raw": dict(self.raw),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExecutionEvent":
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be a mapping")
        intent_payload = payload.get("intent")
        if not isinstance(intent_payload, Mapping):
            raise ValueError("payload['intent'] must be a mapping")
        return cls(
            intent=OrderIntent.from_dict(intent_payload),
            status=payload.get("status"),
            filled_quantity=payload.get("filled_quantity", 0.0),
            average_fill_price=payload.get("average_fill_price"),
            fees=payload.get("fees", 0.0),
            message=payload.get("message"),
            raw=payload.get("raw", {}),
        )


__all__ = ["ALLOWED_ORDER_TYPES", "ALLOWED_EVENT_STATUSES", "OrderIntent", "ExecutionEvent"]

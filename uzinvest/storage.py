"""Состояние бота: подписчики, их портфели и заказы на оплату.

JSON-файл здесь достаточен: рабочий объём — это «люди, которые пишут одному
боту». Запись атомарна (временный файл и замена) и под замком, потому что в
хранилище лезут и обработчики Telegram, и входящие вызовы Payme и Click.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .billing.base import OrderState
from .bonds import Position
from .money import to_decimal

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _dump_dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def new_order_id() -> str:
    """Короткий непредсказуемый идентификатор заказа.

    Он уходит в платёжную ссылку, поэтому последовательные номера не годятся:
    по ним можно было бы перебирать чужие заказы.
    """
    return f"UZ{secrets.token_hex(5).upper()}"


@dataclass
class Subscriber:
    """Чат и всё, что бот о нём помнит."""

    chat_id: int
    username: str = ""
    paid_until: datetime | None = None
    alerts_enabled: bool = False
    positions: tuple[Position, ...] = ()
    deposit_rate: Decimal | None = None     # с чем сравнивать доходность
    inflation: Decimal | None = None        # для реальной доходности
    created_at: datetime = field(default_factory=utcnow)
    last_alerts: dict[str, datetime] = field(default_factory=dict)

    @property
    def has_access(self) -> bool:
        return self.paid_until is not None and self.paid_until > utcnow()

    @property
    def access_days_left(self) -> int:
        if not self.has_access:
            return 0
        return max((self.paid_until - utcnow()).days, 0)

    def to_json(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "username": self.username,
            "paid_until": _dump_dt(self.paid_until),
            "alerts_enabled": self.alerts_enabled,
            "positions": [
                {
                    "isin": p.isin,
                    "quantity": p.quantity,
                    "purchase_price": str(p.purchase_price) if p.purchase_price else None,
                    "purchase_date": p.purchase_date.isoformat() if p.purchase_date else None,
                    "note": p.note,
                }
                for p in self.positions
            ],
            "deposit_rate": str(self.deposit_rate) if self.deposit_rate is not None else None,
            "inflation": str(self.inflation) if self.inflation is not None else None,
            "created_at": _dump_dt(self.created_at),
            "last_alerts": {k: _dump_dt(v) for k, v in self.last_alerts.items()},
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Subscriber":
        positions = []
        for raw in data.get("positions", []):
            try:
                positions.append(
                    Position(
                        isin=raw["isin"],
                        quantity=int(raw["quantity"]),
                        purchase_price=(
                            to_decimal(raw["purchase_price"])
                            if raw.get("purchase_price")
                            else None
                        ),
                        purchase_date=(
                            date.fromisoformat(raw["purchase_date"])
                            if raw.get("purchase_date")
                            else None
                        ),
                        note=raw.get("note", ""),
                    )
                )
            except (KeyError, ValueError) as exc:
                log.warning("пропускаю позицию чата %s: %s", data.get("chat_id"), exc)
        return cls(
            chat_id=int(data["chat_id"]),
            username=data.get("username", ""),
            paid_until=_parse_dt(data.get("paid_until")),
            alerts_enabled=bool(data.get("alerts_enabled", False)),
            positions=tuple(positions),
            deposit_rate=(
                to_decimal(data["deposit_rate"]) if data.get("deposit_rate") else None
            ),
            inflation=to_decimal(data["inflation"]) if data.get("inflation") else None,
            created_at=_parse_dt(data.get("created_at")) or utcnow(),
            last_alerts={
                k: dt
                for k, v in (data.get("last_alerts") or {}).items()
                if (dt := _parse_dt(v)) is not None
            },
        )


@dataclass
class Order:
    """Заказ на оплату доступа."""

    order_id: str
    chat_id: int
    plan: str
    days: int
    amount: Decimal
    provider: str                       # "payme" или "click"
    state: int = OrderState.CREATED
    provider_trans_id: str = ""         # id транзакции на стороне провайдера
    prepare_id: str = ""                # merchant_prepare_id для Click
    created_at: datetime = field(default_factory=utcnow)
    paid_at: datetime | None = None
    cancelled_at: datetime | None = None
    cancel_reason: str = ""

    @property
    def is_paid(self) -> bool:
        return self.state == OrderState.PAID

    @property
    def is_open(self) -> bool:
        return self.state == OrderState.CREATED

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["amount"] = str(self.amount)
        data["created_at"] = _dump_dt(self.created_at)
        data["paid_at"] = _dump_dt(self.paid_at)
        data["cancelled_at"] = _dump_dt(self.cancelled_at)
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Order":
        return cls(
            order_id=str(data["order_id"]),
            chat_id=int(data["chat_id"]),
            plan=data.get("plan", ""),
            days=int(data.get("days", 0)),
            amount=to_decimal(data.get("amount", 0)),
            provider=data.get("provider", ""),
            state=int(data.get("state", OrderState.CREATED)),
            provider_trans_id=str(data.get("provider_trans_id", "")),
            prepare_id=str(data.get("prepare_id", "")),
            created_at=_parse_dt(data.get("created_at")) or utcnow(),
            paid_at=_parse_dt(data.get("paid_at")),
            cancelled_at=_parse_dt(data.get("cancelled_at")),
            cancel_reason=data.get("cancel_reason", ""),
        )


class Store:
    """Потокобезопасное JSON-хранилище."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._subscribers: dict[int, Subscriber] = {}
        self._orders: dict[str, Order] = {}
        self.load()

    # -- файл -------------------------------------------------------------

    def load(self) -> None:
        with self._lock:
            if not self.path.is_file():
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                log.error("не читается %s, начинаю с пустого: %s", self.path, exc)
                return
            self._subscribers = {
                int(s["chat_id"]): Subscriber.from_json(s)
                for s in raw.get("subscribers", [])
            }
            self._orders = {
                str(o["order_id"]): Order.from_json(o) for o in raw.get("orders", [])
            }

    def save(self) -> None:
        with self._lock:
            payload = {
                "version": SCHEMA_VERSION,
                "saved_at": _dump_dt(utcnow()),
                "subscribers": [s.to_json() for s in self._subscribers.values()],
                "orders": [o.to_json() for o in self._orders.values()],
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)

    # -- подписчики -------------------------------------------------------

    def get(self, chat_id: int) -> Subscriber | None:
        with self._lock:
            return self._subscribers.get(int(chat_id))

    def upsert(self, chat_id: int, username: str = "", **changes: Any) -> Subscriber:
        with self._lock:
            current = self._subscribers.get(int(chat_id)) or Subscriber(
                chat_id=int(chat_id), username=username
            )
            if username and username != current.username:
                changes.setdefault("username", username)
            updated = replace(current, **changes) if changes else current
            self._subscribers[int(chat_id)] = updated
            self.save()
            return updated

    def all(self) -> list[Subscriber]:
        with self._lock:
            return list(self._subscribers.values())

    def alerting_subscribers(self) -> list[Subscriber]:
        """Все, кто попросил напоминания. Право на них решает бот, не хранилище."""
        return [s for s in self.all() if s.alerts_enabled]

    def grant_access(self, chat_id: int, days: int) -> Subscriber:
        """Продлить доступ, добавляя к остатку, а не затирая его."""
        with self._lock:
            current = self._subscribers.get(int(chat_id)) or Subscriber(chat_id=int(chat_id))
            base = current.paid_until if current.has_access else utcnow()
            return self.upsert(chat_id, paid_until=base + timedelta(days=days))

    def revoke_access(self, chat_id: int) -> Subscriber:
        return self.upsert(chat_id, paid_until=None)

    # -- портфель ---------------------------------------------------------

    def set_position(self, chat_id: int, position: Position) -> Subscriber:
        """Добавить или заменить позицию по ISIN."""
        with self._lock:
            sub = self._subscribers.get(int(chat_id)) or Subscriber(chat_id=int(chat_id))
            kept = [p for p in sub.positions if p.isin != position.isin]
            return self.upsert(chat_id, positions=(*kept, position))

    def remove_position(self, chat_id: int, isin: str) -> bool:
        with self._lock:
            sub = self._subscribers.get(int(chat_id))
            if sub is None:
                return False
            probe = isin.strip().upper()
            kept = tuple(p for p in sub.positions if p.isin != probe)
            if len(kept) == len(sub.positions):
                return False
            self.upsert(chat_id, positions=kept)
            return True

    def clear_positions(self, chat_id: int) -> Subscriber:
        return self.upsert(chat_id, positions=())

    # -- напоминания ------------------------------------------------------

    def should_alert(self, chat_id: int, key: str, cooldown_seconds: int) -> bool:
        with self._lock:
            sub = self._subscribers.get(int(chat_id))
            if sub is None:
                return False
            last = sub.last_alerts.get(key)
            return last is None or (utcnow() - last).total_seconds() >= cooldown_seconds

    def mark_alerted(self, chat_id: int, key: str, keep: int = 50) -> None:
        with self._lock:
            sub = self._subscribers.get(int(chat_id))
            if sub is None:
                return
            alerts = dict(sub.last_alerts)
            alerts[key] = utcnow()
            if len(alerts) > keep:
                alerts = dict(sorted(alerts.items(), key=lambda kv: kv[1], reverse=True)[:keep])
            self._subscribers[int(chat_id)] = replace(sub, last_alerts=alerts)
            self.save()

    # -- заказы -----------------------------------------------------------

    def create_order(
        self, chat_id: int, plan: str, days: int, amount: Decimal, provider: str
    ) -> Order:
        with self._lock:
            order = Order(
                order_id=new_order_id(),
                chat_id=int(chat_id),
                plan=plan,
                days=days,
                amount=to_decimal(amount),
                provider=provider,
            )
            self._orders[order.order_id] = order
            self.save()
            return order

    def get_order(self, order_id: str) -> Order | None:
        with self._lock:
            return self._orders.get(str(order_id).strip())

    def update_order(self, order_id: str, **changes: Any) -> Order | None:
        with self._lock:
            order = self._orders.get(str(order_id))
            if order is None:
                return None
            updated = replace(order, **changes)
            self._orders[updated.order_id] = updated
            self.save()
            return updated

    def mark_order_paid(self, order_id: str, provider_trans_id: str = "") -> Order | None:
        """Провести оплату ровно один раз.

        Возвращает None, если заказа нет или он уже оплачен: повторный вызов
        Complete от провайдера не должен начислять доступ дважды.
        """
        with self._lock:
            order = self._orders.get(str(order_id))
            if order is None or order.is_paid:
                return None
            paid = replace(
                order,
                state=OrderState.PAID,
                paid_at=utcnow(),
                provider_trans_id=provider_trans_id or order.provider_trans_id,
            )
            self._orders[paid.order_id] = paid
            self.save()
            return paid

    def cancel_order(self, order_id: str, reason: str = "") -> Order | None:
        with self._lock:
            order = self._orders.get(str(order_id))
            if order is None:
                return None
            state = OrderState.REFUNDED if order.is_paid else OrderState.CANCELLED
            cancelled = replace(
                order, state=state, cancelled_at=utcnow(), cancel_reason=reason
            )
            self._orders[cancelled.order_id] = cancelled
            self.save()
            return cancelled

    def open_orders(self, chat_id: int | None = None) -> list[Order]:
        with self._lock:
            return [
                o
                for o in self._orders.values()
                if o.is_open and (chat_id is None or o.chat_id == int(chat_id))
            ]

    def orders_all(self) -> list[Order]:
        with self._lock:
            return list(self._orders.values())

    def orders_for(self, chat_id: int) -> list[Order]:
        with self._lock:
            return sorted(
                (o for o in self._orders.values() if o.chat_id == int(chat_id)),
                key=lambda o: o.created_at,
                reverse=True,
            )

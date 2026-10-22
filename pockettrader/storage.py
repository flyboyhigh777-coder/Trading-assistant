"""Durable bot state: subscribers, their settings, and billing entitlements.

A JSON file is plenty here — the working set is "people who talk to one bot".
Writes are atomic (temp file + replace) and guarded by a lock so the Telegram
handlers and the billing webhook can both touch the store.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def _utcnow() -> datetime:
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


@dataclass
class Subscriber:
    """One chat and everything the bot remembers about it."""

    chat_id: int
    username: str = ""
    alerts_enabled: bool = False
    watchlist: tuple[str, ...] = ()
    timeframe: str = ""
    min_confidence: float | None = None
    paid_until: datetime | None = None
    created_at: datetime = field(default_factory=_utcnow)
    #: signal key -> when it was last pushed, used to suppress repeats
    last_alerts: dict[str, datetime] = field(default_factory=dict)

    @property
    def has_access(self) -> bool:
        return self.paid_until is not None and self.paid_until > _utcnow()

    @property
    def access_days_left(self) -> int:
        if not self.has_access:
            return 0
        return max((self.paid_until - _utcnow()).days, 0)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["watchlist"] = list(self.watchlist)
        data["paid_until"] = _dump_dt(self.paid_until)
        data["created_at"] = _dump_dt(self.created_at)
        data["last_alerts"] = {k: _dump_dt(v) for k, v in self.last_alerts.items()}
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Subscriber":
        return cls(
            chat_id=int(data["chat_id"]),
            username=data.get("username", ""),
            alerts_enabled=bool(data.get("alerts_enabled", False)),
            watchlist=tuple(data.get("watchlist", ())),
            timeframe=data.get("timeframe", "") or "",
            min_confidence=data.get("min_confidence"),
            paid_until=_parse_dt(data.get("paid_until")),
            created_at=_parse_dt(data.get("created_at")) or _utcnow(),
            last_alerts={
                k: dt
                for k, v in (data.get("last_alerts") or {}).items()
                if (dt := _parse_dt(v)) is not None
            },
        )


@dataclass
class Invoice:
    """A Crypto Pay invoice we are waiting on."""

    invoice_id: int
    chat_id: int
    plan: str
    days: int
    amount: str
    asset: str
    status: str = "active"
    created_at: datetime = field(default_factory=_utcnow)
    paid_at: datetime | None = None

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = _dump_dt(self.created_at)
        data["paid_at"] = _dump_dt(self.paid_at)
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Invoice":
        return cls(
            invoice_id=int(data["invoice_id"]),
            chat_id=int(data["chat_id"]),
            plan=data.get("plan", ""),
            days=int(data.get("days", 0)),
            amount=str(data.get("amount", "0")),
            asset=data.get("asset", ""),
            status=data.get("status", "active"),
            created_at=_parse_dt(data.get("created_at")) or _utcnow(),
            paid_at=_parse_dt(data.get("paid_at")),
        )


class Store:
    """Thread-safe JSON store for subscribers and invoices."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._subscribers: dict[int, Subscriber] = {}
        self._invoices: dict[int, Invoice] = {}
        self.load()

    # -- persistence ------------------------------------------------------

    def load(self) -> None:
        with self._lock:
            if not self.path.is_file():
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                log.error("could not read %s, starting empty: %s", self.path, exc)
                return
            self._subscribers = {
                int(s["chat_id"]): Subscriber.from_json(s)
                for s in raw.get("subscribers", [])
            }
            self._invoices = {
                int(i["invoice_id"]): Invoice.from_json(i)
                for i in raw.get("invoices", [])
            }

    def save(self) -> None:
        with self._lock:
            payload = {
                "version": SCHEMA_VERSION,
                "saved_at": _dump_dt(_utcnow()),
                "subscribers": [s.to_json() for s in self._subscribers.values()],
                "invoices": [i.to_json() for i in self._invoices.values()],
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)

    # -- subscribers ------------------------------------------------------

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
        """Everyone who asked for alerts.

        Whether they are *entitled* to them is the caller's call: billing can be
        switched off entirely, and the store has no opinion on that.
        """
        return [s for s in self.all() if s.alerts_enabled]

    def grant_access(self, chat_id: int, days: int) -> Subscriber:
        """Extend paid access, stacking onto any time still remaining."""
        with self._lock:
            current = self._subscribers.get(int(chat_id)) or Subscriber(chat_id=int(chat_id))
            base = current.paid_until if current.has_access else _utcnow()
            return self.upsert(chat_id, paid_until=base + timedelta(days=days))

    def revoke_access(self, chat_id: int) -> Subscriber:
        return self.upsert(chat_id, paid_until=None)

    # -- alert de-duplication --------------------------------------------

    def should_alert(self, chat_id: int, key: str, cooldown_seconds: int) -> bool:
        """True if this signal has not been pushed to this chat recently."""
        with self._lock:
            sub = self._subscribers.get(int(chat_id))
            if sub is None:
                return False
            last = sub.last_alerts.get(key)
            return last is None or (_utcnow() - last).total_seconds() >= cooldown_seconds

    def mark_alerted(self, chat_id: int, key: str, keep: int = 50) -> None:
        with self._lock:
            sub = self._subscribers.get(int(chat_id))
            if sub is None:
                return
            alerts = dict(sub.last_alerts)
            alerts[key] = _utcnow()
            if len(alerts) > keep:
                newest = sorted(alerts.items(), key=lambda kv: kv[1], reverse=True)[:keep]
                alerts = dict(newest)
            self._subscribers[int(chat_id)] = replace(sub, last_alerts=alerts)
            self.save()

    # -- invoices ---------------------------------------------------------

    def add_invoice(self, invoice: Invoice) -> Invoice:
        with self._lock:
            self._invoices[invoice.invoice_id] = invoice
            self.save()
            return invoice

    def get_invoice(self, invoice_id: int) -> Invoice | None:
        with self._lock:
            return self._invoices.get(int(invoice_id))

    def mark_invoice_paid(self, invoice_id: int) -> Invoice | None:
        """Settle an invoice exactly once; returns None if already settled."""
        with self._lock:
            invoice = self._invoices.get(int(invoice_id))
            if invoice is None or invoice.status == "paid":
                return None
            settled = replace(invoice, status="paid", paid_at=_utcnow())
            self._invoices[int(invoice_id)] = settled
            self.save()
            return settled

    def pending_invoices(self, chat_id: int | None = None) -> list[Invoice]:
        with self._lock:
            return [
                i
                for i in self._invoices.values()
                if i.status == "active" and (chat_id is None or i.chat_id == int(chat_id))
            ]

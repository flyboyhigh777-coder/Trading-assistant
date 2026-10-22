from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from uzinvest.billing.base import OrderState
from uzinvest.bonds import Position
from uzinvest.storage import Store, Subscriber, new_order_id


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "s.json")


def test_upsert_creates_then_updates(store):
    created = store.upsert(42, username="investor")
    assert created.username == "investor"
    assert store.upsert(42, alerts_enabled=True).username == "investor"
    assert len(store.all()) == 1


def test_everything_survives_a_restart(tmp_path):
    path = tmp_path / "s.json"
    first = Store(path)
    first.upsert(7, deposit_rate=Decimal("0.24"), inflation=Decimal("0.11"))
    first.set_position(7, Position("UZ6058537AA0", 3, 1_000_000, date(2025, 2, 1)))
    first.grant_access(7, 10)
    order = first.create_order(7, "month", 30, Decimal("49000"), "payme")

    second = Store(path)
    restored = second.get(7)
    assert restored.deposit_rate == Decimal("0.24")
    assert restored.positions[0].purchase_date == date(2025, 2, 1)
    assert restored.has_access
    assert second.get_order(order.order_id).amount == Decimal("49000")


def test_a_corrupt_file_does_not_crash_the_bot(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{ это не json", encoding="utf-8")
    assert Store(path).all() == []


def test_a_broken_position_is_skipped_not_fatal():
    sub = Subscriber.from_json(
        {
            "chat_id": 1,
            "positions": [
                {"isin": "UZ1", "quantity": 2},
                {"isin": "UZ2"},                       # нет количества
                {"isin": "UZ3", "quantity": "много"},  # не число
            ],
        }
    )
    assert [p.isin for p in sub.positions] == ["UZ1"]


def test_order_ids_are_unpredictable():
    # Номер уходит в платёжную ссылку: по последовательным можно было бы
    # перебирать чужие заказы.
    ids = {new_order_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(i.startswith("UZ") and len(i) == 12 for i in ids)


# -- доступ ---------------------------------------------------------------


def test_grant_stacks_onto_remaining_time(store):
    store.grant_access(1, 5)
    assert 9 <= store.grant_access(1, 5).access_days_left <= 10


def test_expired_access_restarts_from_now(store):
    store.upsert(1, paid_until=datetime.now(timezone.utc) - timedelta(days=30))
    assert not store.get(1).has_access
    assert store.grant_access(1, 7).access_days_left >= 6


def test_revoke_clears_access(store):
    store.grant_access(1, 7)
    assert not store.revoke_access(1).has_access


def test_alerting_subscribers_are_the_ones_who_opted_in(store):
    store.upsert(1, alerts_enabled=True)
    store.grant_access(2, 1)
    store.upsert(3, alerts_enabled=True)
    store.grant_access(3, 1)
    assert [s.chat_id for s in store.alerting_subscribers()] == [1, 3]


# -- портфель -------------------------------------------------------------


def test_adding_the_same_isin_replaces_rather_than_duplicates(store):
    store.set_position(1, Position("UZ1", 3))
    store.set_position(1, Position("UZ1", 7))
    assert [(p.isin, p.quantity) for p in store.get(1).positions] == [("UZ1", 7)]


def test_remove_reports_whether_anything_changed(store):
    store.set_position(1, Position("UZ1", 3))
    assert store.remove_position(1, "uz1") is True
    assert store.remove_position(1, "UZ1") is False
    assert store.remove_position(999, "UZ1") is False


def test_clear_empties_the_portfolio(store):
    store.set_position(1, Position("UZ1", 3))
    store.set_position(1, Position("UZ2", 1))
    assert store.clear_positions(1).positions == ()


# -- заказы ---------------------------------------------------------------


def test_an_order_is_paid_exactly_once(store):
    order = store.create_order(5, "month", 30, Decimal("49000"), "payme")
    paid = store.mark_order_paid(order.order_id, "tx-1")
    assert paid.state == OrderState.PAID
    assert paid.provider_trans_id == "tx-1"
    assert store.mark_order_paid(order.order_id) is None    # повторный вызов
    assert store.open_orders() == []


def test_cancelling_a_paid_order_is_a_refund(store):
    order = store.create_order(5, "month", 30, Decimal("49000"), "payme")
    store.mark_order_paid(order.order_id)
    assert store.cancel_order(order.order_id, "5").state == OrderState.REFUNDED


def test_cancelling_an_unpaid_order_is_a_cancel(store):
    order = store.create_order(5, "month", 30, Decimal("49000"), "click")
    assert store.cancel_order(order.order_id).state == OrderState.CANCELLED


def test_open_orders_filter_by_chat(store):
    first = store.create_order(5, "week", 7, Decimal("15000"), "payme")
    store.create_order(6, "month", 30, Decimal("49000"), "click")
    assert [o.order_id for o in store.open_orders(5)] == [first.order_id]


def test_updating_a_missing_order_returns_none(store):
    assert store.update_order("НЕТ", state=OrderState.PAID) is None
    assert store.cancel_order("НЕТ") is None


# -- напоминания ----------------------------------------------------------


def test_reminder_cooldown(store):
    store.upsert(1)
    key = "coupons|2026-10-25"
    assert store.should_alert(1, key, 3600)
    store.mark_alerted(1, key)
    assert not store.should_alert(1, key, 3600)
    assert store.should_alert(1, key, 0)


def test_reminder_history_is_capped(store):
    store.upsert(1)
    for i in range(60):
        store.mark_alerted(1, f"k{i}", keep=50)
    assert len(store.get(1).last_alerts) == 50


def test_unknown_chat_is_never_alerted(store):
    assert not store.should_alert(999, "k", 0)

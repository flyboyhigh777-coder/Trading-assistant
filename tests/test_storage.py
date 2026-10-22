from datetime import datetime, timedelta, timezone

import pytest

from pockettrader.storage import Invoice, Store, Subscriber


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "subs.json")


def test_upsert_creates_then_updates(store):
    created = store.upsert(42, username="trader")
    assert created.chat_id == 42
    assert created.username == "trader"
    updated = store.upsert(42, alerts_enabled=True)
    assert updated.alerts_enabled
    assert updated.username == "trader"
    assert len(store.all()) == 1


def test_state_survives_a_restart(tmp_path):
    path = tmp_path / "subs.json"
    first = Store(path)
    first.upsert(7, watchlist=("BTC/USDT",), timeframe="15m", min_confidence=0.7)
    first.grant_access(7, days=10)

    second = Store(path)
    reloaded = second.get(7)
    assert reloaded.watchlist == ("BTC/USDT",)
    assert reloaded.timeframe == "15m"
    assert reloaded.min_confidence == 0.7
    assert reloaded.has_access


def test_a_corrupt_file_does_not_crash_the_bot(tmp_path):
    path = tmp_path / "subs.json"
    path.write_text("{ this is not json")
    assert Store(path).all() == []


def test_grant_stacks_onto_remaining_time(store):
    store.grant_access(1, days=5)
    stacked = store.grant_access(1, days=5)
    assert 9 <= stacked.access_days_left <= 10


def test_expired_access_restarts_from_now(store):
    past = datetime.now(tz=timezone.utc) - timedelta(days=30)
    store.upsert(1, paid_until=past)
    assert not store.get(1).has_access
    granted = store.grant_access(1, days=7)
    assert granted.access_days_left >= 6


def test_revoke_clears_access(store):
    store.grant_access(1, days=7)
    assert not store.revoke_access(1).has_access


def test_alerting_subscribers_are_the_ones_who_opted_in(store):
    store.upsert(1, alerts_enabled=True)     # opted in, unpaid
    store.grant_access(2, days=1)            # paid, opted out
    store.upsert(3, alerts_enabled=True)
    store.grant_access(3, days=1)
    # Entitlement is the bot's call, so an unpaid opt-in still shows up here.
    assert [s.chat_id for s in store.alerting_subscribers()] == [1, 3]


def test_alert_cooldown(store):
    store.upsert(1)
    key = "BTC/USDT|5m|CALL"
    assert store.should_alert(1, key, cooldown_seconds=600)
    store.mark_alerted(1, key)
    assert not store.should_alert(1, key, cooldown_seconds=600)
    assert store.should_alert(1, key, cooldown_seconds=0)
    assert store.should_alert(1, "ETH/USDT|5m|PUT", cooldown_seconds=600)


def test_alert_history_is_capped(store):
    store.upsert(1)
    for i in range(60):
        store.mark_alerted(1, f"SYM{i}|5m|CALL", keep=50)
    assert len(store.get(1).last_alerts) == 50


def test_unknown_chat_is_never_alerted(store):
    assert not store.should_alert(999, "k", cooldown_seconds=0)


def test_an_invoice_settles_exactly_once(store):
    store.add_invoice(Invoice(invoice_id=11, chat_id=5, plan="week", days=7,
                              amount="5", asset="USDT"))
    settled = store.mark_invoice_paid(11)
    assert settled is not None and settled.status == "paid"
    assert store.mark_invoice_paid(11) is None     # a duplicate webhook
    assert store.pending_invoices() == []


def test_pending_invoices_filter_by_chat(store):
    store.add_invoice(Invoice(11, 5, "week", 7, "5", "USDT"))
    store.add_invoice(Invoice(12, 6, "month", 30, "15", "USDT"))
    assert [i.invoice_id for i in store.pending_invoices(chat_id=6)] == [12]


def test_subscriber_round_trips_through_json():
    sub = Subscriber(chat_id=1, watchlist=("BTC/USDT",), paid_until=datetime.now(timezone.utc))
    restored = Subscriber.from_json(sub.to_json())
    assert restored.watchlist == sub.watchlist
    assert restored.paid_until == sub.paid_until

import hashlib
import hmac
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from pockettrader.billing import SIGNATURE_HEADER, plans_by_code
from pockettrader.config import Settings
from pockettrader.storage import Invoice, Store
from pockettrader.webhook import BillingWebhook

TOKEN = "12345:AAbbCC"


def sign(body: bytes) -> str:
    return hmac.new(hashlib.sha256(TOKEN.encode()).digest(), body, hashlib.sha256).hexdigest()


def paid_update(invoice_id: int, chat_id: int, plan: str) -> bytes:
    return json.dumps(
        {
            "update_id": 1,
            "update_type": "invoice_paid",
            "payload": {
                "invoice_id": invoice_id,
                "status": "paid",
                "payload": json.dumps({"chat_id": chat_id, "plan": plan}),
            },
        }
    ).encode()


@pytest.fixture
def store(tmp_path) -> Store:
    store = Store(tmp_path / "subs.json")
    store.upsert(4242)
    store.add_invoice(Invoice(invoice_id=7, chat_id=4242, plan="week", days=7,
                              amount="5", asset="USDT"))
    return store


@pytest.fixture
def hook(store, tmp_path):
    settings = Settings(cryptobot_token=TOKEN, subscribers_path=tmp_path / "subs.json")
    notified = []

    async def on_paid(chat_id, plan, invoice):
        notified.append((chat_id, plan.code, invoice.invoice_id))

    webhook = BillingWebhook(settings, store, plans_by_code(), on_paid=on_paid)
    webhook.notified = notified
    return webhook


@pytest.fixture
async def client(hook):
    async with TestClient(TestServer(hook.build_app())) as client:
        yield client


async def test_a_signed_payment_grants_access(client, hook, store):
    body = paid_update(7, 4242, "week")
    response = await client.post(
        hook.settings.webhook_path, data=body, headers={SIGNATURE_HEADER: sign(body)}
    )
    assert response.status == 200
    assert store.get(4242).has_access
    assert store.get_invoice(7).status == "paid"
    assert hook.notified == [(4242, "week", 7)]


async def test_an_unsigned_payment_is_rejected(client, hook, store):
    body = paid_update(7, 4242, "week")
    response = await client.post(hook.settings.webhook_path, data=body)
    assert response.status == 401
    assert not store.get(4242).has_access


async def test_a_forged_signature_is_rejected(client, hook, store):
    body = paid_update(7, 4242, "week")
    response = await client.post(
        hook.settings.webhook_path, data=body, headers={SIGNATURE_HEADER: "00" * 32}
    )
    assert response.status == 401
    assert not store.get(4242).has_access


async def test_a_replayed_delivery_does_not_double_credit(client, hook, store):
    body = paid_update(7, 4242, "week")
    headers = {SIGNATURE_HEADER: sign(body)}
    await client.post(hook.settings.webhook_path, data=body, headers=headers)
    first = store.get(4242).paid_until
    await client.post(hook.settings.webhook_path, data=body, headers=headers)
    assert store.get(4242).paid_until == first
    assert len(hook.notified) == 1


async def test_other_update_types_are_acknowledged_and_ignored(client, hook, store):
    body = json.dumps({"update_type": "invoice_expired", "payload": {"invoice_id": 7}}).encode()
    response = await client.post(
        hook.settings.webhook_path, data=body, headers={SIGNATURE_HEADER: sign(body)}
    )
    assert response.status == 200
    assert (await response.json())["ignored"] is True
    assert not store.get(4242).has_access


async def test_malformed_json_is_a_400(client, hook):
    body = b"{not json"
    response = await client.post(
        hook.settings.webhook_path, data=body, headers={SIGNATURE_HEADER: sign(body)}
    )
    assert response.status == 400


async def test_health_endpoint_reports_state(client, store):
    response = await client.get("/health")
    assert response.status == 200
    body = await response.json()
    assert body["status"] == "ok"
    assert body["billing"] is True
    assert body["subscribers"] == 1


async def test_settle_survives_a_failing_notification(hook, store):
    async def boom(chat_id, plan, invoice):
        raise RuntimeError("telegram is down")

    hook.on_paid = boom
    assert await hook.settle(7, 4242, "week") is True
    assert store.get(4242).has_access     # the grant stands regardless


async def test_settle_falls_back_to_the_stored_day_count(hook, store):
    # An unknown plan code (renamed since the invoice was raised) must still
    # credit the days the user actually paid for.
    assert await hook.settle(7, 4242, "legacy-plan") is True
    assert store.get(4242).access_days_left >= 6

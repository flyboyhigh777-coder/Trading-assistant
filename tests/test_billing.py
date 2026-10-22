import hashlib
import hmac
import json

import httpx
import pytest

from pockettrader.billing import (
    DEFAULT_PLANS,
    MAINNET_API,
    TESTNET_API,
    BillingError,
    CryptoPayClient,
    Plan,
    parse_invoice_paid,
    plans_by_code,
    verify_signature,
)

TOKEN = "12345:AAbbCC"


def sign(body: bytes, token: str = TOKEN) -> str:
    secret = hashlib.sha256(token.encode()).digest()
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def test_signature_accepts_a_correct_hmac():
    body = b'{"update_type":"invoice_paid"}'
    assert verify_signature(TOKEN, body, sign(body))


@pytest.mark.parametrize(
    "signature", ["", "deadbeef", "DEADBEEF" * 8], ids=["empty", "short", "wrong"]
)
def test_signature_rejects_anything_else(signature):
    assert not verify_signature(TOKEN, b"{}", signature)


def test_signature_rejects_a_tampered_body():
    body = b'{"amount":"5"}'
    signature = sign(body)
    assert not verify_signature(TOKEN, b'{"amount":"500"}', signature)


def test_signature_is_token_specific():
    body = b"{}"
    assert not verify_signature("other:token", body, sign(body))


def test_parse_invoice_paid_extracts_the_attached_payload():
    update = {
        "update_type": "invoice_paid",
        "payload": {
            "invoice_id": 99,
            "payload": json.dumps({"chat_id": 4242, "plan": "month"}),
        },
    }
    assert parse_invoice_paid(update) == (99, 4242, "month")


@pytest.mark.parametrize(
    "update",
    [
        {"update_type": "invoice_expired", "payload": {"invoice_id": 1}},
        {"update_type": "invoice_paid", "payload": {}},
        {"update_type": "invoice_paid", "payload": {"invoice_id": 1, "payload": "{oops"}},
        {"update_type": "invoice_paid", "payload": {"invoice_id": 1, "payload": "{}"}},
    ],
    ids=["other-type", "no-invoice-id", "bad-json", "missing-keys"],
)
def test_parse_invoice_paid_returns_none_for_unusable_updates(update):
    assert parse_invoice_paid(update) is None


def test_client_picks_the_host_from_the_testnet_flag():
    assert CryptoPayClient(TOKEN, testnet=True).base_url == TESTNET_API
    assert CryptoPayClient(TOKEN, testnet=False).base_url == MAINNET_API


def test_client_refuses_an_empty_token():
    with pytest.raises(BillingError, match="token is empty"):
        CryptoPayClient("")


def _client_with(handler) -> CryptoPayClient:
    client = CryptoPayClient(TOKEN, testnet=True)
    client._client = httpx.AsyncClient(
        base_url=TESTNET_API,
        transport=httpx.MockTransport(handler),
        headers={"Crypto-Pay-API-Token": TOKEN},
    )
    return client


@pytest.mark.asyncio
async def test_create_invoice_sends_the_plan_and_returns_the_pay_url():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["token"] = request.headers.get("Crypto-Pay-API-Token")
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "invoice_id": 7,
                    "status": "active",
                    "amount": "5",
                    "asset": "USDT",
                    "bot_invoice_url": "https://t.me/CryptoTestnetBot?start=IV7",
                },
            },
        )

    async with _client_with(handler) as client:
        invoice = await client.create_invoice(DEFAULT_PLANS[0], chat_id=4242)

    assert invoice.invoice_id == 7
    assert invoice.pay_url.endswith("IV7")
    assert seen["url"].endswith("/createInvoice")
    assert seen["token"] == TOKEN
    assert json.loads(seen["body"]["payload"]) == {"chat_id": 4242, "plan": "week"}


@pytest.mark.asyncio
async def test_api_errors_become_billing_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": False, "error": {"code": 401, "name": "UNAUTHORIZED"}}
        )

    async with _client_with(handler) as client:
        with pytest.raises(BillingError, match="UNAUTHORIZED"):
            await client.get_me()


@pytest.mark.asyncio
async def test_an_invoice_without_a_pay_url_is_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"invoice_id": 7}})

    async with _client_with(handler) as client:
        with pytest.raises(BillingError, match="without a payment URL"):
            await client.create_invoice(DEFAULT_PLANS[0], chat_id=1)


@pytest.mark.asyncio
async def test_transport_failures_become_billing_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    async with _client_with(handler) as client:
        with pytest.raises(BillingError, match="getMe failed"):
            await client.get_me()


@pytest.mark.asyncio
async def test_get_invoice_unwraps_the_items_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": True, "result": {"items": [{"invoice_id": 7, "status": "paid"}]}}
        )

    async with _client_with(handler) as client:
        assert (await client.get_invoice(7))["status"] == "paid"


def test_plans_are_indexed_by_code():
    plans = plans_by_code()
    assert set(plans) == {"week", "month", "quarter"}
    assert "7 days" in plans["week"].describe()
    assert Plan("x", "X", 1, "1").asset == "USDT"

"""Входящие вызовы Payme и Click против живого aiohttp-сервера."""

from __future__ import annotations

import base64
import hashlib
import json
from decimal import Decimal

import pytest
from aiohttp.test_utils import TestClient, TestServer

from uzinvest.billing import (
    ClickCode,
    ClickProvider,
    PaymeCode,
    PaymeProvider,
    plans_by_code,
)
from uzinvest.billing.base import OrderState
from uzinvest.config import Settings
from uzinvest.storage import Store
from uzinvest.webhook import BillingRoutes

PAYME_KEY = "payme-secret-key"
CLICK_SECRET = "click-secret"
MERCHANT = "merchant-id"
SERVICE_ID = "12345"
MONTH_PRICE = Decimal("49000")
MONTH_TIYIN = 4_900_000


def basic(key: str = PAYME_KEY) -> str:
    return "Basic " + base64.b64encode(f"Paycom:{key}".encode()).decode()


def click_sign(params: dict, secret: str = CLICK_SECRET) -> str:
    parts = [
        str(params.get("click_trans_id", "")),
        str(params.get("service_id", "")),
        secret,
        str(params.get("merchant_trans_id", "")),
    ]
    if str(params.get("action")) == "1":
        parts.append(str(params.get("merchant_prepare_id", "")))
    parts += [str(params.get("amount", "")), str(params.get("action", "")),
              str(params.get("sign_time", ""))]
    return hashlib.md5("".join(parts).encode()).hexdigest()


def signed(params: dict) -> dict:
    return {**params, "sign_string": click_sign(params)}


@pytest.fixture
def store(tmp_path) -> Store:
    store = Store(tmp_path / "s.json")
    store.upsert(555)
    return store


@pytest.fixture
def order(store):
    return store.create_order(555, "month", 30, MONTH_PRICE, "payme")


@pytest.fixture
def routes(store, tmp_path):
    settings = Settings(
        subscribers_path=tmp_path / "s.json",
        payme_merchant_id=MERCHANT,
        payme_merchant_key=PAYME_KEY,
        click_service_id=SERVICE_ID,
        click_merchant_id="678",
        click_secret_key=CLICK_SECRET,
    )
    notified: list[tuple[int, str, str]] = []

    async def on_paid(chat_id, plan, paid_order):
        notified.append((chat_id, plan.code, paid_order.order_id))

    routes = BillingRoutes(
        settings,
        store,
        plans_by_code(),
        PaymeProvider(MERCHANT, PAYME_KEY),
        ClickProvider(SERVICE_ID, "678", CLICK_SECRET),
        on_paid=on_paid,
    )
    routes.notified = notified
    return routes


@pytest.fixture
async def client(routes):
    async with TestClient(TestServer(routes.build_app())) as client:
        yield client


async def payme(client, method: str, params: dict, auth: str | None = basic()):
    headers = {"Authorization": auth} if auth else {}
    response = await client.post(
        "/api/billing/payme",
        data=json.dumps({"id": 1, "method": method, "params": params}),
        headers={**headers, "Content-Type": "application/json"},
    )
    return response.status, await response.json()


# == Payme ================================================================


async def test_payme_rejects_a_wrong_key(client, order):
    status, body = await payme(
        client, "CheckPerformTransaction",
        {"amount": MONTH_TIYIN, "account": {"order_id": order.order_id}},
        auth=basic("не тот ключ"),
    )
    # HTTP всё равно 200: Payme читает код из тела, а не из статуса.
    assert status == 200
    assert body["error"]["code"] == PaymeCode.UNAUTHORIZED


@pytest.mark.parametrize(
    "auth",
    [None, "Bearer xxx", "Basic !!!не base64", basic("ключ-с-кириллицей")],
    ids=["нет", "не-basic", "не-base64", "не-ascii-ключ"],
)
async def test_payme_rejects_malformed_auth(client, order, auth):
    _, body = await payme(
        client, "CheckPerformTransaction",
        {"amount": MONTH_TIYIN, "account": {"order_id": order.order_id}}, auth=auth,
    )
    assert body["error"]["code"] == PaymeCode.UNAUTHORIZED


async def test_payme_check_perform_allows_a_valid_order(client, order):
    _, body = await payme(
        client, "CheckPerformTransaction",
        {"amount": MONTH_TIYIN, "account": {"order_id": order.order_id}},
    )
    assert body["result"] == {"allow": True}


async def test_payme_rejects_a_wrong_amount(client, order):
    # 49 000 сум — это 4 900 000 тийинов. Путаница в сто раз ловится здесь.
    _, body = await payme(
        client, "CheckPerformTransaction",
        {"amount": 49_000, "account": {"order_id": order.order_id}},
    )
    assert body["error"]["code"] == PaymeCode.INVALID_AMOUNT


async def test_payme_rejects_an_unknown_order(client):
    _, body = await payme(
        client, "CheckPerformTransaction",
        {"amount": MONTH_TIYIN, "account": {"order_id": "НЕТ-ТАКОГО"}},
    )
    assert body["error"]["code"] == PaymeCode.ORDER_NOT_FOUND


async def test_payme_error_messages_are_trilingual(client):
    _, body = await payme(
        client, "CheckPerformTransaction",
        {"amount": MONTH_TIYIN, "account": {"order_id": "НЕТ"}},
    )
    assert set(body["error"]["message"]) == {"ru", "uz", "en"}


async def test_payme_unknown_method(client):
    _, body = await payme(client, "НеСуществующийМетод", {})
    assert body["error"]["code"] == PaymeCode.METHOD_NOT_FOUND


async def test_payme_full_payment_flow(client, routes, store, order):
    account = {"order_id": order.order_id}
    _, created = await payme(
        client, "CreateTransaction",
        {"id": "tx-1", "time": 1, "amount": MONTH_TIYIN, "account": account},
    )
    assert created["result"]["state"] == OrderState.CREATED

    _, performed = await payme(client, "PerformTransaction", {"id": "tx-1"})
    assert performed["result"]["state"] == OrderState.PAID
    assert store.get(555).has_access
    assert routes.notified == [(555, "month", order.order_id)]


async def test_payme_perform_is_idempotent(client, routes, store, order):
    account = {"order_id": order.order_id}
    await payme(client, "CreateTransaction",
                {"id": "tx-1", "amount": MONTH_TIYIN, "account": account})
    await payme(client, "PerformTransaction", {"id": "tx-1"})
    paid_until = store.get(555).paid_until

    _, again = await payme(client, "PerformTransaction", {"id": "tx-1"})
    assert again["result"]["state"] == OrderState.PAID
    assert store.get(555).paid_until == paid_until    # второй раз не начислили
    assert len(routes.notified) == 1


async def test_payme_create_is_idempotent_for_the_same_transaction(client, order):
    account = {"order_id": order.order_id}
    params = {"id": "tx-1", "amount": MONTH_TIYIN, "account": account}
    _, first = await payme(client, "CreateTransaction", params)
    _, second = await payme(client, "CreateTransaction", params)
    assert first["result"]["transaction"] == second["result"]["transaction"]


async def test_payme_refuses_a_second_transaction_on_one_order(client, order):
    account = {"order_id": order.order_id}
    await payme(client, "CreateTransaction",
                {"id": "tx-1", "amount": MONTH_TIYIN, "account": account})
    _, body = await payme(client, "CreateTransaction",
                          {"id": "tx-2", "amount": MONTH_TIYIN, "account": account})
    assert body["error"]["code"] == PaymeCode.CANNOT_PERFORM


async def test_payme_perform_on_unknown_transaction(client):
    _, body = await payme(client, "PerformTransaction", {"id": "призрак"})
    assert body["error"]["code"] == PaymeCode.TRANSACTION_NOT_FOUND


async def test_payme_cancel_before_payment(client, store, order):
    account = {"order_id": order.order_id}
    await payme(client, "CreateTransaction",
                {"id": "tx-1", "amount": MONTH_TIYIN, "account": account})
    _, body = await payme(client, "CancelTransaction", {"id": "tx-1", "reason": 3})
    assert body["result"]["state"] == OrderState.CANCELLED
    assert not store.get(555).has_access


async def test_payme_refund_takes_access_back(client, store, order):
    account = {"order_id": order.order_id}
    await payme(client, "CreateTransaction",
                {"id": "tx-1", "amount": MONTH_TIYIN, "account": account})
    await payme(client, "PerformTransaction", {"id": "tx-1"})
    assert store.get(555).has_access

    _, body = await payme(client, "CancelTransaction", {"id": "tx-1", "reason": 5})
    assert body["result"]["state"] == OrderState.REFUNDED
    assert not store.get(555).has_access


async def test_payme_cannot_pay_a_cancelled_order(client, order):
    account = {"order_id": order.order_id}
    await payme(client, "CreateTransaction",
                {"id": "tx-1", "amount": MONTH_TIYIN, "account": account})
    await payme(client, "CancelTransaction", {"id": "tx-1", "reason": 3})
    _, body = await payme(client, "PerformTransaction", {"id": "tx-1"})
    assert body["error"]["code"] == PaymeCode.CANNOT_PERFORM


async def test_payme_check_transaction_reports_state(client, order):
    account = {"order_id": order.order_id}
    await payme(client, "CreateTransaction",
                {"id": "tx-1", "amount": MONTH_TIYIN, "account": account})
    _, body = await payme(client, "CheckTransaction", {"id": "tx-1"})
    assert body["result"]["state"] == OrderState.CREATED
    assert body["result"]["perform_time"] == 0


async def test_payme_statement_lists_transactions_in_range(client, order):
    account = {"order_id": order.order_id}
    await payme(client, "CreateTransaction",
                {"id": "tx-1", "amount": MONTH_TIYIN, "account": account})
    _, body = await payme(client, "GetStatement", {"from": 0, "to": 9_999_999_999_999})
    assert [t["id"] for t in body["result"]["transactions"]] == ["tx-1"]

    _, empty = await payme(client, "GetStatement", {"from": 0, "to": 1})
    assert empty["result"]["transactions"] == []


async def test_payme_survives_broken_json(client):
    response = await client.post(
        "/api/billing/payme", data="{не json".encode("utf-8"),
        headers={"Authorization": basic(), "Content-Type": "application/json"},
    )
    assert response.status == 200
    assert (await response.json())["error"]["code"] == PaymeCode.INVALID_REQUEST


# == Click ================================================================


def click_params(order_id: str, action: int, **extra) -> dict:
    return {
        "click_trans_id": "999",
        "service_id": SERVICE_ID,
        "merchant_trans_id": order_id,
        "amount": "49000.00",
        "action": str(action),
        "sign_time": "2026-10-22 09:57:00",
        **extra,
    }


async def test_click_prepare_then_complete_grants_access(client, routes, store):
    order = store.create_order(555, "month", 30, MONTH_PRICE, "click")
    prepare = await client.post(
        "/api/billing/click/prepare", data=signed(click_params(order.order_id, 0))
    )
    body = await prepare.json()
    assert body["error"] == ClickCode.SUCCESS
    prepare_id = body["merchant_prepare_id"]

    complete = await client.post(
        "/api/billing/click/complete",
        data=signed(click_params(order.order_id, 1, merchant_prepare_id=prepare_id, error="0")),
    )
    assert (await complete.json())["error"] == ClickCode.SUCCESS
    assert store.get(555).has_access
    assert routes.notified == [(555, "month", order.order_id)]


@pytest.mark.parametrize("forged", ["0" * 32, "подпись-кириллицей", " "])
async def test_click_rejects_a_non_ascii_signature(client, store, forged):
    # compare_digest на строках с не-ASCII бросает TypeError: проверяем,
    # что это отказ, а не падение обработчика.
    order = store.create_order(555, "month", 30, MONTH_PRICE, "click")
    params = {**click_params(order.order_id, 0), "sign_string": forged}
    response = await client.post("/api/billing/click/prepare", data=params)
    assert (await response.json())["error"] == ClickCode.SIGN_CHECK_FAILED


async def test_click_rejects_a_forged_signature(client, store):
    order = store.create_order(555, "month", 30, MONTH_PRICE, "click")
    params = {**click_params(order.order_id, 0), "sign_string": "0" * 32}
    response = await client.post("/api/billing/click/prepare", data=params)
    assert (await response.json())["error"] == ClickCode.SIGN_CHECK_FAILED
    assert not store.get(555).has_access


async def test_click_rejects_an_unsigned_call(client, store):
    order = store.create_order(555, "month", 30, MONTH_PRICE, "click")
    response = await client.post(
        "/api/billing/click/prepare", data=click_params(order.order_id, 0)
    )
    assert (await response.json())["error"] == ClickCode.SIGN_CHECK_FAILED


async def test_click_rejects_a_wrong_amount(client, store):
    order = store.create_order(555, "month", 30, MONTH_PRICE, "click")
    params = click_params(order.order_id, 0)
    params["amount"] = "1000"
    response = await client.post("/api/billing/click/prepare", data=signed(params))
    assert (await response.json())["error"] == ClickCode.INCORRECT_AMOUNT


async def test_click_tolerates_rounding_in_the_amount(client, store):
    order = store.create_order(555, "month", 30, MONTH_PRICE, "click")
    params = click_params(order.order_id, 0)
    params["amount"] = "49000.40"
    response = await client.post("/api/billing/click/prepare", data=signed(params))
    # Клиент уже заплатил; отказывать из-за 40 тийинов нельзя.
    assert (await response.json())["error"] == ClickCode.SUCCESS


async def test_click_rejects_an_unknown_order(client):
    response = await client.post(
        "/api/billing/click/prepare", data=signed(click_params("НЕТ-ТАКОГО", 0))
    )
    assert (await response.json())["error"] == ClickCode.USER_NOT_FOUND


async def test_click_complete_requires_the_prepare_id(client, store):
    order = store.create_order(555, "month", 30, MONTH_PRICE, "click")
    await client.post("/api/billing/click/prepare", data=signed(click_params(order.order_id, 0)))
    response = await client.post(
        "/api/billing/click/complete",
        data=signed(click_params(order.order_id, 1, merchant_prepare_id="подделка", error="0")),
    )
    assert (await response.json())["error"] == ClickCode.TRANSACTION_NOT_FOUND
    assert not store.get(555).has_access


async def test_click_complete_is_idempotent(client, routes, store):
    order = store.create_order(555, "month", 30, MONTH_PRICE, "click")
    prepare = await client.post(
        "/api/billing/click/prepare", data=signed(click_params(order.order_id, 0))
    )
    prepare_id = (await prepare.json())["merchant_prepare_id"]
    payload = signed(
        click_params(order.order_id, 1, merchant_prepare_id=prepare_id, error="0")
    )
    await client.post("/api/billing/click/complete", data=payload)
    paid_until = store.get(555).paid_until

    second = await client.post("/api/billing/click/complete", data=payload)
    assert (await second.json())["error"] == ClickCode.ALREADY_PAID
    assert store.get(555).paid_until == paid_until
    assert len(routes.notified) == 1


async def test_click_failure_cancels_the_order(client, store):
    order = store.create_order(555, "month", 30, MONTH_PRICE, "click")
    prepare = await client.post(
        "/api/billing/click/prepare", data=signed(click_params(order.order_id, 0))
    )
    prepare_id = (await prepare.json())["merchant_prepare_id"]
    response = await client.post(
        "/api/billing/click/complete",
        data=signed(
            click_params(order.order_id, 1, merchant_prepare_id=prepare_id, error="-9")
        ),
    )
    assert (await response.json())["error"] == ClickCode.TRANSACTION_CANCELLED
    assert not store.get(555).has_access


async def test_click_will_not_prepare_an_already_paid_order(client, store):
    order = store.create_order(555, "month", 30, MONTH_PRICE, "click")
    store.mark_order_paid(order.order_id)
    response = await client.post(
        "/api/billing/click/prepare", data=signed(click_params(order.order_id, 0))
    )
    assert (await response.json())["error"] == ClickCode.ALREADY_PAID


# == прочее ===============================================================


async def test_health_reports_which_tills_are_live(client, store):
    response = await client.get("/health")
    body = await response.json()
    assert body == {
        "status": "ok", "payme": True, "click": True,
        "subscribers": 1, "open_orders": 0,
    }


async def test_a_failing_notification_does_not_undo_payment(routes, store, client):
    order = store.create_order(555, "month", 30, MONTH_PRICE, "payme")

    async def boom(*args):
        raise RuntimeError("telegram недоступен")

    routes.on_paid = boom
    assert await routes._credit(order) is True
    assert store.get(555).has_access

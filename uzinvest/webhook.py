"""Эндпоинты, которые вызывают платёжные системы.

    POST /api/billing/payme           — JSON-RPC Merchant API
    POST /api/billing/click/prepare   — SHOP-API, action=0
    POST /api/billing/click/complete  — SHOP-API, action=1
    GET  /health

Обе системы всегда отвечают HTTP 200; смысл передаётся кодом внутри тела.
Вернуть 500 вместо кода ошибки — значит заставить их повторять вызов.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from .billing.base import OrderState, Plan
from .billing.click import ClickCode, ClickProvider
from .billing.click import amount_matches as click_amount_matches
from .billing.click import error_response as click_error
from .billing.payme import (
    PaymeCode,
    PaymeError,
    PaymeProvider,
    now_ms,
)
from .billing.payme import (
    amount_matches as payme_amount_matches,
)
from .config import Settings
from .storage import Order, Store, utcnow

log = logging.getLogger(__name__)

PaidCallback = Callable[[int, Plan, Order], Awaitable[None]]

#: Payme не даёт проводить транзакцию, провисевшую дольше 12 часов.
TRANSACTION_TIMEOUT_MS = 12 * 60 * 60 * 1000


class BillingRoutes:
    """Обработчики входящих вызовов Payme и Click."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        plans: dict[str, Plan],
        payme: PaymeProvider | None = None,
        click: ClickProvider | None = None,
        on_paid: PaidCallback | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.plans = plans
        self.payme = payme
        self.click = click
        self.on_paid = on_paid
        self._runner: web.AppRunner | None = None

    # -- приложение -------------------------------------------------------

    def build_app(self) -> web.Application:
        app = web.Application()
        base = self.settings.webhook_path.rstrip("/")
        app.router.add_post(f"{base}/payme", self.handle_payme)
        app.router.add_post(f"{base}/click/prepare", self.handle_click_prepare)
        app.router.add_post(f"{base}/click/complete", self.handle_click_complete)
        app.router.add_get("/health", self.handle_health)
        return app

    async def handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "payme": bool(self.payme and self.payme.is_configured()),
                "click": bool(self.click and self.click.is_configured()),
                "subscribers": len(self.store.all()),
                "open_orders": len(self.store.open_orders()),
            }
        )

    # -- общее ------------------------------------------------------------

    async def _credit(self, order: Order, provider_trans_id: str = "") -> bool:
        """Начислить доступ по заказу ровно один раз."""
        paid = self.store.mark_order_paid(order.order_id, provider_trans_id)
        if paid is None:
            log.info("заказ %s уже оплачен, повтор игнорирую", order.order_id)
            return False
        self.store.grant_access(paid.chat_id, paid.days)
        log.info(
            "начислено %s дн. чату %s по заказу %s (%s)",
            paid.days, paid.chat_id, paid.order_id, paid.provider,
        )
        plan = self.plans.get(paid.plan)
        if self.on_paid is not None and plan is not None:
            try:
                await self.on_paid(paid.chat_id, plan, paid)
            except Exception:  # сбой уведомления не отменяет оплату
                log.exception("не смог уведомить чат %s о заказе %s",
                              paid.chat_id, paid.order_id)
        return True

    # -- Payme ------------------------------------------------------------

    async def handle_payme(self, request: web.Request) -> web.Response:
        """JSON-RPC: разобрать запрос, выполнить метод, ответить по протоколу."""
        body = await request.read()
        request_id: Any = None
        try:
            payload = json.loads(body)
            request_id = payload.get("id")
            if self.payme is None:
                raise PaymeError(PaymeCode.UNAUTHORIZED)
            self.payme.check_auth(request.headers.get("Authorization"))

            method = payload.get("method")
            params = payload.get("params") or {}
            handler = {
                "CheckPerformTransaction": self._payme_check_perform,
                "CreateTransaction": self._payme_create,
                "PerformTransaction": self._payme_perform,
                "CancelTransaction": self._payme_cancel,
                "CheckTransaction": self._payme_check,
                "GetStatement": self._payme_statement,
            }.get(method)
            if handler is None:
                raise PaymeError(PaymeCode.METHOD_NOT_FOUND, str(method))
            result = await handler(params)
        except PaymeError as exc:
            return web.json_response({"id": request_id, "error": exc.to_json()})
        except json.JSONDecodeError:
            return web.json_response(
                {"id": None, "error": PaymeError(PaymeCode.INVALID_REQUEST).to_json()}
            )
        except Exception:
            log.exception("payme: необработанная ошибка")
            return web.json_response(
                {"id": request_id, "error": PaymeError(PaymeCode.INVALID_REQUEST).to_json()}
            )
        return web.json_response({"id": request_id, "result": result})

    def _payme_order(self, params: dict[str, Any]) -> Order:
        """Достать заказ по account-полю запроса."""
        account = params.get("account") or {}
        field = self.payme.account_field if self.payme else "order_id"
        order_id = str(account.get(field, "")).strip()
        order = self.store.get_order(order_id) if order_id else None
        if order is None:
            raise PaymeError(PaymeCode.ORDER_NOT_FOUND, field)
        return order

    async def _payme_check_perform(self, params: dict[str, Any]) -> dict[str, Any]:
        order = self._payme_order(params)
        if not payme_amount_matches(order.amount, params.get("amount")):
            raise PaymeError(PaymeCode.INVALID_AMOUNT)
        if order.is_paid:
            raise PaymeError(PaymeCode.ORDER_ALREADY_PAID)
        if order.state in (OrderState.CANCELLED, OrderState.REFUNDED):
            raise PaymeError(PaymeCode.CANNOT_PERFORM)
        return {"allow": True}

    async def _payme_create(self, params: dict[str, Any]) -> dict[str, Any]:
        transaction_id = str(params.get("id", ""))
        order = self._payme_order(params)

        if order.provider_trans_id and order.provider_trans_id != transaction_id:
            # На один заказ уже заведена другая транзакция.
            raise PaymeError(PaymeCode.CANNOT_PERFORM)
        if not payme_amount_matches(order.amount, params.get("amount")):
            raise PaymeError(PaymeCode.INVALID_AMOUNT)
        if order.is_paid:
            raise PaymeError(PaymeCode.ORDER_ALREADY_PAID)
        if order.state in (OrderState.CANCELLED, OrderState.REFUNDED):
            raise PaymeError(PaymeCode.CANNOT_PERFORM)

        if order.provider_trans_id == transaction_id:
            # Повторный CreateTransaction — отвечаем тем же, что и в первый раз.
            return {
                "create_time": int(order.created_at.timestamp() * 1000),
                "transaction": order.order_id,
                "state": OrderState.CREATED,
            }

        updated = self.store.update_order(
            order.order_id,
            provider="payme",
            provider_trans_id=transaction_id,
            state=OrderState.CREATED,
            created_at=utcnow(),
        )
        return {
            "create_time": int(updated.created_at.timestamp() * 1000),
            "transaction": updated.order_id,
            "state": OrderState.CREATED,
        }

    def _payme_by_transaction(self, params: dict[str, Any]) -> Order:
        transaction_id = str(params.get("id", ""))
        for order in self.store.orders_all():
            if order.provider == "payme" and order.provider_trans_id == transaction_id:
                return order
        raise PaymeError(PaymeCode.TRANSACTION_NOT_FOUND)

    async def _payme_perform(self, params: dict[str, Any]) -> dict[str, Any]:
        order = self._payme_by_transaction(params)
        if order.is_paid:
            # Повтор: отвечаем тем же результатом, не начисляя второй раз.
            return {
                "transaction": order.order_id,
                "perform_time": int(order.paid_at.timestamp() * 1000),
                "state": OrderState.PAID,
            }
        if order.state in (OrderState.CANCELLED, OrderState.REFUNDED):
            raise PaymeError(PaymeCode.CANNOT_PERFORM)
        if now_ms() - int(order.created_at.timestamp() * 1000) > TRANSACTION_TIMEOUT_MS:
            self.store.cancel_order(order.order_id, "timeout")
            raise PaymeError(PaymeCode.CANNOT_PERFORM)

        await self._credit(order, order.provider_trans_id)
        settled = self.store.get_order(order.order_id)
        return {
            "transaction": settled.order_id,
            "perform_time": int(settled.paid_at.timestamp() * 1000),
            "state": OrderState.PAID,
        }

    async def _payme_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        order = self._payme_by_transaction(params)
        reason = params.get("reason")
        if order.state in (OrderState.CANCELLED, OrderState.REFUNDED):
            cancelled = order
        else:
            cancelled = self.store.cancel_order(order.order_id, str(reason))
            if cancelled.state == OrderState.REFUNDED:
                # Деньги возвращают — доступ надо забрать.
                self.store.revoke_access(cancelled.chat_id)
        return {
            "transaction": cancelled.order_id,
            "cancel_time": int(cancelled.cancelled_at.timestamp() * 1000),
            "state": cancelled.state,
        }

    async def _payme_check(self, params: dict[str, Any]) -> dict[str, Any]:
        order = self._payme_by_transaction(params)
        return {
            "create_time": int(order.created_at.timestamp() * 1000),
            "perform_time": int(order.paid_at.timestamp() * 1000) if order.paid_at else 0,
            "cancel_time": (
                int(order.cancelled_at.timestamp() * 1000) if order.cancelled_at else 0
            ),
            "transaction": order.order_id,
            "state": order.state,
            "reason": int(order.cancel_reason) if order.cancel_reason.isdigit() else None,
        }

    async def _payme_statement(self, params: dict[str, Any]) -> dict[str, Any]:
        start = int(params.get("from", 0))
        end = int(params.get("to", 0))
        transactions = []
        for order in self.store.orders_all():
            if order.provider != "payme" or not order.provider_trans_id:
                continue
            created = int(order.created_at.timestamp() * 1000)
            if not start <= created <= end:
                continue
            transactions.append(
                {
                    "id": order.provider_trans_id,
                    "time": created,
                    "amount": int(order.amount * 100),
                    "account": {
                        (self.payme.account_field if self.payme else "order_id"): order.order_id
                    },
                    "create_time": created,
                    "perform_time": (
                        int(order.paid_at.timestamp() * 1000) if order.paid_at else 0
                    ),
                    "cancel_time": (
                        int(order.cancelled_at.timestamp() * 1000)
                        if order.cancelled_at
                        else 0
                    ),
                    "transaction": order.order_id,
                    "state": order.state,
                    "reason": None,
                }
            )
        return {"transactions": transactions}

    # -- Click ------------------------------------------------------------

    async def _click_params(self, request: web.Request) -> dict[str, Any]:
        """Click шлёт form-urlencoded, но встречается и JSON."""
        if request.content_type == "application/json":
            try:
                return await request.json()
            except json.JSONDecodeError:
                return {}
        return dict(await request.post())

    async def handle_click_prepare(self, request: web.Request) -> web.Response:
        params = await self._click_params(request)
        if self.click is None or not self.click.verify_sign(params):
            log.warning("click/prepare: подпись не сошлась")
            return web.json_response(click_error(ClickCode.SIGN_CHECK_FAILED))

        order_id = str(params.get("merchant_trans_id", "")).strip()
        order = self.store.get_order(order_id)
        if order is None:
            return web.json_response(click_error(ClickCode.USER_NOT_FOUND))
        if order.is_paid:
            return web.json_response(click_error(ClickCode.ALREADY_PAID))
        if order.state in (OrderState.CANCELLED, OrderState.REFUNDED):
            return web.json_response(click_error(ClickCode.TRANSACTION_CANCELLED))
        if not click_amount_matches(order.amount, params.get("amount")):
            return web.json_response(click_error(ClickCode.INCORRECT_AMOUNT))

        click_trans_id = str(params.get("click_trans_id", ""))
        self.store.update_order(
            order.order_id, provider="click", provider_trans_id=click_trans_id,
            prepare_id=click_trans_id,
        )
        return web.json_response(
            click_error(
                ClickCode.SUCCESS,
                click_trans_id=params.get("click_trans_id"),
                merchant_trans_id=order.order_id,
                merchant_prepare_id=click_trans_id,
            )
        )

    async def handle_click_complete(self, request: web.Request) -> web.Response:
        params = await self._click_params(request)
        if self.click is None or not self.click.verify_sign(params):
            log.warning("click/complete: подпись не сошлась")
            return web.json_response(click_error(ClickCode.SIGN_CHECK_FAILED))

        order_id = str(params.get("merchant_trans_id", "")).strip()
        order = self.store.get_order(order_id)
        if order is None:
            return web.json_response(click_error(ClickCode.USER_NOT_FOUND))
        if str(order.prepare_id) != str(params.get("merchant_prepare_id", "")):
            return web.json_response(click_error(ClickCode.TRANSACTION_NOT_FOUND))

        # Click сообщает свой исход в поле error: отрицательное значит отказ.
        click_error_code = int(params.get("error", 0) or 0)
        if click_error_code < 0:
            self.store.cancel_order(order.order_id, f"click error {click_error_code}")
            return web.json_response(click_error(ClickCode.TRANSACTION_CANCELLED))

        if order.is_paid:
            return web.json_response(click_error(ClickCode.ALREADY_PAID))
        if not click_amount_matches(order.amount, params.get("amount")):
            return web.json_response(click_error(ClickCode.INCORRECT_AMOUNT))

        await self._credit(order, str(params.get("click_trans_id", "")))
        return web.json_response(
            click_error(
                ClickCode.SUCCESS,
                click_trans_id=params.get("click_trans_id"),
                merchant_trans_id=order.order_id,
                merchant_confirm_id=order.order_id,
            )
        )

    # -- жизненный цикл ---------------------------------------------------

    async def start(self) -> None:
        self._runner = web.AppRunner(self.build_app())
        await self._runner.setup()
        site = web.TCPSite(
            self._runner, self.settings.webhook_host, self.settings.webhook_port
        )
        await site.start()
        log.info(
            "эндпоинты оплаты слушают %s:%s%s (снаружи: %s)",
            self.settings.webhook_host,
            self.settings.webhook_port,
            self.settings.webhook_path,
            self.settings.public_base_url or "не настроено",
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

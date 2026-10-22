"""HTTP endpoint that receives Crypto Pay invoice notifications.

Runs inside the bot's event loop alongside Telegram long-polling.  Crypto Pay
only delivers to a public HTTPS URL, so point a tunnel at this port locally and
put the real domain in ``PUBLIC_BASE_URL`` in production.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

from aiohttp import web

from .billing import SIGNATURE_HEADER, Plan, parse_invoice_paid, verify_signature
from .config import Settings
from .storage import Invoice, Store

log = logging.getLogger(__name__)

PaidCallback = Callable[[int, Plan, Invoice], Awaitable[None]]


class BillingWebhook:
    """Serves ``POST {webhook_path}`` and ``GET /health``."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        plans: dict[str, Plan],
        on_paid: PaidCallback | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.plans = plans
        self.on_paid = on_paid
        self._runner: web.AppRunner | None = None

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_post(self.settings.webhook_path, self.handle_update)
        app.router.add_get("/health", self.handle_health)
        return app

    async def handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "billing": self.settings.billing_enabled,
                "testnet": self.settings.cryptobot_testnet,
                "subscribers": len(self.store.all()),
            }
        )

    async def handle_update(self, request: web.Request) -> web.Response:
        body = await request.read()
        signature = request.headers.get(SIGNATURE_HEADER, "")

        if not verify_signature(self.settings.cryptobot_token, body, signature):
            log.warning("rejected a billing webhook with a bad signature")
            return web.json_response({"error": "bad signature"}, status=401)

        try:
            update = json.loads(body)
        except json.JSONDecodeError:
            return web.json_response({"error": "bad json"}, status=400)

        parsed = parse_invoice_paid(update)
        if parsed is None:
            # Not an invoice_paid update, or one we cannot attribute. Ack it so
            # Crypto Pay stops retrying.
            return web.json_response({"ok": True, "ignored": True})

        invoice_id, chat_id, plan_code = parsed
        await self.settle(invoice_id, chat_id, plan_code)
        return web.json_response({"ok": True})

    async def settle(self, invoice_id: int, chat_id: int, plan_code: str) -> bool:
        """Grant access for a paid invoice, exactly once.

        Shared by the webhook and the poll-based fallback, so a duplicate
        delivery or a race between the two cannot double-credit anyone.
        """
        settled = self.store.mark_invoice_paid(invoice_id)
        if settled is None:
            log.info("invoice %s already settled, ignoring", invoice_id)
            return False

        plan = self.plans.get(plan_code)
        days = plan.days if plan else settled.days
        self.store.grant_access(chat_id, days)
        log.info("granted %s days to chat %s for invoice %s", days, chat_id, invoice_id)

        if self.on_paid is not None and plan is not None:
            try:
                await self.on_paid(chat_id, plan, settled)
            except Exception:  # a failed notification must not undo the grant
                log.exception("could not notify chat %s about invoice %s", chat_id, invoice_id)
        return True

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        self._runner = web.AppRunner(self.build_app())
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.settings.webhook_host, self.settings.webhook_port)
        await site.start()
        log.info(
            "billing webhook listening on %s:%s%s (public: %s)",
            self.settings.webhook_host,
            self.settings.webhook_port,
            self.settings.webhook_path,
            self.settings.webhook_url or "not configured",
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

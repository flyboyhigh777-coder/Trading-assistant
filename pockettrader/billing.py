"""Crypto Pay billing (@CryptoBot on mainnet, @CryptoTestnetBot on testnet).

Two separate apps exist by design: create one in @CryptoTestnetBot for local
work and one in @CryptoBot for production, and point each at its own public
webhook URL.  ``CRYPTOBOT_TESTNET`` picks which API host this process talks to,
so the only thing that changes between environments is the env file.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from typing import Any, Self

import httpx

log = logging.getLogger(__name__)

MAINNET_API = "https://pay.crypt.bot/api"
TESTNET_API = "https://testnet-pay.crypt.bot/api"
SIGNATURE_HEADER = "crypto-pay-api-signature"


class BillingError(RuntimeError):
    """Crypto Pay refused a call or returned something unusable."""


@dataclass(frozen=True)
class Plan:
    """A purchasable access period."""

    code: str
    title: str
    days: int
    amount: str
    asset: str = "USDT"

    def describe(self) -> str:
        return f"{self.title} — {self.amount} {self.asset} / {self.days} days"


DEFAULT_PLANS: tuple[Plan, ...] = (
    Plan("week", "1 week", 7, "5"),
    Plan("month", "1 month", 30, "15"),
    Plan("quarter", "3 months", 90, "35"),
)


def plans_by_code(plans: tuple[Plan, ...] = DEFAULT_PLANS) -> dict[str, Plan]:
    return {p.code: p for p in plans}


@dataclass(frozen=True)
class CreatedInvoice:
    invoice_id: int
    pay_url: str
    amount: str
    asset: str
    status: str


class CryptoPayClient:
    """Minimal async client for the Crypto Pay API."""

    def __init__(
        self,
        token: str,
        testnet: bool = True,
        timeout: float = 15.0,
        base_url: str | None = None,
    ) -> None:
        if not token:
            raise BillingError("Crypto Pay token is empty; set CRYPTOBOT_TOKEN")
        self.token = token
        self.testnet = testnet
        self.base_url = (base_url or (TESTNET_API if testnet else MAINNET_API)).rstrip("/")
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self._timeout,
                headers={"Crypto-Pay-API-Token": self.token},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def _call(self, method: str, **params: Any) -> Any:
        try:
            response = await self.client.post(f"/{method}", json=params)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            raise BillingError(f"Crypto Pay {method} failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise BillingError(f"Crypto Pay {method} returned non-JSON") from exc
        if not body.get("ok"):
            error = body.get("error", {})
            raise BillingError(
                f"Crypto Pay {method} error {error.get('code', '?')}: "
                f"{error.get('name', body)}"
            )
        return body.get("result")

    # -- API surface ------------------------------------------------------

    async def get_me(self) -> dict[str, Any]:
        """Verify the token and return the app it belongs to."""
        return await self._call("getMe")

    async def create_invoice(
        self,
        plan: Plan,
        chat_id: int,
        description: str | None = None,
        expires_in: int = 3600,
    ) -> CreatedInvoice:
        result = await self._call(
            "createInvoice",
            asset=plan.asset,
            amount=plan.amount,
            description=description or f"PocketTrader signals — {plan.describe()}",
            payload=json.dumps({"chat_id": int(chat_id), "plan": plan.code}),
            expires_in=expires_in,
            allow_comments=False,
            allow_anonymous=False,
        )
        pay_url = result.get("bot_invoice_url") or result.get("pay_url") or ""
        if not pay_url:
            raise BillingError("Crypto Pay returned an invoice without a payment URL")
        return CreatedInvoice(
            invoice_id=int(result["invoice_id"]),
            pay_url=pay_url,
            amount=str(result.get("amount", plan.amount)),
            asset=str(result.get("asset", plan.asset)),
            status=str(result.get("status", "active")),
        )

    async def get_invoice(self, invoice_id: int) -> dict[str, Any] | None:
        """Poll a single invoice — the fallback when a webhook never arrives."""
        result = await self._call("getInvoices", invoice_ids=str(int(invoice_id)))
        items = result.get("items", []) if isinstance(result, dict) else result
        return items[0] if items else None


def verify_signature(token: str, body: bytes, signature: str) -> bool:
    """Check Crypto Pay's webhook signature: HMAC-SHA256 keyed by SHA256(token)."""
    if not signature:
        return False
    secret = hashlib.sha256(token.encode("utf-8")).digest()
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


def parse_invoice_paid(update: dict[str, Any]) -> tuple[int, int, str] | None:
    """Pull ``(invoice_id, chat_id, plan)`` out of an ``invoice_paid`` update.

    Returns None for any other update type, or when the payload we attached at
    invoice creation is missing or malformed.
    """
    if update.get("update_type") != "invoice_paid":
        return None
    invoice = update.get("payload") or {}
    invoice_id = invoice.get("invoice_id")
    if invoice_id is None:
        return None
    try:
        attached = json.loads(invoice.get("payload") or "{}")
        chat_id = int(attached["chat_id"])
        plan = str(attached["plan"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        log.warning("invoice %s paid but its payload is unusable", invoice_id)
        return None
    return int(invoice_id), chat_id, plan

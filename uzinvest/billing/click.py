"""Click SHOP-API.

Click вызывает два наших эндпоинта подряд: Prepare (action=0) — «готовься,
сейчас спишем», и Complete (action=1) — «списали». Каждый вызов подписан md5,
и в подпись входит СЫРАЯ строка суммы из запроса: пересобирать её из Decimal
нельзя, «49000» и «49000.00» дают разные хеши.

Суммы у Click — в СУМАХ, в отличие от Payme, где тийины. Перепутать — ошибиться
ровно в сто раз.

ПРОВЕРЬТЕ ПЕРЕД ПРОДАКШЕНОМ: порядок полей в подписи и коды ошибок взяты по
документации docs.click.uz, но сверить с живым сервисом из этого окружения я
не смог — сеть закрыта.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ..money import to_decimal
from .base import PaymentLink, Plan

log = logging.getLogger(__name__)

PAY_URL = "https://my.click.uz/services/pay"

ACTION_PREPARE = 0
ACTION_COMPLETE = 1


class ClickCode:
    """Коды ответа SHOP-API. Click читает их из поля ``error``."""

    SUCCESS = 0
    SIGN_CHECK_FAILED = -1
    INCORRECT_AMOUNT = -2
    ACTION_NOT_FOUND = -3
    ALREADY_PAID = -4
    USER_NOT_FOUND = -5
    TRANSACTION_NOT_FOUND = -6
    UPDATE_FAILED = -7
    BAD_REQUEST = -8
    TRANSACTION_CANCELLED = -9


NOTES: dict[int, str] = {
    ClickCode.SUCCESS: "Success",
    ClickCode.SIGN_CHECK_FAILED: "SIGN CHECK FAILED",
    ClickCode.INCORRECT_AMOUNT: "Incorrect parameter amount",
    ClickCode.ACTION_NOT_FOUND: "Action not found",
    ClickCode.ALREADY_PAID: "Already paid",
    ClickCode.USER_NOT_FOUND: "User does not exist",
    ClickCode.TRANSACTION_NOT_FOUND: "Transaction does not exist",
    ClickCode.UPDATE_FAILED: "Failed to update user",
    ClickCode.BAD_REQUEST: "Error in request from click",
    ClickCode.TRANSACTION_CANCELLED: "Transaction cancelled",
}


def error_response(code: int, **extra: Any) -> dict[str, Any]:
    """Ответ Click: он всегда HTTP 200, смысл — в полях error/error_note."""
    return {"error": code, "error_note": NOTES.get(code, "Error"), **extra}


@dataclass
class ClickProvider:
    """Ссылка на оплату и проверка подписи входящих вызовов."""

    service_id: str = ""
    merchant_id: str = ""
    secret_key: str = ""
    merchant_user_id: str = ""
    name: str = "click"
    label: str = "Click"

    def is_configured(self) -> bool:
        return bool(self.service_id and self.merchant_id and self.secret_key)

    def payment_link(self, order_id: str, plan: Plan) -> PaymentLink:
        amount = format_amount(plan.price)
        url = (
            f"{PAY_URL}?service_id={self.service_id}"
            f"&merchant_id={self.merchant_id}"
            f"&amount={amount}"
            f"&transaction_param={order_id}"
        )
        return PaymentLink(
            provider=self.name,
            url=url,
            order_id=order_id,
            amount=plan.price,
            title=plan.title,
        )

    # -- подпись ----------------------------------------------------------

    def expected_sign(self, params: dict[str, Any]) -> str:
        """Пересобрать md5-подпись из полей запроса.

        Для Complete в подпись добавляется merchant_prepare_id — между
        merchant_trans_id и amount. Порядок полей имеет значение.
        """
        action = str(params.get("action", ""))
        parts = [
            str(params.get("click_trans_id", "")),
            str(params.get("service_id", "")),
            self.secret_key,
            str(params.get("merchant_trans_id", "")),
        ]
        if action == str(ACTION_COMPLETE):
            parts.append(str(params.get("merchant_prepare_id", "")))
        parts.extend(
            [
                str(params.get("amount", "")),
                action,
                str(params.get("sign_time", "")),
            ]
        )
        return hashlib.md5("".join(parts).encode("utf-8")).hexdigest()  # noqa: S324

    def verify_sign(self, params: dict[str, Any]) -> bool:
        """Сравнить подпись. md5 выбран не нами — таков протокол Click.

        Сравниваются байты: подпись приходит снаружи, и ``compare_digest``
        на строках с не-ASCII бросает TypeError.
        """
        received = str(params.get("sign_string", "")).strip().lower()
        if not received:
            return False
        return hmac.compare_digest(
            self.expected_sign(params).encode("ascii"), received.encode("utf-8")
        )


def format_amount(value: Decimal) -> str:
    """Сумма для ссылки: без лишних нулей в дробной части, но целая — цела.

    Наивный ``rstrip("0")`` превратил бы 49000 в 49, то есть выставил бы счёт
    в тысячу раз меньше.
    """
    text = f"{to_decimal(value):f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def amount_matches(expected: Decimal, received: object, tolerance: str = "1") -> bool:
    """Сверить сумму из запроса с ценой тарифа.

    Допуск в один сум: Click иногда присылает сумму с копейками после своих
    округлений, и отказывать из-за этого клиенту, который уже заплатил, —
    худший вариант из возможных.
    """
    try:
        return abs(to_decimal(str(received)) - expected) <= to_decimal(tolerance)
    except Exception:  # noqa: BLE001 - любой мусор в поле означает «не сходится»
        return False

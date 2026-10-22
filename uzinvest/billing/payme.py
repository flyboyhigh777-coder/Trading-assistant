"""Payme Merchant API (JSON-RPC).

Payme не присылает вебхук «вам заплатили». Он вызывает наш эндпоинт методами
JSON-RPC и ждёт строго определённых ответов и кодов ошибок; неверный ответ он
трактует как отказ и деньги не проводит. Поэтому здесь реализованы все шесть
методов протокола, а не один обработчик.

Сценарий: CheckPerformTransaction (можно ли?) → CreateTransaction (держим) →
PerformTransaction (деньги списаны) либо CancelTransaction.

ПРОВЕРЬТЕ ПЕРЕД ПРОДАКШЕНОМ. Коды и имена полей взяты по документации Payme,
но сверить их с живой песочницей из этого окружения я не смог — сеть закрыта.
Песочница: сначала прогоните сценарии в кабинете Payme Business.
"""

from __future__ import annotations

import base64
import binascii
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ..money import to_tiyin
from .base import PaymentLink, Plan

log = logging.getLogger(__name__)

CHECKOUT_URL = "https://checkout.paycom.uz"
TEST_CHECKOUT_URL = "https://test.paycom.uz"


class PaymeCode:
    """Коды ошибок протокола. Payme различает их по значению, не по тексту."""

    INVALID_AMOUNT = -31001
    TRANSACTION_NOT_FOUND = -31003
    CANNOT_CANCEL = -31007
    CANNOT_PERFORM = -31008
    ORDER_NOT_FOUND = -31050        # из диапазона -31050..-31099 для своих ошибок
    ORDER_ALREADY_PAID = -31051
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    UNAUTHORIZED = -32504


#: Сообщения об ошибке Payme требует на трёх языках сразу.
MESSAGES: dict[int, dict[str, str]] = {
    PaymeCode.INVALID_AMOUNT: {
        "ru": "Неверная сумма",
        "uz": "Noto'g'ri summa",
        "en": "Invalid amount",
    },
    PaymeCode.ORDER_NOT_FOUND: {
        "ru": "Заказ не найден",
        "uz": "Buyurtma topilmadi",
        "en": "Order not found",
    },
    PaymeCode.ORDER_ALREADY_PAID: {
        "ru": "Заказ уже оплачен",
        "uz": "Buyurtma allaqachon to'langan",
        "en": "Order already paid",
    },
    PaymeCode.TRANSACTION_NOT_FOUND: {
        "ru": "Транзакция не найдена",
        "uz": "Tranzaksiya topilmadi",
        "en": "Transaction not found",
    },
    PaymeCode.CANNOT_PERFORM: {
        "ru": "Невозможно выполнить операцию",
        "uz": "Amalni bajarib bo'lmaydi",
        "en": "Unable to perform operation",
    },
    PaymeCode.CANNOT_CANCEL: {
        "ru": "Невозможно отменить операцию",
        "uz": "Amalni bekor qilib bo'lmaydi",
        "en": "Unable to cancel operation",
    },
    PaymeCode.UNAUTHORIZED: {
        "ru": "Недостаточно прав",
        "uz": "Huquqlar yetarli emas",
        "en": "Insufficient privileges",
    },
}


class PaymeError(Exception):
    """Ошибка протокола, которую надо вернуть Payme в поле ``error``."""

    def __init__(self, code: int, data: str | None = None):
        self.code = code
        self.data = data
        super().__init__(f"payme error {code}")

    def to_json(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "code": self.code,
            "message": MESSAGES.get(
                self.code, {"ru": "Ошибка", "uz": "Xato", "en": "Error"}
            ),
        }
        if self.data:
            body["data"] = self.data
        return body


@dataclass
class PaymeProvider:
    """Ссылки на оплату и проверка авторизации входящих вызовов."""

    merchant_id: str = ""
    merchant_key: str = ""
    account_field: str = "order_id"
    test_mode: bool = True
    name: str = "payme"
    label: str = "Payme"

    def is_configured(self) -> bool:
        return bool(self.merchant_id and self.merchant_key)

    @property
    def checkout_url(self) -> str:
        return TEST_CHECKOUT_URL if self.test_mode else CHECKOUT_URL

    def payment_link(self, order_id: str, plan: Plan) -> PaymentLink:
        """Ссылка на кассу. Параметры кодируются в base64, сумма — в тийинах."""
        params = (
            f"m={self.merchant_id};"
            f"ac.{self.account_field}={order_id};"
            f"a={to_tiyin(plan.price)};"
            f"l=ru"
        )
        encoded = base64.b64encode(params.encode("utf-8")).decode("ascii")
        return PaymentLink(
            provider=self.name,
            url=f"{self.checkout_url}/{encoded}",
            order_id=order_id,
            amount=plan.price,
            title=plan.title,
        )

    # -- авторизация ------------------------------------------------------

    def check_auth(self, authorization_header: str | None) -> None:
        """Проверить Basic-заголовок вида ``Paycom:<ключ>``.

        Payme шлёт логин ``Paycom`` и ключ кассы. Несовпадение — это −32504,
        а не 401: Payme читает код из тела, а не из статуса HTTP.
        """
        if not authorization_header or not authorization_header.lower().startswith(
            "basic "
        ):
            raise PaymeError(PaymeCode.UNAUTHORIZED)
        try:
            decoded = base64.b64decode(authorization_header[6:].strip()).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise PaymeError(PaymeCode.UNAUTHORIZED) from exc
        login, _, key = decoded.partition(":")
        if login != "Paycom" or not _secure_equals(key, self.merchant_key):
            raise PaymeError(PaymeCode.UNAUTHORIZED)


def _secure_equals(left: str, right: str) -> bool:
    """Сравнение за постоянное время.

    Сравнивать именно байты: ``compare_digest`` на строках бросает TypeError,
    если в них есть не-ASCII, а значение приходит снаружи — достаточно прислать
    ключ с кириллицей, чтобы вместо «нет прав» получить необработанную ошибку.
    """
    import hmac

    return bool(right) and hmac.compare_digest(
        left.encode("utf-8"), right.encode("utf-8")
    )


def now_ms() -> int:
    """Payme оперирует миллисекундами эпохи."""
    return int(time.time() * 1000)


def amount_matches(expected: Decimal, received: object) -> bool:
    """Сравнить сумму из запроса (в тийинах) с ценой тарифа."""
    try:
        return int(received) == to_tiyin(expected)
    except (TypeError, ValueError):
        return False

#!/usr/bin/env python3
"""Отправить боту подписанный ``invoice_paid`` — как это сделал бы Crypto Pay.

Позволяет проверить весь путь оплаты (подпись → начисление → сообщение юзеру),
не создавая приложение в @CryptoTestnetBot и не поднимая туннель.

    export CRYPTOBOT_TOKEN=...        # тот же токен, что у запущенного бота
    python tools/fake_payment.py --invoice 42 --chat 555 --plan week

Инвойс должен существовать и быть неоплаченным: его создаёт /buy. Повторный
запуск с тем же --invoice ничего не начислит — так и задумано, это проверка
защиты от повторной доставки вебхука.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8080/api/billing/webhook")
    parser.add_argument("--invoice", type=int, required=True, help="id инвойса из /buy")
    parser.add_argument("--chat", type=int, required=True, help="чат, которому начислить")
    parser.add_argument("--plan", default="week", help="код тарифа: week, month, quarter")
    parser.add_argument(
        "--unsigned",
        action="store_true",
        help="отправить без подписи — должно вернуть 401",
    )
    args = parser.parse_args()

    token = os.getenv("CRYPTOBOT_TOKEN", "")
    if not token:
        print("error: CRYPTOBOT_TOKEN не задан", file=sys.stderr)
        return 2

    body = json.dumps(
        {
            "update_id": 1,
            "update_type": "invoice_paid",
            "payload": {
                "invoice_id": args.invoice,
                "status": "paid",
                "payload": json.dumps({"chat_id": args.chat, "plan": args.plan}),
            },
        }
    ).encode()

    headers = {"Content-Type": "application/json"}
    if not args.unsigned:
        secret = hashlib.sha256(token.encode()).digest()
        headers["crypto-pay-api-signature"] = hmac.new(
            secret, body, hashlib.sha256
        ).hexdigest()

    try:
        with urllib.request.urlopen(
            urllib.request.Request(args.url, data=body, headers=headers), timeout=10
        ) as response:
            print(response.status, response.read().decode())
    except urllib.error.HTTPError as exc:
        # 401 на --unsigned — это успех теста, а не сбой.
        print(exc.code, exc.read().decode())
        return 0 if args.unsigned and exc.code == 401 else 1
    except urllib.error.URLError as exc:
        print(f"error: бот недоступен на {args.url}: {exc.reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

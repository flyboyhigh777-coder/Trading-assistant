#!/usr/bin/env python3
"""Сыграть за Payme: пройти сценарий оплаты против запущенного бота.

Позволяет проверить весь путь — авторизацию, машину состояний, начисление
доступа, сообщение пользователю — не заводя кассу и не поднимая туннель.

    export PAYME_MERCHANT_KEY=...            # тот же ключ, что у бота
    python tools/fake_payme.py --order UZ1A2B3C4D5E --amount 49000

Повторный запуск с тем же --order ничего не начислит: это проверка
идемпотентности PerformTransaction.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request


def call(url: str, key: str, method: str, params: dict) -> dict:
    body = json.dumps({"id": 1, "method": method, "params": params}).encode()
    auth = base64.b64encode(f"Paycom:{key}".encode()).decode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except urllib.error.URLError as exc:
        print(f"ошибка: бот недоступен на {url}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def show(method: str, result: dict) -> bool:
    if "error" in result:
        error = result["error"]
        print(f"  {method:<26} ОТКАЗ {error['code']} {error['message']['ru']}")
        return False
    print(f"  {method:<26} ok    {json.dumps(result['result'], ensure_ascii=False)}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8080/api/billing/payme")
    parser.add_argument("--order", required=True, help="номер счёта из /buy")
    parser.add_argument("--amount", type=int, required=True, help="сумма в СУМАХ")
    parser.add_argument("--transaction", default="test-tx-1")
    parser.add_argument("--field", default="order_id", help="account-поле кассы")
    parser.add_argument("--cancel", action="store_true", help="отменить после оплаты")
    parser.add_argument("--bad-key", action="store_true", help="проверить отказ по ключу")
    args = parser.parse_args()

    key = os.getenv("PAYME_MERCHANT_KEY", "")
    if not key:
        print("ошибка: PAYME_MERCHANT_KEY не задан", file=sys.stderr)
        return 2
    if args.bad_key:
        key = "заведомо-неверный-ключ"

    tiyin = args.amount * 100   # Payme считает в тийинах
    account = {args.field: args.order}
    print(f"счёт {args.order}, {args.amount} сум ({tiyin} тийин)")

    if not show("CheckPerformTransaction",
                call(args.url, key, "CheckPerformTransaction",
                     {"amount": tiyin, "account": account})):
        return 1
    if not show("CreateTransaction",
                call(args.url, key, "CreateTransaction",
                     {"id": args.transaction, "time": 0, "amount": tiyin,
                      "account": account})):
        return 1
    if not show("PerformTransaction",
                call(args.url, key, "PerformTransaction", {"id": args.transaction})):
        return 1
    show("CheckTransaction",
         call(args.url, key, "CheckTransaction", {"id": args.transaction}))
    if args.cancel:
        show("CancelTransaction",
             call(args.url, key, "CancelTransaction",
                  {"id": args.transaction, "reason": 5}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

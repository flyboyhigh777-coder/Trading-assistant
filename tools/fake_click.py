#!/usr/bin/env python3
"""Сыграть за Click: Prepare и Complete с настоящей подписью.

    export CLICK_SECRET_KEY=...              # тот же ключ, что у бота
    python tools/fake_click.py --order UZ1A2B3C4D5E --amount 49000

Повторный запуск с тем же --order вернёт «Already paid»: это проверка того,
что повторный Complete не начисляет доступ второй раз.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime


def sign(params: dict, secret: str) -> str:
    parts = [
        str(params["click_trans_id"]),
        str(params["service_id"]),
        secret,
        str(params["merchant_trans_id"]),
    ]
    if str(params["action"]) == "1":
        parts.append(str(params.get("merchant_prepare_id", "")))
    parts += [str(params["amount"]), str(params["action"]), str(params["sign_time"])]
    return hashlib.md5("".join(parts).encode()).hexdigest()  # noqa: S324


def post(url: str, params: dict) -> dict:
    data = urllib.parse.urlencode(params).encode()
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except urllib.error.URLError as exc:
        print(f"ошибка: бот недоступен на {url}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="http://127.0.0.1:8080/api/billing/click")
    parser.add_argument("--order", required=True, help="номер счёта из /buy")
    parser.add_argument("--amount", required=True, help="сумма в СУМАХ")
    parser.add_argument("--service-id", default=os.getenv("CLICK_SERVICE_ID", "12345"))
    parser.add_argument("--trans-id", default="900001")
    parser.add_argument("--fail", action="store_true", help="прислать отказ на Complete")
    parser.add_argument("--bad-sign", action="store_true", help="проверить отказ подписи")
    args = parser.parse_args()

    secret = os.getenv("CLICK_SECRET_KEY", "")
    if not secret:
        print("ошибка: CLICK_SECRET_KEY не задан", file=sys.stderr)
        return 2

    common = {
        "click_trans_id": args.trans_id,
        "service_id": args.service_id,
        "merchant_trans_id": args.order,
        "amount": args.amount,
        "sign_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    prepare = {**common, "action": "0"}
    prepare["sign_string"] = "0" * 32 if args.bad_sign else sign(prepare, secret)
    result = post(f"{args.base}/prepare", prepare)
    print("  Prepare   ", json.dumps(result, ensure_ascii=False))
    if result.get("error") != 0:
        return 1

    complete = {
        **common,
        "action": "1",
        "merchant_prepare_id": result["merchant_prepare_id"],
        "error": "-9" if args.fail else "0",
    }
    complete["sign_string"] = sign(complete, secret)
    print("  Complete  ", json.dumps(post(f"{args.base}/complete", complete),
                                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

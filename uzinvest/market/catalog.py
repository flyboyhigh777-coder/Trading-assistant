"""Каталог выпусков: YAML-файл с параметрами облигаций.

Параметры выпуска (номинал, ставка, частота, даты) не меняются день ото дня и
на бирже толком не публикуются машиночитаемо, поэтому они живут в файле, а не
добываются парсером. Благодаря этому весь расчётный слой работает сразу, даже
когда сайт биржи недоступен или переверстан.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..bonds import Bond, BondError

log = logging.getLogger(__name__)


class CatalogError(ValueError):
    """Каталог не читается или содержит некорректный выпуск."""


def _as_date(value: Any, field_name: str, ticker: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
            try:
                return datetime.strptime(value.strip(), fmt).date()
            except ValueError:
                continue
    raise CatalogError(f"{ticker}: поле {field_name} — не дата: {value!r}")


def bond_from_dict(raw: dict[str, Any]) -> Bond:
    """Собрать выпуск из записи каталога."""
    ticker = str(raw.get("ticker", "?"))
    missing = [k for k in ("isin", "ticker", "face_value", "coupon_rate") if k not in raw]
    if missing:
        raise CatalogError(f"{ticker}: не хватает полей: {', '.join(missing)}")
    try:
        return Bond(
            isin=str(raw["isin"]),
            ticker=ticker,
            issuer=str(raw.get("issuer", "")),
            face_value=raw["face_value"],
            coupon_rate=raw["coupon_rate"],
            issue_date=_as_date(raw.get("issue_date"), "issue_date", ticker),
            maturity_date=_as_date(raw.get("maturity_date"), "maturity_date", ticker),
            per_year=raw.get("per_year"),
            period_days=raw.get("period_days"),
            currency=str(raw.get("currency", "UZS")),
            notes=str(raw.get("notes", "")),
        )
    except BondError as exc:
        raise CatalogError(str(exc)) from exc


def load_catalog(path: str | Path) -> dict[str, Bond]:
    """Прочитать YAML-каталог в словарь {ISIN: Bond}.

    Битая запись не роняет весь каталог: она пропускается с записью в лог,
    остальные выпуски продолжают работать.
    """
    source = Path(path)
    if not source.is_file():
        raise CatalogError(f"каталог не найден: {source}")

    import yaml

    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise CatalogError(f"{source}: некорректный YAML: {exc}") from exc

    entries = data.get("bonds", data) if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise CatalogError(f"{source}: ожидался список выпусков под ключом 'bonds'")

    catalog: dict[str, Bond] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            log.warning("пропускаю запись каталога, это не словарь: %r", entry)
            continue
        try:
            bond = bond_from_dict(entry)
        except CatalogError as exc:
            log.warning("пропускаю выпуск: %s", exc)
            continue
        if bond.isin in catalog:
            log.warning("ISIN %s встречается дважды, беру последний", bond.isin)
        catalog[bond.isin] = bond
    return catalog


def find(catalog: dict[str, Bond], query: str) -> Bond | None:
    """Найти выпуск по ISIN или тикеру, без учёта регистра."""
    probe = query.strip().upper()
    if probe in catalog:
        return catalog[probe]
    for bond in catalog.values():
        if bond.ticker == probe:
            return bond
    return None

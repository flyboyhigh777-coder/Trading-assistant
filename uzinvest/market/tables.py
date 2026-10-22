"""Извлечение таблиц из HTML средствами стандартной библиотеки.

Написано нарочно обобщённо: колонки ищутся по тексту заголовка, а не по
CSS-селекторам. Биржа может переверстать страницу, поменять классы и обёртки —
пока в таблице есть колонка со словом «цена», парсер продолжит работать.
Ни bs4, ни lxml для этого не нужны.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

__all__ = ["Table", "extract_tables", "find_column", "normalize"]

_SPACES = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Схлопнуть пробелы и неразрывные пробелы, обрезать края."""
    return _SPACES.sub(" ", text.replace(" ", " ")).strip()


class Table:
    """Таблица как список строк, с доступом по имени колонки."""

    def __init__(self, rows: list[list[str]]):
        self.rows = rows

    @property
    def header(self) -> list[str]:
        return self.rows[0] if self.rows else []

    @property
    def body(self) -> list[list[str]]:
        return self.rows[1:] if len(self.rows) > 1 else []

    def column_index(self, *needles: str) -> int | None:
        """Номер первой колонки, в заголовке которой встречается одно из слов."""
        return find_column(self.header, *needles)

    def as_dicts(self) -> list[dict[str, str]]:
        """Строки как словари {заголовок: значение}."""
        header = self.header
        out: list[dict[str, str]] = []
        for row in self.body:
            out.append({header[i]: cell for i, cell in enumerate(row) if i < len(header)})
        return out

    def __len__(self) -> int:
        return len(self.rows)

    def __repr__(self) -> str:  # pragma: no cover - отладочное
        return f"<Table {len(self.body)} строк, колонки {self.header}>"


def find_column(header: list[str], *needles: str) -> int | None:
    """Найти колонку по подстроке заголовка, без учёта регистра."""
    lowered = [h.lower() for h in header]
    for needle in needles:
        probe = needle.lower()
        for index, title in enumerate(lowered):
            if probe in title:
                return index
    return None


class _TableParser(HTMLParser):
    """Собирает все <table> на странице, включая вложенные."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._stack: list[list[list[str]]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "table":
            self._stack.append([])
        elif tag == "tr" and self._stack:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None:
            self._row.append(normalize("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(self._row):
                self._stack[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._stack:
            finished = self._stack.pop()
            if finished:
                self.tables.append(finished)

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def close(self) -> None:
        # Незакрытые теги — обычное дело на живых страницах, так что всё,
        # что осталось в стеке, тоже отдаём, а не выбрасываем.
        super().close()
        while self._stack:
            finished = self._stack.pop()
            if finished:
                self.tables.append(finished)


def extract_tables(html: str, min_rows: int = 2) -> list[Table]:
    """Все таблицы страницы, где есть хотя бы ``min_rows`` строк."""
    parser = _TableParser()
    parser.feed(html)
    parser.close()
    return [Table(rows) for rows in parser.tables if len(rows) >= min_rows]

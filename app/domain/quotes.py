"""Проверка дословных цитат.

Вывод модели принимается, только если его цитата действительно есть в документе.
Сравнение идёт по нормализованному тексту: пробелы, переносы, кавычки и тире разных видов
не должны ронять настоящую цитату. Отброшенные выводы возвращаются, а не теряются.
"""
import re
import unicodedata
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, TypeVar

from app import limits

_QUOTES = dict.fromkeys(map(ord, "«»“”„‟″"), '"') | dict.fromkeys(map(ord, "‘’‚′`"), "'")
_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_DROP = dict.fromkeys(map(ord, "­​‌‍﻿"), None)
_SPACE = re.compile(r"\s+")

T = TypeVar("T")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.translate(_DROP).translate(_QUOTES).translate(_DASHES)
    text = text.lower().replace("ё", "е")
    return _SPACE.sub(" ", text).strip()


_GAP = re.compile(r"\s*(?:\[\s*)?(?:\.{3,}|…)(?:\s*\])?\s*")


def fragments(quote: str) -> list[str]:
    """Нормализованные фрагменты цитаты. Пропуск в цитате обозначается многоточием."""
    return [part for part in (normalize(p) for p in _GAP.split(quote or "")) if part]


def check(normalized_document: str, quote: str) -> str:
    """Пустая строка, если цитата подтверждена, иначе причина отказа.

    Цитата без пропусков должна встретиться целиком. Цитата с пропусками: каждый фрагмент
    дословно, по порядку, и все вместе в пределах одного отрезка документа.
    """
    parts = fragments(quote)
    if not parts or any(len(part) < limits.QUOTE_MIN_CHARS for part in parts):
        return f"цитата или её фрагмент короче {limits.QUOTE_MIN_CHARS} знаков"
    start = normalized_document.find(parts[0])
    while start >= 0:
        position, ok = start + len(parts[0]), True
        for part in parts[1:]:
            found = normalized_document.find(part, position, start + limits.QUOTE_SPAN_MAX)
            if found < 0:
                ok = False
                break
            position = found + len(part)
        if ok:
            return ""
        start = normalized_document.find(parts[0], start + 1)
    return "цитаты нет в указанном документе"


def contains(document: str, quote: str) -> bool:
    return check(normalize(document), quote) == ""


@dataclass(frozen=True)
class Dropped:
    item: object
    reason: str


def split_by_quote(
    items: Iterable[T],
    documents: Mapping[str, str],
    quote_of: Callable[[T], str],
    doc_of: Callable[[T], str],
) -> tuple[list[T], list[Dropped]]:
    """Делит выводы на подтверждённые цитатой и отброшенные (с причиной)."""
    normalized = {doc_id: normalize(text) for doc_id, text in documents.items()}
    kept: list[T] = []
    dropped: list[Dropped] = []
    for item in items:
        doc_id = doc_of(item)
        if doc_id not in normalized:
            dropped.append(Dropped(item, f"документ «{doc_id}» не найден среди прочитанных"))
            continue
        reason = check(normalized[doc_id], quote_of(item))
        if reason:
            dropped.append(Dropped(item, reason))
        else:
            kept.append(item)
    return kept, dropped

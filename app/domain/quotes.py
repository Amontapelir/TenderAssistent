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


def contains(document: str, quote: str) -> bool:
    q = normalize(quote)
    if len(q) < limits.QUOTE_MIN_CHARS:
        return False
    return q in normalize(document)


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
        quote = normalize(quote_of(item))
        doc_id = doc_of(item)
        if len(quote) < limits.QUOTE_MIN_CHARS:
            dropped.append(Dropped(item, f"цитата короче {limits.QUOTE_MIN_CHARS} знаков"))
        elif doc_id not in normalized:
            dropped.append(Dropped(item, f"документ «{doc_id}» не найден среди прочитанных"))
        elif quote not in normalized[doc_id]:
            dropped.append(Dropped(item, "цитаты нет в указанном документе"))
        else:
            kept.append(item)
    return kept, dropped

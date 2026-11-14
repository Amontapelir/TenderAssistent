"""Разбор ответов модели: только то, что прошло проверку, попадает в карточку.

Ответ модели — недоверенные данные. Здесь он приводится к строгому виду: известная
категория, непустой текст, цитата. Всё, что не подошло, возвращается с причиной.
"""
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from app.domain import quotes

CATEGORIES = ("experience", "license", "security", "deadline", "staff", "equipment",
              "documents", "restriction", "other")
# По публичным данным нельзя подтвердить, что у компании это есть.
NOT_PROVABLE = ("license", "staff", "equipment", "documents")
STATUSES = ("met", "not_met", "unknown")
IMPORTANCE = ("high", "medium", "low")
CONFIDENCE = ("high", "medium", "low")
MAX_TEXT = 600


@dataclass(frozen=True)
class RawFinding:
    block: str  # "requirements" или "attention"
    category: str
    text: str
    quote: str
    doc_id: str


@dataclass(frozen=True)
class Rejected:
    block: str
    text: str
    reason: str


def _clean(value: Any) -> str:
    return " ".join(value.split())[:MAX_TEXT] if isinstance(value, str) else ""


def parse_extraction(data: Any, doc_id: str, limit: int) -> tuple[list[RawFinding], list[Rejected]]:
    """Ответ модели на чтение одной части документа."""
    found: list[RawFinding] = []
    rejected: list[Rejected] = []
    if not isinstance(data, dict):
        return found, [Rejected("requirements", "", "ответ модели не является объектом")]
    for block, text_key in (("requirements", "requirement"), ("attention", "text")):
        items = data.get(block)
        if items is None:
            continue
        if not isinstance(items, list):
            rejected.append(Rejected(block, "", "список в ответе модели имеет неверный вид"))
            continue
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                rejected.append(Rejected(block, "", "элемент ответа модели имеет неверный вид"))
                continue
            text, quote = _clean(item.get(text_key)), item.get("quote")
            quote = quote.strip() if isinstance(quote, str) else ""
            if not text:
                rejected.append(Rejected(block, "", "вывод без текста"))
            elif not quote:
                rejected.append(Rejected(block, text, "вывод без цитаты"))
            elif i >= limit:
                rejected.append(Rejected(block, text, f"сверх предела {limit} выводов на часть документа"))
            else:
                category = item.get("category")
                category = category if category in CATEGORIES else "other"
                found.append(RawFinding(block, category if block == "requirements" else "other",
                                        text, quote, doc_id))
    return found, rejected


def verify(findings: Iterable[RawFinding], documents: dict[str, str]
           ) -> tuple[list[RawFinding], list[Rejected]]:
    """Оставляет выводы, цитата которых есть в названном документе. Повторы убирает."""
    kept, dropped = quotes.split_by_quote(list(findings), documents,
                                          quote_of=lambda f: f.quote, doc_of=lambda f: f.doc_id)
    rejected = [Rejected(d.item.block, d.item.text, d.reason) for d in dropped]
    unique: list[RawFinding] = []
    seen: set[tuple[str, str]] = set()
    for f in kept:
        for key in ((f.block, quotes.normalize(f.quote)), (f.block, quotes.normalize(f.text))):
            if key in seen:
                break
        else:
            seen.add((f.block, quotes.normalize(f.quote)))
            seen.add((f.block, quotes.normalize(f.text)))
            unique.append(f)
    return unique, rejected


@dataclass(frozen=True)
class Assessment:
    status: str
    gap: str
    action: str
    forced: bool  # модель назвала требование выполненным, но подтвердить это нечем
    importance: str = "medium"  # high: может не пустить или стоит больших денег; low: рутина


def parse_assessments(data: Any, requirements: Sequence[RawFinding]) -> list[Assessment]:
    """Сопоставление требований с профилем. Чего модель не оценила, то «неизвестно»."""
    by_id: dict[str, dict] = {}
    items = data.get("requirements") if isinstance(data, dict) else None
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            by_id.setdefault(item["id"], item)
    result = []
    for i, requirement in enumerate(requirements, 1):
        item = by_id.get(f"r{i}", {})
        status = item.get("status") if item.get("status") in STATUSES else "unknown"
        forced = status == "met" and requirement.category in NOT_PROVABLE
        if forced:
            status = "unknown"
        gap, action = _clean(item.get("gap")), _clean(item.get("action"))
        if forced and not action:
            action = "Проверить внутри компании: публичные данные этого не подтверждают."
        importance = item.get("importance") if item.get("importance") in IMPORTANCE else "medium"
        result.append(Assessment(status, gap, action, forced, importance))
    return result


@dataclass(frozen=True)
class RawHypothesis:
    text: str
    based_on: tuple[str, ...]
    confidence: str


def parse_hypotheses(data: Any, allowed_sources: Iterable[str]
                     ) -> tuple[list[RawHypothesis], list[Rejected]]:
    """Гипотезы о причинах побед. Без ссылки на известный источник гипотеза отбрасывается."""
    allowed = set(allowed_sources)
    kept: list[RawHypothesis] = []
    rejected: list[Rejected] = []
    items = data.get("hypotheses") if isinstance(data, dict) else None
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        text = _clean(item.get("text"))
        refs = item.get("based_on")
        refs = tuple(dict.fromkeys(r for r in refs if isinstance(r, str))) if isinstance(refs, list) else ()
        if not text:
            continue
        known = tuple(r for r in refs if r in allowed)
        if not known:
            rejected.append(Rejected("winning_hypotheses", text, "гипотеза без ссылки на известный источник"))
            continue
        confidence = item.get("confidence") if item.get("confidence") in CONFIDENCE else "low"
        kept.append(RawHypothesis(text, known, confidence))
    return kept, rejected

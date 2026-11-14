"""Отбор файлов закупки для чтения и деление текста на части.

Требования к участнику лежат в нескольких файлах закупки. Читаем действующую редакцию,
без протоколов, в порядке важности. Файлы, которые не взяли, возвращаются с причиной.
"""
from dataclasses import dataclass
from typing import Sequence

from app.domain.models import DocRef

# Роль файла и её место в очереди на чтение (меньше — важнее). None — не читаем.
ROLES: dict[str, tuple[int | None, str]] = {
    "requirements": (0, "требования к заявке и участнику"),
    "notice": (1, "извещение"),
    "contract_draft": (2, "проект контракта"),
    "specification": (3, "описание объекта закупки"),
    "clarification": (4, "разъяснение заказчика"),
    "other": (5, "прочее"),
    "price_justification": (None, "обоснование цены: требований к участнику не содержит"),
    "protocol": (None, "протокол: требований к участнику не содержит"),
}


@dataclass(frozen=True)
class Selected:
    doc: DocRef
    role: str


@dataclass(frozen=True)
class Skipped:
    doc: DocRef
    role: str
    reason: str


def classify(doc: DocRef) -> str:
    title, group = doc.title.lower(), doc.group.lower()
    if group.startswith("протокол") or "отмене документа" in group:
        return "protocol"
    if "обоснование" in title and "цен" in title:
        return "price_justification"
    if "требовани" in title and ("заявк" in title or "участник" in title):
        return "requirements"
    if "проект" in title and ("контракт" in title or "договор" in title):
        return "contract_draft"
    if "описание объекта" in title or "техническ" in title:
        return "specification"
    if group.startswith("разъяснени"):
        return "clarification"
    if "извещени" in title:
        return "notice"
    return "other"


def select(documents: Sequence[DocRef], limit: int) -> tuple[list[Selected], list[Skipped]]:
    chosen: list[Selected] = []
    skipped: list[Skipped] = []
    seen: set[str] = set()
    candidates: list[tuple[int, str, DocRef, str]] = []
    for doc in documents:
        role = classify(doc)
        order, label = ROLES[role]
        if not doc.url:
            skipped.append(Skipped(doc, role, "у файла нет ссылки"))
        elif not doc.active:
            skipped.append(Skipped(doc, role, "недействующая редакция"))
        elif order is None:
            skipped.append(Skipped(doc, role, label))
        elif doc.url in seen:
            skipped.append(Skipped(doc, role, "тот же файл уже отобран"))
        else:
            seen.add(doc.url)
            candidates.append((order, doc.group_date, doc, role))
    # Важные роли вперёд; внутри роли свежие вперёд. Сортировка устойчива к порядку источника.
    candidates.sort(key=lambda c: c[1], reverse=True)
    candidates.sort(key=lambda c: c[0])
    for i, (_, _, doc, role) in enumerate(candidates):
        if i < limit:
            chosen.append(Selected(doc, role))
        else:
            skipped.append(Skipped(doc, role, f"предел {limit} файлов на закупку"))
    return chosen, skipped


def split_text(text: str, max_chars: int) -> list[str]:
    """Делит текст на части не длиннее max_chars по границам абзацев.

    Абзац длиннее предела режется по пробелам. Склейка частей через перевод строки
    возвращает исходный текст с точностью до пробелов на стыках.
    """
    if max_chars < 200:
        raise ValueError("Часть документа не может быть короче 200 знаков.")
    parts: list[str] = []
    current: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal current, size
        if current:
            parts.append("\n".join(current))
            current, size = [], 0

    for paragraph in text.split("\n"):
        while len(paragraph) > max_chars:
            flush()
            cut = paragraph.rfind(" ", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            parts.append(paragraph[:cut])
            paragraph = paragraph[cut:].lstrip()
        if size + len(paragraph) + 1 > max_chars:
            flush()
        current.append(paragraph)
        size += len(paragraph) + 1
    flush()
    return [p for p in parts if p.strip()]

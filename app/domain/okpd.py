"""Совпадение области закупки и компании по кодам ОКПД2 и по словам названия.

Код вида 61.20.11.000 делится на части. Чем больше первых частей совпало, тем ближе области.
Совпадение по словам нужно там, где кода нет: в результатах поиска и у части контрактов.
"""
import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Mapping

_CODE = re.compile(r"^\d{2}(\.\d{1,2}(\.\d{1,2}(\.\d{3})?)?)?$")
_WORD = re.compile(r"[а-яёa-z]{4,}")
STEM_LEN = 5
# Слова, которые есть почти в любом названии закупки и области не различают.
_STOP = {"оказа", "услуг", "нужд", "году", "годах", "поста", "выпол", "работ", "предо",
         "обесп", "закуп", "госуд", "муниц", "бюдже", "учреж", "облас", "котор", "также"}

LEVELS = ("none", "low", "medium", "high")


@dataclass(frozen=True)
class AreaMatch:
    level: str  # high, medium, low, none, unknown
    shared_parts: int  # сколько первых частей кода совпало в лучшем случае
    tender_codes: tuple[str, ...]
    matched_codes: tuple[str, ...]  # коды компании, давшие лучшее совпадение
    profile_share: float  # доля контрактов компании с кодом в той же группе
    explanation: str


def normalize(code: str) -> str:
    code = (code or "").strip()
    return code if _CODE.match(code) else ""


def shared_parts(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a.split("."), b.split(".")):
        if x != y:
            break
        n += 1
    return n


def match_area(tender_codes: Iterable[str], profile_codes: Mapping[str, int]) -> AreaMatch:
    """profile_codes: {код: число контрактов компании с этим кодом}."""
    codes = tuple(dict.fromkeys(c for c in map(normalize, tender_codes) if c))
    profile = {normalize(k): v for k, v in profile_codes.items() if normalize(k) and v > 0}
    if not codes:
        return AreaMatch("unknown", 0, codes, (), 0.0, "У закупки нет кода ОКПД2.")
    if not profile:
        return AreaMatch("unknown", 0, codes, (), 0.0, "В профиле компании нет кодов ОКПД2.")

    best = max(shared_parts(t, p) for t in codes for p in profile)
    matched = tuple(sorted(p for p in profile if any(shared_parts(t, p) == best for t in codes)))
    total = sum(profile.values())
    share = sum(profile[p] for p in matched) / total if best else 0.0

    if best >= 3:
        level, text = "high", "Компания исполняла контракты с тем же видом продукции."
    elif best == 2:
        level = "high" if share >= 0.1 else "medium"
        text = "Компания исполняла контракты в той же группе ОКПД2."
    elif best == 1:
        level, text = "low", "Совпал только класс ОКПД2: область смежная."
    else:
        level, text = "none", "По кодам ОКПД2 совпадений с контрактами компании нет."
        matched = ()
    return AreaMatch(level, best, codes, matched, share, text)


def stems(text: str) -> set[str]:
    """Грубые основы слов: первые буквы слова. Достаточно для сравнения названий."""
    found = {w[:STEM_LEN] for w in _WORD.findall((text or "").lower().replace("ё", "е"))}
    return found - _STOP


def title_terms(titles: Iterable[str], top: int) -> list[tuple[str, int]]:
    """Основы слов, которые чаще всего встречаются в названиях контрактов компании."""
    counter: Counter[str] = Counter()
    for title in titles:
        counter.update(stems(title))
    return [(s, n) for s, n in counter.most_common(top) if n >= 2]


def title_words(titles: Iterable[str], top: int) -> list[str]:
    """Целые слова для поискового запроса: самые частые в названиях контрактов компании.

    Слова считаются по основам (как в title_terms), а в запрос идёт самая частая полная форма
    слова из названий. Основа, встретившаяся меньше двух раз, не берётся."""
    counter: Counter[str] = Counter()
    forms: dict[str, Counter[str]] = {}
    for title in titles:
        words = _WORD.findall((title or "").lower().replace("ё", "е"))
        for stem in {w[:STEM_LEN] for w in words} - _STOP:
            counter[stem] += 1
        for word in words:
            if word[:STEM_LEN] not in _STOP:
                forms.setdefault(word[:STEM_LEN], Counter())[word] += 1
    ranked = sorted((s for s, n in counter.items() if n >= 2), key=lambda s: (-counter[s], s))
    return [min(forms[s].items(), key=lambda kv: (-kv[1], kv[0]))[0] for s in ranked[:max(0, top)]]


def text_match(title: str, terms: Iterable[tuple[str, int]]) -> float:
    """Оценка 0..1: насколько название закупки похоже на названия контрактов компании."""
    weights = dict(terms)
    if not weights:
        return 0.0
    hit = sum(weights[s] for s in stems(title) if s in weights)
    top = sum(sorted(weights.values(), reverse=True)[:6])  # шесть главных слов дают единицу
    return min(1.0, hit / top) if top else 0.0

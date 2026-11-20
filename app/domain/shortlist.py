"""Подборка: какие из найденных поиском закупок стоит смотреть первыми.

Балл от 0 до 100 строится из того, что известно по результатам поиска, без чтения документации:
сходство названия закупки с названиями прошлых контрактов компании, опыт компании у этого
заказчика и размер закупки относительно самого крупного контракта компании. Кода ОКПД2 в
результатах поиска нет, поэтому порядок предварительный: точную оценку даёт анализ карточки.

Балл не вероятность победы. Он отвечает на вопрос «насколько закупка похожа на то, что компания
уже делает». Те же признаки считаются по прошлым закупкам (check), и результат проверки хранится
рядом с подборкой.
"""
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from app import limits
from app.domain import backtest, okpd
from app.domain.candidates import Candidate
from app.domain.profile import CompanyProfile


@dataclass(frozen=True)
class Weights:
    text: int = 70  # сходство названия (0..1) умножается на это число
    wins_many_from: int = 3  # столько и больше контрактов компании у заказчика: «много»
    wins_many: int = 25
    wins_some: int = 15
    larger_than_usual: int = -15  # закупка крупнее любого известного контракта компании


DEFAULT_WEIGHTS = Weights()


def score(text_match: float, wins: int, larger: bool, weights: Weights = DEFAULT_WEIGHTS) -> int:
    points = weights.text * text_match
    if wins >= weights.wins_many_from:
        points += weights.wins_many
    elif wins >= 1:
        points += weights.wins_some
    if larger:
        points += weights.larger_than_usual
    return max(0, min(100, round(points)))


@dataclass(frozen=True)
class QueryLine:
    """Один поиск подборки."""

    word: str
    found: int  # сколько закупок вернул поиск (все законы)
    from_cache: bool  # ответ взят с диска, запрос не потрачен
    more_pages: bool  # у поиска есть следующая страница, она не читалась


@dataclass(frozen=True)
class Pick:
    candidate: Candidate
    score: int  # 0..100
    reasons: tuple[str, ...]  # почему закупка в подборке, по-русски
    found_by: tuple[str, ...]  # какие слова поиска её нашли
    wins_at_customer: int  # контрактов компании у этого заказчика (из главных заказчиков профиля)
    larger_than_usual: bool


def _reasons(c: Candidate, matched: Sequence[str], wins: int, larger: bool) -> tuple[str, ...]:
    out = []
    if c.text_match > 0:
        words = f", общих слов: {len(matched)}" if matched else ""
        out.append(f"название похоже на контракты компании на {round(c.text_match * 100)}%{words}")
    else:
        out.append("название не похоже на контракты компании: закупку нашёл только поиск")
    if wins:
        out.append(f"компания уже выполняла у этого заказчика контрактов: {wins}")
    else:
        out.append("у этого заказчика нет контрактов компании среди главных")
    if larger:
        out.append("закупка крупнее любого известного контракта компании")
    return tuple(out)


def pick(ranked: Sequence[Candidate], profile: CompanyProfile, found_by: Mapping[str, Sequence[str]],
         top: int = limits.SHORTLIST_TOP) -> list[Pick]:
    """ranked: кандидаты после candidates.rank. В подборку идут только закупки с открытым приёмом."""
    wins_by_customer = {c.inn: c.contracts for c in profile.customers}
    profile_stems = {stem for stem, _ in profile.terms()}
    picks = []
    for c in ranked:
        if not c.is_open:
            continue
        wins = wins_by_customer.get(c.customer_inn, 0)
        larger = c.size_fit == "large"
        matched = sorted(okpd.stems(c.title) & profile_stems)
        picks.append(Pick(
            candidate=c, score=score(c.text_match, wins, larger),
            reasons=_reasons(c, matched, wins, larger),
            found_by=tuple(found_by.get(c.regn, ())), wins_at_customer=wins,
            larger_than_usual=larger))
    picks.sort(key=lambda p: (-p.score, p.candidate.deadline, p.candidate.regn))
    return picks[:max(0, top)]


# --- проверка на прошлых закупках ---

@dataclass(frozen=True)
class Quality:
    """Как подборочный балл разделяет выигранные компанией закупки и чужие у тех же заказчиков.

    Это проверка «похожа ли закупка на дело компании», а не «выиграет ли она»: чужие закупки
    заказчиков в основном не из области компании, и балл различает их в первую очередь по названию.
    """

    separation: backtest.Separation
    verdict: str
    won: int
    other: int
    top_n: int  # сколько закупок с самым высоким баллом взято
    top_won_share: Optional[float]  # доля выигранных компанией среди них
    base_won_share: Optional[float]  # доля выигранных среди всех проверенных
    profile: backtest.ProfileStamp


def check(features: Sequence[backtest.Features], stamp: backtest.ProfileStamp,
          weights: Weights = DEFAULT_WEIGHTS) -> Optional[Quality]:
    """features: признаки прошлых закупок (backtest.collect). None, если сравнивать не с чем."""
    rows = []
    for f in features:
        larger = f.tender_price > 0 and f.typical_price_max > 0 and f.tender_price > f.typical_price_max
        rows.append((score(f.text_match, f.wins_before, larger, weights), f.is_company, f.contract_regn))
    won = [s for s, is_company, _ in rows if is_company]
    other = [s for s, is_company, _ in rows if not is_company]
    if not won or not other:
        return None
    separation = backtest.separation_with_interval([float(s) for s in won], [float(s) for s in other])
    top_n = min(len(rows), max(limits.SHORTLIST_MIN_TOP, len(rows) // 10))
    best = sorted(rows, key=lambda r: (-r[0], r[2]))[:top_n]
    return Quality(
        separation=separation, verdict=backtest.verdict(separation), won=len(won), other=len(other),
        top_n=top_n, top_won_share=round(sum(1 for r in best if r[1]) / top_n, 3),
        base_won_share=round(len(won) / len(rows), 3), profile=stamp)

"""Проверка правил оценки на прошлых закупках.

Контракты компании сравниваются с чужими контрактами тех же заказчиков. Каждая закупка
оценивается только по тому, что было известно до неё: профиль строится из более ранних
контрактов компании, история заказчика берётся до даты закупки.

Чего проверка не показывает: проигранных заявок в источнике нет. «Чужой контракт» значит
только, что закупку выиграла не компания; участвовала ли она, неизвестно.
"""
import bisect
import statistics
from collections import Counter
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from app.domain import chance, okpd, prices
from app.domain import profile as profile_domain
from app.domain.models import Contract

@dataclass(frozen=True)
class Scored:
    contract_regn: str
    tender_regn: str
    published: str
    customer_inn: str
    is_company: bool  # контракт компании (выигранная закупка)
    area_level: str
    wins_before: int  # более ранних контрактов компании с этим заказчиком
    score: Optional[float]  # середина диапазона оценки; None, если оценка «неизвестно»
    level: str


@dataclass(frozen=True)
class Group:
    """Сколько закупок получило каждую оценку."""

    total: int
    high: int
    medium: int
    low: int
    unknown: int
    median_score: Optional[float]


@dataclass(frozen=True)
class FactorLine:
    """Одно значение фактора: сколько выигранных и чужих закупок с ним."""

    factor: str
    value: str
    won: int
    other: int
    won_share: Optional[float]  # доля выигранных среди закупок с этим значением


@dataclass(frozen=True)
class Report:
    company_inn: str
    won: Group  # все выигранные закупки компании
    won_at_customers: Group  # выигранные у заказчиков, для которых есть чужие контракты
    other: Group  # чужие контракты этих заказчиков
    # Вероятность, что случайная выигранная закупка оценена выше случайной чужой
    # (0.5: правила не различают, 1.0: различают всегда). None, если сравнивать нечего.
    separation: Optional[float]
    won_rated_low_share: Optional[float]  # доля выигранных закупок с оценкой «низкий»
    factors: list[FactorLine]
    skipped_early: int  # закупок, до которых у компании было слишком мало контрактов
    skipped_no_tender: int  # контрактов без извещения о закупке
    customers: list[str]  # заказчики, чужие контракты которых вошли в сравнение
    period_from: str
    period_to: str
    limitations: list[str]


def _score(target: Contract, earlier: Sequence[Contract], built: profile_domain.CompanyProfile,
           customer_before: Optional[Sequence[Contract]], is_company: bool) -> Scored:
    area = okpd.match_area([target.okpd] if target.okpd else [], built.code_counts())
    wins = sum(1 for c in earlier if c.customer_inn == target.customer_inn)
    values, skipped = prices.tender_reductions(customer_before or ())
    reductions = prices.reduction_stats(values.values(), skipped)
    size = built.contract_price
    result = chance.estimate(chance.ChanceInput(
        area_level=area.level,
        text_match=okpd.text_match(target.title, built.terms()),
        # История побед у заказчика известна из контрактов самой компании.
        customer_contracts=max(1, len(customer_before or ())),
        company_wins_at_customer=wins,
        no_reduction_share=reductions.no_reduction_share,
        reduction_observations=reductions.observations,
        tender_price=target.tender_price,
        typical_price_max=size.max if size else 0.0,
    ))
    score = None if result.range_low is None else (result.range_low + result.range_high) / 2
    return Scored(target.regn, target.tender_regn, target.published, target.customer_inn,
                  is_company, area.level, wins, score, result.level)


def _group(rows: Sequence[Scored]) -> Group:
    counts = Counter(r.level for r in rows)
    scores = sorted(r.score for r in rows if r.score is not None)
    return Group(len(rows), counts["high"], counts["medium"], counts["low"], counts["unknown"],
                 statistics.median(scores) if scores else None)


def separation(won: Sequence[float], other: Sequence[float]) -> Optional[float]:
    """Доля пар (выигранная, чужая), где выигранная оценена выше; равные оценки дают половину."""
    if not won or not other:
        return None
    ordered = sorted(other)
    below = equal = 0
    for value in won:
        lo, hi = bisect.bisect_left(ordered, value), bisect.bisect_right(ordered, value)
        below += lo
        equal += hi - lo
    return round((below + equal / 2) / (len(won) * len(ordered)), 3)


def _factor(name: str, rows: Sequence[Scored], key) -> list[FactorLine]:
    won, other = Counter(), Counter()
    for r in rows:
        (won if r.is_company else other)[key(r)] += 1
    lines = []
    for value in sorted(set(won) | set(other)):
        total = won[value] + other[value]
        lines.append(FactorLine(name, value, won[value], other[value],
                                round(won[value] / total, 3) if total else None))
    return lines


def evaluate(company_inn: str, company_contracts: Sequence[Contract],
             customer_contracts: Mapping[str, Sequence[Contract]], min_history: int
             ) -> tuple[Report, list[Scored]]:
    """customer_contracts: ИНН заказчика -> все его контракты (поставщик в них не назван)."""
    own = sorted({c.regn: c for c in company_contracts}.values(),
                 key=lambda c: (c.published, c.regn))
    own_regns = {c.regn for c in own}
    profiles: dict[str, tuple[list[Contract], profile_domain.CompanyProfile]] = {}

    def before(day: str) -> tuple[list[Contract], profile_domain.CompanyProfile]:
        if day not in profiles:
            earlier = [c for c in own if c.published < day]
            profiles[day] = (earlier, profile_domain.build_profile(company_inn, earlier))
        return profiles[day]

    rows: list[Scored] = []
    skipped_early = skipped_no_tender = 0

    def add(target: Contract, is_company: bool) -> None:
        nonlocal skipped_early, skipped_no_tender
        if not target.tender_regn or not target.published:
            skipped_no_tender += 1
            return
        earlier, built = before(target.published)
        if len(earlier) < min_history:
            skipped_early += 1
            return
        history = customer_contracts.get(target.customer_inn)
        customer_before = None if history is None else [
            c for c in history if c.published < target.published and c.regn != target.regn]
        rows.append(_score(target, earlier, built, customer_before, is_company))

    for contract in own:
        add(contract, True)
    for inn in sorted(customer_contracts):
        for contract in sorted(customer_contracts[inn], key=lambda c: (c.published, c.regn)):
            if contract.regn not in own_regns:
                add(contract, False)

    won = [r for r in rows if r.is_company]
    other = [r for r in rows if not r.is_company]
    compared = set(customer_contracts)
    won_there = [r for r in won if r.customer_inn in compared]
    pairs = won_there + other
    days = [r.published for r in rows]
    report = Report(
        company_inn=company_inn,
        won=_group(won), won_at_customers=_group(won_there), other=_group(other),
        separation=separation([r.score for r in won_there if r.score is not None],
                              [r.score for r in other if r.score is not None]),
        won_rated_low_share=round(sum(r.level == "low" for r in won) / len(won), 3) if won else None,
        factors=(_factor("область по кодам", pairs, lambda r: r.area_level)
                 + _factor("победы у заказчика раньше", pairs,
                           lambda r: "3 и больше" if r.wins_before >= 3
                           else ("1-2" if r.wins_before else "нет"))
                 + _factor("оценка", pairs, lambda r: r.level)),
        skipped_early=skipped_early, skipped_no_tender=skipped_no_tender,
        customers=sorted(compared),
        period_from=min(days) if days else "", period_to=max(days) if days else "",
        limitations=[
            "Проигранных заявок в источнике нет. Чужой контракт значит только, что закупку "
            "выиграла не компания; участвовала ли она, неизвестно.",
            "Заказчики для сравнения выбраны среди тех, у кого компания уже выигрывала: "
            "на незнакомых заказчиках правила не проверены.",
            "В проверке нет чтения документации и поставщиков прошлых закупок: оценка здесь "
            "строится на области, истории побед у заказчика, снижении цены и размере закупки.",
            "Учтены только контракты по 44-ФЗ, заключённые по итогам закупки с извещением.",
        ],
    )
    return report, rows

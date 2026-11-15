"""Проверка правил оценки на прошлых закупках.

Контракты компании сравниваются с чужими контрактами тех же заказчиков. Каждая закупка
оценивается только по тому, что было известно до неё: профиль строится из более ранних
контрактов компании, история заказчика берётся до даты закупки.

Чего проверка не показывает: проигранных заявок в источнике нет. «Чужой контракт» значит
только, что закупку выиграла не компания; участвовала ли она, неизвестно.

Два числа вместо одного. Разделение по всем закупкам в основном отвечает на вопрос «похоже ли
это на то, что компания делает»: чужие закупки заказчиков по большей части не из её области.
Разделение среди похожих закупок отвечает на вопрос «выиграет ли она там, где работает»; оно
считается по малой выборке, поэтому у каждого числа есть доверительный интервал.

Признаки закупки (Features) отделены от правил (RuleSet): одни и те же данные можно оценить
разными наборами правил и сравнить результат (compare), например после пополнения профиля.
"""
import bisect
import hashlib
import random
import statistics
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence

from app import limits
from app.domain import chance, okpd, prices
from app.domain import profile as profile_domain
from app.domain.models import Contract

_AREA_ORDER = {"high": 3.0, "medium": 2.0, "unknown": 1.5, "low": 1.0, "none": 0.0}

WORKS, INVERTED, INDISTINGUISHABLE, TOO_FEW = "works", "inverted", "indistinguishable", "too_few"


@dataclass(frozen=True)
class Features:
    """Что известно о закупке на момент до неё. Не зависит от набора правил."""

    contract_regn: str
    tender_regn: str
    published: str
    customer_inn: str
    is_company: bool  # контракт компании (выигранная закупка)
    area_level: str
    text_match: float
    wins_before: int  # более ранних контрактов компании с этим заказчиком
    customer_contracts: int
    no_reduction_share: Optional[float]
    observations: int
    tender_price: float
    typical_price_max: float


@dataclass(frozen=True)
class Scored:
    contract_regn: str
    tender_regn: str
    published: str
    customer_inn: str
    is_company: bool  # контракт компании (выигранная закупка)
    area_level: str
    wins_before: int  # более ранних контрактов компании с этим заказчиком
    text_match: float  # сходство названия с контрактами компании, 0..1
    no_reduction_share: Optional[float]  # доля закупок заказчика без снижения цены до этой даты
    larger_than_usual: bool  # крупнее любого известного контракта компании
    comparable: bool  # закупка похожа на то, что компания делает
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
class Separation:
    """Вероятность, что случайная выигранная закупка оценена выше случайной чужой.

    0,5 значит «не различает», 1,0 «различает всегда». Интервал считается пересэмплированием
    закупок как независимых; закупки одного заказчика зависимы, поэтому настоящая
    неопределённость больше.
    """

    value: Optional[float]
    low: Optional[float]  # нижняя граница 95% интервала
    high: Optional[float]
    positives: int  # выигранных закупок в сравнении
    negatives: int  # чужих закупок в сравнении


@dataclass(frozen=True)
class FactorCheck:
    """Различает ли один фактор выигранные и чужие закупки."""

    factor: str
    overall: Separation
    comparable: Separation
    verdict_overall: str  # works, inverted, indistinguishable, too_few
    verdict_comparable: str


@dataclass(frozen=True)
class ProfileStamp:
    """По какому профилю считалось: после пополнения профиля цифры нужно сравнивать с оговоркой."""

    contracts: int
    period_from: str
    period_to: str
    fingerprint: str  # короткий отпечаток списка контрактов компании


@dataclass(frozen=True)
class Report:
    company_inn: str
    won: Group  # все выигранные закупки компании
    won_at_customers: Group  # выигранные у заказчиков, для которых есть чужие контракты
    other: Group  # чужие контракты этих заказчиков
    separation: Separation  # по всем закупкам: в основном «похоже ли на дело компании»
    separation_comparable: Separation  # среди закупок, похожих на дело компании
    won_rated_low_share: Optional[float]  # доля выигранных закупок с оценкой «низкий»
    factors: list[FactorLine]
    factor_checks: list[FactorCheck]
    profile: ProfileStamp
    rules: chance.RuleSet  # по каким правилам считалась оценка
    skipped_early: int  # закупок, до которых у компании было слишком мало контрактов
    skipped_no_tender: int  # контрактов без извещения о закупке
    customers: list[str]  # заказчики, чужие контракты которых вошли в сравнение
    period_from: str
    period_to: str
    limitations: list[str]


@dataclass(frozen=True)
class Collected:
    features: list[Features]
    skipped_early: int
    skipped_no_tender: int
    customers: list[str]
    profile: ProfileStamp


@dataclass(frozen=True)
class Comparison:
    """Один набор правил на тех же данных."""

    name: str
    separation: Separation
    separation_comparable: Separation
    won_rated_low_share: Optional[float]
    other_rated_high_share: Optional[float]
    won: int
    other: int


def is_comparable(area_level: str, text_match: float) -> bool:
    """Закупка похожа на то, что компания делает: близкий код или сильно похожее название."""
    return area_level in ("high", "medium") or text_match >= limits.COMPARABLE_TEXT_MATCH


# --- сбор признаков ---

def collect(company_inn: str, company_contracts: Sequence[Contract],
            customer_contracts: Mapping[str, Sequence[Contract]], min_history: int) -> Collected:
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

    found: list[Features] = []
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
        values, skipped = prices.tender_reductions(customer_before or ())
        reductions = prices.reduction_stats(values.values(), skipped)
        size = built.contract_price
        area = okpd.match_area([target.okpd] if target.okpd else [], built.code_counts())
        found.append(Features(
            contract_regn=target.regn, tender_regn=target.tender_regn, published=target.published,
            customer_inn=target.customer_inn, is_company=is_company, area_level=area.level,
            text_match=okpd.text_match(target.title, built.terms()),
            wins_before=sum(1 for c in earlier if c.customer_inn == target.customer_inn),
            # Без известной истории заказчика считаем, что в ней один контракт: так считали всегда.
            customer_contracts=max(1, len(customer_before or ())),
            no_reduction_share=reductions.no_reduction_share, observations=reductions.observations,
            tender_price=target.tender_price, typical_price_max=size.max if size else 0.0))

    for contract in own:
        add(contract, True)
    for inn in sorted(customer_contracts):
        for contract in sorted(customer_contracts[inn], key=lambda c: (c.published, c.regn)):
            if contract.regn not in own_regns:
                add(contract, False)

    days = [c.published for c in own if c.published]
    stamp = ProfileStamp(
        contracts=len(own), period_from=min(days) if days else "", period_to=max(days) if days else "",
        fingerprint=hashlib.sha1(",".join(sorted(own_regns)).encode("utf-8")).hexdigest()[:10])
    return Collected(found, skipped_early, skipped_no_tender, sorted(customer_contracts), stamp)


# --- оценка по правилам ---

def rate(features: Sequence[Features], rules: chance.RuleSet = chance.DEFAULT_RULES) -> list[Scored]:
    rows: list[Scored] = []
    for f in features:
        larger = f.tender_price > 0 and f.typical_price_max > 0 and f.tender_price > f.typical_price_max
        result = chance.estimate(chance.ChanceInput(
            area_level=f.area_level, text_match=f.text_match, customer_contracts=f.customer_contracts,
            company_wins_at_customer=f.wins_before, no_reduction_share=f.no_reduction_share,
            reduction_observations=f.observations, tender_price=f.tender_price,
            typical_price_max=f.typical_price_max), rules)
        score = None if result.range_low is None else (result.range_low + result.range_high) / 2
        rows.append(Scored(
            f.contract_regn, f.tender_regn, f.published, f.customer_inn, f.is_company, f.area_level,
            f.wins_before, f.text_match, f.no_reduction_share, larger,
            is_comparable(f.area_level, f.text_match), score, result.level))
    return rows


# --- числа ---

def _group(rows: Sequence[Scored]) -> Group:
    counts = Counter(r.level for r in rows)
    scores = sorted(r.score for r in rows if r.score is not None)
    return Group(len(rows), counts["high"], counts["medium"], counts["low"], counts["unknown"],
                 statistics.median(scores) if scores else None)


def _auc(won: Sequence[float], other: Sequence[float]) -> float:
    ordered = sorted(other)
    below = equal = 0
    for value in won:
        lo, hi = bisect.bisect_left(ordered, value), bisect.bisect_right(ordered, value)
        below += lo
        equal += hi - lo
    return (below + equal / 2) / (len(won) * len(ordered))


def separation(won: Sequence[float], other: Sequence[float]) -> Optional[float]:
    """Доля пар (выигранная, чужая), где выигранная оценена выше; равные оценки дают половину."""
    if not won or not other:
        return None
    return round(_auc(won, other), 3)


def separation_with_interval(won: Sequence[float], other: Sequence[float]) -> Separation:
    """Разделение и 95% интервал пересэмплированием (зерно фиксировано: результат повторяем)."""
    if not won or not other:
        return Separation(None, None, None, len(won), len(other))
    rnd = random.Random(limits.BACKTEST_SEED)
    draws = sorted(_auc(rnd.choices(won, k=len(won)), rnd.choices(other, k=len(other)))
                   for _ in range(limits.BACKTEST_BOOTSTRAP))
    last = len(draws) - 1
    return Separation(round(_auc(won, other), 3), round(draws[int(0.025 * last)], 3),
                      round(draws[int(0.975 * last)], 3), len(won), len(other))


def verdict(s: Separation) -> str:
    """works: интервал целиком выше 0,5 и отклонение не меньше BACKTEST_MIN_EFFECT; inverted: то же
    ниже 0,5; иначе не различает. Узкий интервал вокруг пустяка (почти постоянный признак) не считается.
    При малой выборке вердикта нет."""
    if s.value is None or s.positives < limits.BACKTEST_MIN_GROUP or s.negatives < limits.BACKTEST_MIN_GROUP:
        return TOO_FEW
    if s.low > 0.5 and s.value >= 0.5 + limits.BACKTEST_MIN_EFFECT:
        return WORKS
    if s.high < 0.5 and s.value <= 0.5 - limits.BACKTEST_MIN_EFFECT:
        return INVERTED
    return INDISTINGUISHABLE


_FACTORS: tuple[tuple[str, Callable[[Scored], float]], ...] = (
    ("область по кодам ОКПД2", lambda r: _AREA_ORDER[r.area_level]),
    ("сходство названия с контрактами компании", lambda r: r.text_match),
    ("победы у заказчика раньше", lambda r: float(r.wins_before)),
    ("доля закупок заказчика без снижения цены", lambda r: 0.5 if r.no_reduction_share is None else r.no_reduction_share),
    ("размер закупки: не крупнее обычного", lambda r: 0.0 if r.larger_than_usual else 1.0),
)


def _score_separation(positives: Sequence[Scored], negatives: Sequence[Scored]) -> Separation:
    return separation_with_interval([r.score for r in positives if r.score is not None],
                                    [r.score for r in negatives if r.score is not None])


def _factor_checks(positives: Sequence[Scored], negatives: Sequence[Scored]) -> list[FactorCheck]:
    pos_c = [r for r in positives if r.comparable]
    neg_c = [r for r in negatives if r.comparable]
    checks = []
    for name, value in _FACTORS:
        overall = separation_with_interval([value(r) for r in positives], [value(r) for r in negatives])
        within = separation_with_interval([value(r) for r in pos_c], [value(r) for r in neg_c])
        checks.append(FactorCheck(name, overall, within, verdict(overall), verdict(within)))
    return checks


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


def _share(rows: Sequence[Scored], level: str) -> Optional[float]:
    return round(sum(r.level == level for r in rows) / len(rows), 3) if rows else None


def evaluate(company_inn: str, company_contracts: Sequence[Contract],
             customer_contracts: Mapping[str, Sequence[Contract]], min_history: int,
             rules: chance.RuleSet = chance.DEFAULT_RULES) -> tuple[Report, list[Scored]]:
    got = collect(company_inn, company_contracts, customer_contracts, min_history)
    rows = rate(got.features, rules)
    won = [r for r in rows if r.is_company]
    other = [r for r in rows if not r.is_company]
    compared = set(customer_contracts)
    won_there = [r for r in won if r.customer_inn in compared]
    pairs = won_there + other
    days = [r.published for r in rows]
    report = Report(
        company_inn=company_inn,
        won=_group(won), won_at_customers=_group(won_there), other=_group(other),
        separation=_score_separation(won_there, other),
        separation_comparable=_score_separation([r for r in won_there if r.comparable],
                                                [r for r in other if r.comparable]),
        won_rated_low_share=_share(won, "low"),
        factors=(_factor("область по кодам", pairs, lambda r: r.area_level)
                 + _factor("победы у заказчика раньше", pairs,
                           lambda r: "3 и больше" if r.wins_before >= 3
                           else ("1-2" if r.wins_before else "нет"))
                 + _factor("оценка", pairs, lambda r: r.level)),
        factor_checks=_factor_checks(won_there, other),
        profile=got.profile, rules=rules,
        skipped_early=got.skipped_early, skipped_no_tender=got.skipped_no_tender,
        customers=got.customers,
        period_from=min(days) if days else "", period_to=max(days) if days else "",
        limitations=[
            "Проигранных заявок в источнике нет. Чужой контракт значит только, что закупку "
            "выиграла не компания; участвовала ли она, неизвестно.",
            "Заказчики для сравнения выбраны среди тех, у кого компания уже выигрывала: "
            "на незнакомых заказчиках правила не проверены.",
            "В проверке нет чтения документации и поставщиков прошлых закупок: оценка здесь "
            "строится на области, истории побед у заказчика, снижении цены и размере закупки.",
            "Учтены только контракты по 44-ФЗ, заключённые по итогам закупки с извещением.",
            f"Похожая закупка: код ОКПД2 из той же группы или сходство названия не меньше "
            f"{limits.COMPARABLE_TEXT_MATCH}. Разделение среди похожих закупок считается по малой "
            "выборке: победы идут сериями у немногих заказчиков.",
            "Доверительные интервалы считают закупки независимыми. Закупки одного заказчика "
            "зависимы, поэтому настоящая неопределённость больше показанной.",
            "Разделение по всем закупкам в основном отвечает на вопрос «похоже ли это на дело компании»: "
            "чужие закупки заказчиков по большей части не из её области. Судить о качестве правил "
            "нужно по разделению среди похожих закупок.",
            f"Вердикт по фактору выдаётся, если в каждой группе не меньше {limits.BACKTEST_MIN_GROUP} закупок.",
            "Результат относится к профилю с указанным отпечатком: после пополнения профиля проверку "
            "нужно запустить заново и сравнивать числа с оговоркой о другом профиле.",
        ],
    )
    return report, rows


def compare(company_inn: str, company_contracts: Sequence[Contract],
            customer_contracts: Mapping[str, Sequence[Contract]], min_history: int,
            variants: Mapping[str, chance.RuleSet]) -> list[Comparison]:
    """Оценивает одни и те же закупки разными наборами правил. Признаки собираются один раз."""
    got = collect(company_inn, company_contracts, customer_contracts, min_history)
    compared = set(customer_contracts)
    out = []
    for name, rules in variants.items():
        rows = rate(got.features, rules)
        won = [r for r in rows if r.is_company]
        other = [r for r in rows if not r.is_company]
        won_there = [r for r in won if r.customer_inn in compared]
        out.append(Comparison(
            name=name, separation=_score_separation(won_there, other),
            separation_comparable=_score_separation([r for r in won_there if r.comparable],
                                                    [r for r in other if r.comparable]),
            won_rated_low_share=_share(won, "low"), other_rated_high_share=_share(other, "high"),
            won=len(won), other=len(other)))
    return out

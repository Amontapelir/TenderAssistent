"""Снижение цены, ожидаемая цена победы и маржа.

Снижение = 1 - цена контракта / начальная цена закупки. Это единственная величина
о конкуренции, которая надёжно видна в публичных данных.
"""
import statistics
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Optional

from app import limits
from app.domain.models import Contract


@dataclass(frozen=True)
class ReductionStats:
    observations: int
    skipped: int  # закупки, по которым снижение посчитать нельзя
    median: Optional[float]
    q1: Optional[float]
    q3: Optional[float]
    mean: Optional[float]
    no_reduction_share: Optional[float]
    deep_share: Optional[float]


@dataclass(frozen=True)
class PriceEstimate:
    initial_price: float
    expected_price: Optional[float]
    low_price: Optional[float]
    high_price: Optional[float]
    expected_reduction: Optional[float]
    method: str
    observations: int
    cost: Optional[float]
    margin: Optional[float]
    margin_share: Optional[float]
    note: str


def reduction(contract_price: float, tender_price: float) -> Optional[float]:
    """Доля снижения от начальной цены или None, если посчитать нельзя."""
    if not tender_price or tender_price <= 0 or contract_price is None or contract_price < 0:
        return None
    if contract_price > tender_price * 1.0001:
        return None  # контракт дороже закупки: записи не сопоставимы
    return max(0.0, 1 - contract_price / tender_price)


def tender_reductions(contracts: Iterable[Contract]) -> tuple[dict[str, float], int]:
    """Снижение по каждой закупке. Несколько контрактов одной закупки складываются.

    Возвращает ({номер закупки: снижение}, число закупок без результата).
    """
    by_tender: dict[str, list[Contract]] = defaultdict(list)
    for c in contracts:
        if c.tender_regn:
            by_tender[c.tender_regn].append(c)
    result: dict[str, float] = {}
    skipped = 0
    for regn, group in by_tender.items():
        value = reduction(sum(c.price for c in group), group[0].tender_price)
        if value is None:
            skipped += 1
        else:
            result[regn] = value
    return result, skipped


def reduction_stats(values: Iterable[float], skipped: int = 0) -> ReductionStats:
    data = sorted(values)
    n = len(data)
    if n == 0:
        return ReductionStats(0, skipped, None, None, None, None, None, None)
    if n == 1:
        q1 = q3 = data[0]
    else:
        q1, _, q3 = statistics.quantiles(data, n=4, method="inclusive")
    return ReductionStats(
        observations=n,
        skipped=skipped,
        median=statistics.median(data),
        q1=q1,
        q3=q3,
        mean=statistics.fmean(data),
        no_reduction_share=sum(v <= limits.NO_REDUCTION_EPS for v in data) / n,
        deep_share=sum(v >= limits.DEEP_REDUCTION for v in data) / n,
    )


def estimate_price(
    initial_price: float, stats: ReductionStats, cost: Optional[float] = None
) -> PriceEstimate:
    """Ожидаемая цена победы по истории снижений. Маржа только от заданной себестоимости."""
    if not initial_price or initial_price <= 0:
        return PriceEstimate(0.0, None, None, None, None, "no_initial_price", stats.observations,
                             cost, None, None, "Начальная цена закупки неизвестна.")
    if stats.observations < limits.MIN_PRICE_OBSERVATIONS:
        return PriceEstimate(
            initial_price, None, None, None, None, "insufficient_history", stats.observations,
            cost, None, None,
            f"Наблюдений о снижении цены: {stats.observations}, нужно не меньше "
            f"{limits.MIN_PRICE_OBSERVATIONS}. Ожидаемая цена не называется.",
        )
    expected = round(initial_price * (1 - stats.median), 2)
    low = round(initial_price * (1 - stats.q3), 2)
    high = round(initial_price * (1 - stats.q1), 2)
    margin = margin_share = None
    note = "Маржа не считается: себестоимость не задана."
    if cost is not None and cost >= 0:
        margin = round(expected - cost, 2)
        margin_share = margin / expected if expected > 0 else None
        note = "Маржа посчитана от себестоимости, заданной пользователем. Это допущение."
    return PriceEstimate(initial_price, expected, low, high, stats.median, "median_reduction",
                         stats.observations, cost, margin, margin_share, note)

"""Сводка прошлых контрактов заказчика: кто выигрывал и насколько снижалась цена."""
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional, Sequence

from app import limits
from app.domain import prices
from app.domain.models import Contract


@dataclass(frozen=True)
class Winner:
    inn: str
    contracts: int
    total: float
    is_company: bool


@dataclass(frozen=True)
class HistoryExample:
    contract_regn: str
    tender_regn: str
    published: str
    title: str
    price: float
    tender_price: float
    reduction: Optional[float]
    supplier_inns: tuple[str, ...]


@dataclass(frozen=True)
class CustomerHistory:
    customer_inn: str
    contracts: int
    tenders: int
    suppliers_known: bool
    company_wins: Optional[int]  # None, если поставщики в данных не названы
    company_win_share: Optional[float]
    winners: list[Winner]
    winners_total: int
    reductions: prices.ReductionStats
    examples: list[HistoryExample]
    limitations: list[str]


def summarize(customer_inn: str, contracts: Sequence[Contract], company_inn: str) -> CustomerHistory:
    contracts = [c for c in contracts if c.customer_inn == customer_inn]
    limitations: list[str] = []
    values, skipped = prices.tender_reductions(contracts)
    stats = prices.reduction_stats(values.values(), skipped)

    if not contracts:
        limitations.append("У заказчика не найдено прошлых контрактов: выводы по истории невозможны.")
        return CustomerHistory(customer_inn, 0, 0, False, None, None, [], 0, stats, [], limitations)

    named = [c for c in contracts if c.supplier_inns]
    suppliers_known = len(named) == len(contracts)
    winners: list[Winner] = []
    company_wins = company_share = None
    by_supplier: dict[str, list[float]] = defaultdict(list)
    for c in named:
        for inn in c.supplier_inns:
            by_supplier[inn].append(c.price)
    if named:
        ranked = sorted(by_supplier.items(), key=lambda kv: (-len(kv[1]), -sum(kv[1]), kv[0]))
        winners = [Winner(i, len(p), round(sum(p), 2), i == company_inn)
                   for i, p in ranked[: limits.TOP_CUSTOMERS]]
        company_wins = len(by_supplier.get(company_inn, []))
        company_share = company_wins / len(named)
        if len(by_supplier) > limits.TOP_CUSTOMERS:
            limitations.append(
                f"Показаны {limits.TOP_CUSTOMERS} поставщиков с наибольшим числом контрактов "
                f"из {len(by_supplier)}."
            )
    if not suppliers_known:
        limitations.append(
            f"Поставщик назван в {len(named)} из {len(contracts)} контрактов: "
            "доля побед посчитана только по ним."
        )
    if stats.observations < limits.MIN_PRICE_OBSERVATIONS:
        limitations.append(
            f"Снижение цены известно по {stats.observations} закупкам: для оценки цены этого мало."
        )

    recent = sorted(contracts, key=lambda c: (c.published, c.regn), reverse=True)
    examples = [
        HistoryExample(c.regn, c.tender_regn, c.published, c.title, c.price, c.tender_price,
                       prices.reduction(c.price, c.tender_price) if c.tender_regn else None,
                       c.supplier_inns)
        for c in recent[: limits.HISTORY_EXAMPLES]
    ]
    limitations.append("Участники, которые проиграли, в источнике данных не названы.")
    return CustomerHistory(
        customer_inn=customer_inn,
        contracts=len(contracts),
        tenders=len({c.tender_regn for c in contracts if c.tender_regn}),
        suppliers_known=suppliers_known,
        company_wins=company_wins,
        company_win_share=company_share,
        winners=winners,
        winners_total=len(by_supplier),
        reductions=stats,
        examples=examples,
        limitations=limitations,
    )

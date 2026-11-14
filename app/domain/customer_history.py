"""Сводка прошлых контрактов заказчика: похожие закупки, победители, снижение цены.

В списке контрактов заказчика поставщик не назван. Он известен только у контрактов,
по которым сделан отдельный запрос, и у контрактов самой компании.
"""
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Mapping, Optional, Sequence

from app import limits
from app.domain import okpd, prices
from app.domain.models import Contract


@dataclass(frozen=True)
class Winner:
    inn: str
    name: str
    contracts: int
    total: float
    is_company: bool


@dataclass(frozen=True)
class HistoryExample:
    contract_regn: str
    tender_regn: str
    published: str
    title: str
    okpd: str
    price: float
    tender_price: float
    reduction: Optional[float]
    supplier_inns: tuple[str, ...]
    supplier_names: tuple[str, ...]
    placing_way: str


@dataclass(frozen=True)
class CustomerHistory:
    customer_inn: str
    contracts: int
    period_from: str
    period_to: str
    tenders: int  # контрактов, заключённых по итогам закупки с извещением
    relevant_contracts: int  # похожих на анализируемую закупку
    reductions: prices.ReductionStats  # по всем закупкам заказчика
    relevant_reductions: prices.ReductionStats  # только по похожим
    suppliers_known_for: int  # у скольких похожих контрактов известен поставщик
    winners: list[Winner]  # по похожим контрактам с известным поставщиком
    company_wins: int  # контрактов компании с этим заказчиком (любых, из её профиля)
    examples: list[HistoryExample]  # похожие контракты, свежие вперёд
    limitations: list[str]


def is_relevant(contract: Contract, tender_codes: Sequence[str], tender_title: str) -> bool:
    """Похож ли прошлый контракт на закупку: та же группа ОКПД2 или близкое название."""
    if contract.okpd and any(okpd.shared_parts(contract.okpd, code) >= 2 for code in tender_codes):
        return True
    mine, theirs = okpd.stems(tender_title), okpd.stems(contract.title)
    return len(mine & theirs) >= 3


def summarize(
    customer_inn: str,
    contracts: Sequence[Contract],
    company_inn: str,
    *,
    tender_codes: Sequence[str] = (),
    tender_title: str = "",
    exclude_tender: str = "",
    details: Optional[Mapping[str, Contract]] = None,
    company_contracts: Sequence[Contract] = (),
) -> CustomerHistory:
    """contracts: список контрактов заказчика. details: контракты, по которым известен поставщик.

    exclude_tender: номер анализируемой закупки. Её собственный контракт в историю не входит,
    иначе оценка знала бы исход.
    """
    details = details or {}
    limitations: list[str] = []
    own = {c.regn: c for c in company_contracts if c.customer_inn == customer_inn}
    rows: list[Contract] = []
    for c in contracts:
        if c.customer_inn != customer_inn or (exclude_tender and c.tender_regn == exclude_tender):
            continue
        known = details.get(c.regn) or own.get(c.regn)
        if known and known.supplier_inns:
            c = replace(c, supplier_inns=known.supplier_inns,
                        supplier_names=known.supplier_names, placing_way=known.placing_way)
        rows.append(c)
    company_wins = sum(1 for c in own.values()
                       if not (exclude_tender and c.tender_regn == exclude_tender))

    values, skipped = prices.tender_reductions(rows)
    stats = prices.reduction_stats(values.values(), skipped)
    if not rows:
        limitations.append("У заказчика не найдено прошлых контрактов: выводы по истории невозможны.")
        empty = prices.reduction_stats([])
        return CustomerHistory(customer_inn, 0, "", "", 0, 0, stats, empty, 0, [], company_wins,
                               [], limitations)

    relevant = [c for c in rows if is_relevant(c, tender_codes, tender_title)]
    relevant.sort(key=lambda c: (c.published, c.regn), reverse=True)
    rel_values, rel_skipped = prices.tender_reductions(relevant)
    rel_stats = prices.reduction_stats(rel_values.values(), rel_skipped)

    named = [c for c in relevant if c.supplier_inns]
    by_supplier: dict[str, list[Contract]] = defaultdict(list)
    for c in named:
        for inn in c.supplier_inns:
            by_supplier[inn].append(c)
    ranked = sorted(by_supplier.items(),
                    key=lambda kv: (-len(kv[1]), -sum(c.price for c in kv[1]), kv[0]))
    winners = []
    for inn, group in ranked[: limits.TOP_CUSTOMERS]:
        first = group[0]
        position = first.supplier_inns.index(inn)
        name = first.supplier_names[position] if position < len(first.supplier_names) else ""
        winners.append(Winner(inn, name, len(group), round(sum(c.price for c in group), 2),
                              inn == company_inn))

    if not relevant:
        limitations.append("Похожих закупок у заказчика не найдено: выводы о победителях невозможны.")
    elif len(named) < len(relevant):
        limitations.append(
            f"Поставщик известен у {len(named)} из {len(relevant)} похожих контрактов "
            f"(по одной закупке запрашивается не больше {limits.HISTORY_LOOKUPS})."
        )
    if len(relevant) > limits.HISTORY_EXAMPLES:
        limitations.append(
            f"Показаны {limits.HISTORY_EXAMPLES} последних похожих контрактов из {len(relevant)}."
        )
    if rel_stats.observations < limits.MIN_PRICE_OBSERVATIONS:
        limitations.append(
            f"Снижение цены в похожих закупках известно по {rel_stats.observations} закупкам: "
            "для оценки цены этого мало."
        )
    limitations.append("Участники, которые проиграли, в источнике данных не названы.")
    limitations.append(
        "Победы компании у заказчика посчитаны по её контрактам из профиля: более ранние не видны."
    )

    examples = [
        HistoryExample(c.regn, c.tender_regn, c.published, c.title, c.okpd, c.price, c.tender_price,
                       prices.reduction(c.price, c.tender_price) if c.tender_regn else None,
                       c.supplier_inns, c.supplier_names, c.placing_way)
        for c in relevant[: limits.HISTORY_EXAMPLES]
    ]
    dates = sorted(c.published for c in rows if c.published)
    return CustomerHistory(
        customer_inn=customer_inn,
        contracts=len(rows),
        period_from=dates[0] if dates else "",
        period_to=dates[-1] if dates else "",
        tenders=len({c.tender_regn for c in rows if c.tender_regn}),
        relevant_contracts=len(relevant),
        reductions=stats,
        relevant_reductions=rel_stats,
        suppliers_known_for=len(named),
        winners=winners,
        company_wins=company_wins,
        examples=examples,
        limitations=limitations,
    )


def lookup_candidates(history: CustomerHistory, limit: int) -> list[str]:
    """Номера похожих контрактов без поставщика, свежие вперёд: по ним стоит сделать запрос."""
    return [e.contract_regn for e in history.examples if not e.supplier_inns][:limit]

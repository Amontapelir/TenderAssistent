"""Профиль компании-исполнителя, построенный из её контрактов."""
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Optional, Sequence

from app import limits
from app.domain import okpd, prices
from app.domain.models import Contract


@dataclass(frozen=True)
class CodeShare:
    code: str
    contracts: int
    share: float  # доля среди контрактов, у которых код указан


@dataclass(frozen=True)
class CustomerShare:
    inn: str
    contracts: int
    total: float


@dataclass(frozen=True)
class RegionShare:
    code: str  # первые две цифры ИНН заказчика
    contracts: int


@dataclass(frozen=True)
class SizeStats:
    min: float
    q1: float
    median: float
    q3: float
    max: float


@dataclass(frozen=True)
class Term:
    stem: str
    contracts: int


@dataclass(frozen=True)
class CompanyProfile:
    inn: str
    contracts: int
    period_from: str
    period_to: str
    contracts_with_tender: int
    okpd_codes: list[CodeShare]
    okpd_codes_total: int  # сколько разных кодов всего (в списке только первые)
    okpd_missing: int
    contract_price: Optional[SizeStats]
    customers: list[CustomerShare]
    customers_total: int
    regions: list[RegionShare]
    reductions: prices.ReductionStats
    title_terms: list[Term]
    limitations: list[str]

    def code_counts(self) -> dict[str, int]:
        return {c.code: c.contracts for c in self.okpd_codes}

    def terms(self) -> list[tuple[str, int]]:
        return [(t.stem, t.contracts) for t in self.title_terms]


def _size(values: Sequence[float]) -> Optional[SizeStats]:
    data = sorted(v for v in values if v > 0)
    if not data:
        return None
    if len(data) == 1:
        q1 = q3 = data[0]
    else:
        q1, _, q3 = statistics.quantiles(data, n=4, method="inclusive")
    return SizeStats(data[0], q1, statistics.median(data), q3, data[-1])


def build_profile(inn: str, contracts: Sequence[Contract]) -> CompanyProfile:
    contracts = list(contracts)
    limitations: list[str] = []
    if not contracts:
        return CompanyProfile(inn, 0, "", "", 0, [], 0, 0, None, [], 0, [],
                              prices.reduction_stats([]), [],
                              ["У компании не найдено контрактов: профиль пуст."])

    codes = Counter(c.okpd for c in contracts if c.okpd)
    coded = sum(codes.values())
    missing = len(contracts) - coded
    okpd_codes = [CodeShare(code, n, n / coded) for code, n in codes.most_common(limits.TOP_OKPD)]
    if missing:
        limitations.append(
            f"У {missing} из {len(contracts)} контрактов в реестре нет кода ОКПД2: "
            "область по кодам описана неполно, поэтому учитываются и слова названий."
        )
    if len(codes) > limits.TOP_OKPD:
        limitations.append(f"Показаны {limits.TOP_OKPD} самых частых кодов ОКПД2 из {len(codes)}.")

    by_customer: dict[str, list[float]] = defaultdict(list)
    for c in contracts:
        by_customer[c.customer_inn].append(c.price)
    top_customers = sorted(by_customer.items(), key=lambda kv: (-len(kv[1]), -sum(kv[1]), kv[0]))
    customers = [CustomerShare(i, len(p), round(sum(p), 2))
                 for i, p in top_customers[: limits.TOP_CUSTOMERS]]
    if len(by_customer) > limits.TOP_CUSTOMERS:
        limitations.append(
            f"Показаны {limits.TOP_CUSTOMERS} заказчиков с наибольшим числом контрактов "
            f"из {len(by_customer)}."
        )

    regions = Counter(c.customer_inn[:2] for c in contracts if len(c.customer_inn) >= 2)
    limitations.append(
        "Регион определён по первым двум цифрам ИНН заказчика: это место регистрации, "
        "оно может не совпадать с местом оказания услуг."
    )

    values, skipped = prices.tender_reductions(contracts)
    with_tender = len({c.tender_regn for c in contracts if c.tender_regn})
    without = sum(1 for c in contracts if not c.tender_regn)
    if without:
        limitations.append(
            f"У {without} контрактов нет номера закупки: снижение цены по ним не считается."
        )
    limitations.append(
        "В профиль входят только выигранные закупки. Проигранные в источнике данных не видны."
    )

    dates = sorted(c.published for c in contracts if c.published)
    return CompanyProfile(
        inn=inn,
        contracts=len(contracts),
        period_from=dates[0] if dates else "",
        period_to=dates[-1] if dates else "",
        contracts_with_tender=with_tender,
        okpd_codes=okpd_codes,
        okpd_codes_total=len(codes),
        okpd_missing=missing,
        contract_price=_size([c.price for c in contracts]),
        customers=customers,
        customers_total=len(by_customer),
        regions=[RegionShare(code, n) for code, n in regions.most_common(limits.TOP_REGIONS)],
        reductions=prices.reduction_stats(values.values(), skipped),
        title_terms=[Term(s, n) for s, n in
                     okpd.title_terms((c.title for c in contracts), limits.TOP_TERMS)],
        limitations=limitations,
    )

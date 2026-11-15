"""Проверка правил на прошлых закупках: сбор данных и сохранение итога."""
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping

from app import limits
from app.domain import backtest as backtest_domain
from app.domain import chance
from app.domain.models import Contract
from app.pipeline import steps
from app.schemas.card import BacktestFile, CompareFile, Source
from app.sources import damia_parse
from app.sources.damia import DamiaClient
from app.storage.files import Store


@dataclass
class Gathered:
    own: list[Contract]
    histories: dict[str, list[Contract]]
    sources: list[Source]
    notes: list[str]


def gather(client: DamiaClient, inn: str, *, network: bool = False,
           max_pages: int = limits.PROFILE_MAX_PAGES,
           customers: int = limits.BACKTEST_CUSTOMERS) -> Gathered:
    """Контракты компании и её заказчиков. network=False: только сохранённые ответы.
    Каждый новый заказчик стоит один запрос."""
    own, sources, notes = steps.company_contracts(
        client, inn, network=network, max_pages=max_pages, saved_only_after_first=True)
    notes = [n for n in notes if "44-ФЗ" not in n]  # это уже сказано в ограничениях отчёта

    # Заказчики, у которых компания выигрывала закупки чаще всего; при равенстве по ИНН.
    counts = Counter(c.customer_inn for c in own if c.tender_regn and c.customer_inn)
    order = sorted(counts, key=lambda k: (-counts[k], k))
    limit = max(0, min(customers, limits.BACKTEST_CUSTOMERS))
    histories: dict[str, list[Contract]] = {}
    not_saved = more_pages = 0
    for customer in order:
        if len(histories) >= limit:
            break
        params = dict(inn=customer, fz="44", role="1", format="2")
        reply = client.call("contracts", **params) if network else client.cached("contracts", **params)
        if reply is None:
            not_saved += 1
            continue
        contracts, _ = damia_parse.parse_contracts(reply.data, customer, "1")
        histories[customer] = contracts
        more_pages += damia_parse.contracts_page_full(reply.data, customer)
        sources.append(Source(
            id=f"history:{customer}", kind="contract",
            title=f"Контракты заказчика (ИНН {customer}), 44-ФЗ", used_for=["backtest"],
            comment=f"Получено {reply.fetched_at}, контрактов: {len(contracts)}."))

    notes.append(f"Заказчиков в сравнении: {len(histories)} из {len(order)} "
                 f"(предел {limits.BACKTEST_CUSTOMERS}).")
    if not_saved:
        notes.append(f"Заказчиков, пропущенных потому, что их контрактов нет среди сохранённых, "
                     f"а запрос к API не разрешён: {not_saved}.")
    if more_pages:
        notes.append(f"Заказчиков, у которых взяты только {limits.DAMIA_CONTRACTS_PAGE} самых "
                     f"свежих контрактов (более ранние не запрашивались): {more_pages}.")
    notes.append(f"Закупка не оценивается, если до неё у компании известно меньше "
                 f"{limits.BACKTEST_MIN_HISTORY} контрактов.")
    for source in sources:
        if "backtest" not in source.used_for:
            source.used_for.append("backtest")
    return Gathered(own, histories, sources, notes)


def run(client: DamiaClient, store: Store, inn: str, *, network: bool = False,
        max_pages: int = limits.PROFILE_MAX_PAGES,
        customers: int = limits.BACKTEST_CUSTOMERS,
        rules: chance.RuleSet = chance.DEFAULT_RULES) -> BacktestFile:
    """network=False: только сохранённые ответы. Каждый новый заказчик стоит один запрос."""
    got = gather(client, inn, network=network, max_pages=max_pages, customers=customers)
    report, rows = backtest_domain.evaluate(inn, got.own, got.histories, limits.BACKTEST_MIN_HISTORY, rules)
    result = BacktestFile(generated_at=steps._now(), report=report, items=rows,
                          sources=got.sources, limitations=report.limitations + got.notes)
    store.save("backtest", result)
    return result


def compare(client: DamiaClient, inn: str, variants: Mapping[str, Mapping[str, Any]], *,
            max_pages: int = limits.PROFILE_MAX_PAGES,
            customers: int = limits.BACKTEST_CUSTOMERS) -> CompareFile:
    """Сравнение наборов правил на сохранённых данных. К API не обращается никогда.

    variants: имя -> изменения относительно текущих правил (пустой словарь: текущие правила).
    Текущие правила всегда идут первой строкой."""
    if len(variants) > limits.COMPARE_MAX_VARIANTS:
        raise ValueError(f"Наборов правил не больше {limits.COMPARE_MAX_VARIANTS} за раз.")
    sets: dict[str, chance.RuleSet] = {"текущие правила": chance.DEFAULT_RULES}
    for name, changes in variants.items():
        label = name.strip()
        if not label or len(label) > 60 or label in sets:
            raise ValueError(f"Имя набора правил должно быть уникальным, непустым и не длиннее 60 знаков: {name!r}.")
        sets[label] = chance.rules_from_dict({**changes, "name": label})
    got = gather(client, inn, network=False, max_pages=max_pages, customers=customers)
    rows = backtest_domain.compare(inn, got.own, got.histories, limits.BACKTEST_MIN_HISTORY, sets)
    stamp = backtest_domain.collect(inn, got.own, got.histories, limits.BACKTEST_MIN_HISTORY).profile
    return CompareFile(generated_at=steps._now(), profile=stamp, rows=rows, rules=sets,
                       limitations=[
                           "Все наборы оценивают одни и те же сохранённые закупки; запросы к API не выполняются.",
                           "Сравнивать нужно разделение среди похожих закупок и его интервал, а не первое число. "
                           "Если интервалы наборов пересекаются, выбора между ними данные не дают.",
                           "Набор, подобранный по этим же данным, на них же и выглядит лучше: хорошее число "
                           "проверяют на данных, которых при подборе не было (например, после пополнения профиля)."]
                       + got.notes)

"""Проверка правил на прошлых закупках: сбор данных и сохранение итога."""
from collections import Counter

from app import limits
from app.domain import backtest as backtest_domain
from app.domain.models import Contract
from app.pipeline import steps
from app.schemas.card import BacktestFile, Source
from app.sources import damia_parse
from app.sources.damia import DamiaClient
from app.storage.files import Store


def run(client: DamiaClient, store: Store, inn: str, *, network: bool = False,
        max_pages: int = limits.PROFILE_MAX_PAGES,
        customers: int = limits.BACKTEST_CUSTOMERS) -> BacktestFile:
    """network=False: только сохранённые ответы. Каждый новый заказчик стоит один запрос."""
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

    report, rows = backtest_domain.evaluate(inn, own, histories, limits.BACKTEST_MIN_HISTORY)
    for source in sources:
        if "backtest" not in source.used_for:
            source.used_for.append("backtest")
    result = BacktestFile(generated_at=steps._now(), report=report, items=rows,
                          sources=sources, limitations=report.limitations + notes)
    store.save("backtest", result)
    return result

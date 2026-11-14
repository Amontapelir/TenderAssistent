"""Перевод ответов API закупок в записи домена.

Формат ответов сверен с реальными ответами. Поля читаются по точным именам; запись без
обязательных полей не пропускается молча, а считается и возвращается в числе отброшенных.
"""
from typing import Any

from app.domain import okpd
from app.domain.models import Bid, Contract, DocRef, TenderBrief, TenderCard, TenderContract

LAWS = ("44", "223", "615")


def _num(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse_contracts(response: Any, inn: str, role: str) -> tuple[list[Contract], int]:
    """Ответ метода contracts (format=2). role "0": inn — поставщик, "1": inn — заказчик.

    Возвращает (контракты, число записей, которые не удалось разобрать).
    """
    block = response.get(inn) if isinstance(response, dict) else None
    if not isinstance(block, dict):
        return [], 0
    contracts: list[Contract] = []
    bad = 0
    for regn, item in block.items():
        if not isinstance(item, dict) or not isinstance(item.get("Цена"), dict):
            bad += 1
            continue
        product = item.get("Продукт") or {}
        tender = item.get("Закупка") or {}
        if role == "0":
            customer, suppliers = _text(item.get("ЗаказчикИНН")), (inn,)
        else:
            # Для роли заказчика имя поля с поставщиком реальным ответом не подтверждено.
            supplier = _text(item.get("ПоставщикИНН"))
            customer, suppliers = inn, ((supplier,) if supplier else ())
        contracts.append(Contract(
            regn=str(regn),
            published=_text(item.get("ДатаПубл")),
            customer_inn=customer,
            supplier_inns=suppliers,
            price=_num(item["Цена"].get("Сумма")),
            okpd=okpd.normalize(_text(product.get("ОКПД"))),
            title=_text(product.get("Название")),
            tender_regn=_text(tender.get("НомерИзвещения")),
            tender_price=_num(tender.get("Сумма")),
            status=_text((item.get("Статус") or {}).get("Статус")),
        ))
    return contracts, bad


def parse_tender(response: Any, regn: str) -> TenderCard | None:
    """Ответ метода zakupka. None, если закупки в ответе нет."""
    item = response.get(regn) if isinstance(response, dict) else None
    if not isinstance(item, dict) or not item:
        return None
    customers = item.get("Заказчик")
    customer = customers[0] if isinstance(customers, list) and customers else (
        customers if isinstance(customers, dict) else {})
    product = item.get("Продукт") or {}
    codes = [_text(product.get("ОКПД"))] + [
        _text(o.get("ОКПД")) for o in product.get("ОбъектыЗак") or [] if isinstance(o, dict)]

    documents: list[DocRef] = []
    for group in item.get("Документы") or []:
        if not isinstance(group, dict):
            continue
        for file in group.get("Файлы") or []:
            if isinstance(file, dict):
                documents.append(DocRef(
                    group=_text(group.get("Название")),
                    group_date=_text(group.get("ДатаРазм")),
                    active=_text(group.get("Редакция")).lower() == "действующая",
                    title=_text(file.get("Название")),
                    url=_text(file.get("Url")),
                ))

    bids = tuple(
        Bid(_text(b.get("Номер")), _num(b.get("Сумма")))
        for b in (item.get("Протокол") or {}).get("Заявки") or [] if isinstance(b, dict)
    )
    contracts: list[TenderContract] = []
    for contract_regn, c in (item.get("Контракты") or {}).items():
        if not isinstance(c, dict):
            continue
        suppliers = [s for kind in ("ЮЛ", "ИП", "ФЛ")
                     for s in (c.get("Поставщики") or {}).get(kind) or [] if isinstance(s, dict)]
        contracts.append(TenderContract(
            regn=str(contract_regn),
            signed=_text(c.get("ДатаПодп")),
            price=_num((c.get("Цена") or {}).get("Сумма")),
            supplier_inns=tuple(_text(s.get("ИНН")) for s in suppliers if _text(s.get("ИНН"))),
            supplier_names=tuple(_text(s.get("НаимСокр")) or _text(s.get("НаимПолн"))
                                 for s in suppliers),
        ))

    return TenderCard(
        regn=regn,
        law=str(item.get("ФЗ") or ""),
        title=_text(product.get("Название")),
        region=_text(item.get("Регион")),
        customer_inn=_text(customer.get("ИНН")),
        customer_name=_text(customer.get("НаимСокр")) or _text(customer.get("НаимПолн")),
        placing_way=_text(item.get("СпособРазм")),
        initial_price=_num((item.get("НачЦена") or {}).get("Сумма")),
        published=_text(item.get("ДатаПубл")),
        deadline=_text(item.get("ДатаОконч")),
        status=_text((item.get("Статус") or {}).get("Статус")),
        okpd_codes=tuple(dict.fromkeys(c for c in map(okpd.normalize, codes) if c)),
        bid_security=_num((item.get("ОбеспУчаст") or {}).get("Сумма")),
        contract_security=_num((item.get("ОбеспИсп") or {}).get("Сумма")),
        documents=tuple(documents),
        bids=bids,
        contracts=tuple(contracts),
    )


def has_next_page(response: Any) -> bool:
    value = response.get("next_page") if isinstance(response, dict) else None
    return value is True or (isinstance(value, str) and value.lower() == "true")


def parse_search(response: Any) -> tuple[list[TenderBrief], dict[str, int]]:
    """Ответ метода zsearch. Возвращает (закупки, {закон: число закупок})."""
    tenders: list[TenderBrief] = []
    by_law: dict[str, int] = {}
    if not isinstance(response, dict):
        return tenders, by_law
    for law in LAWS:
        block = response.get(law)
        if not isinstance(block, dict):
            by_law[law] = 0
            continue
        by_law[law] = len(block)
        for regn, item in block.items():
            if not isinstance(item, dict):
                continue
            customer = item.get("Заказчик") or {}
            tenders.append(TenderBrief(
                regn=str(regn),
                law=law,
                title=_text(item.get("Продукт")),
                customer_inn=_text(customer.get("ИНН")),
                customer_name=_text(customer.get("НаимПолн")),
                price=_num(item.get("Сумма")),
                published=_text(item.get("ДатаПубл")),
                deadline=_text(item.get("ДатаОконч")),
                status=_text(item.get("Статус")),
            ))
    return tenders, by_law

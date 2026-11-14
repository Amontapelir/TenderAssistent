"""Записи, с которыми работают расчёты."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Contract:
    """Контракт из реестра. Суммы в рублях."""

    regn: str
    published: str  # дата ГГГГ-ММ-ДД
    customer_inn: str
    supplier_inns: tuple[str, ...]  # пусто, если источник не назвал поставщика
    price: float
    okpd: str  # пусто, если в реестре кода нет
    title: str
    tender_regn: str  # пусто, если контракт заключён без извещения
    tender_price: float  # начальная цена закупки; 0, если неизвестна
    status: str = ""


@dataclass(frozen=True)
class TenderBrief:
    """Строка из результата поиска закупок."""

    regn: str
    law: str  # "44", "223", "615"
    title: str
    customer_inn: str
    customer_name: str
    price: float  # 0, если в поиске не указана
    published: str
    deadline: str  # дата окончания подачи заявок
    status: str = ""

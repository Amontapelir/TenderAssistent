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
class DocRef:
    """Файл, приложенный к закупке."""

    group: str  # название документа-контейнера: извещение, разъяснение, протокол
    group_date: str
    active: bool  # действующая редакция
    title: str
    url: str


@dataclass(frozen=True)
class Bid:
    """Заявка из протокола. Участник в источнике не назван."""

    number: str
    amount: float


@dataclass(frozen=True)
class TenderContract:
    regn: str
    signed: str
    price: float
    supplier_inns: tuple[str, ...]
    supplier_names: tuple[str, ...]


@dataclass(frozen=True)
class TenderCard:
    """Карточка закупки."""

    regn: str
    law: str
    title: str
    region: str
    customer_inn: str
    customer_name: str
    placing_way: str
    initial_price: float
    published: str
    deadline: str
    status: str
    okpd_codes: tuple[str, ...]
    bid_security: float  # 0, если не требуется или не указано
    contract_security: float
    documents: tuple[DocRef, ...]
    bids: tuple[Bid, ...]
    contracts: tuple[TenderContract, ...]


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

"""Список закупок-кандидатов: какие открыты и насколько похожи на контракты компании.

В результатах поиска нет кода ОКПД2, поэтому первичный порядок строится по словам названия
и размеру закупки. Точная оценка появляется после анализа карточки закупки.
"""
from dataclasses import dataclass
from typing import Sequence

from app.domain import okpd
from app.domain.models import TenderBrief
from app.domain.profile import CompanyProfile


@dataclass(frozen=True)
class Candidate:
    regn: str
    law: str
    title: str
    customer_inn: str
    customer_name: str
    price: float
    published: str
    deadline: str
    status: str
    is_open: bool
    text_match: float  # 0..1
    known_customer: bool  # компания уже выигрывала у этого заказчика (среди показанных в профиле)
    size_fit: str  # typical, small, large, unknown


def _size_fit(price: float, profile: CompanyProfile) -> str:
    size = profile.contract_price
    if not price or size is None:
        return "unknown"
    if price > size.max:
        return "large"
    if price < size.min:
        return "small"
    return "typical"


def rank(tenders: Sequence[TenderBrief], profile: CompanyProfile, today: str) -> list[Candidate]:
    """today: дата ГГГГ-ММ-ДД. Открытой считается закупка со сроком не раньше today и без отмены."""
    terms = profile.terms()
    known = {c.inn for c in profile.customers}
    result = [
        Candidate(
            regn=t.regn, law=t.law, title=t.title, customer_inn=t.customer_inn,
            customer_name=t.customer_name, price=t.price, published=t.published,
            deadline=t.deadline, status=t.status,
            is_open=bool(t.deadline) and t.deadline >= today and "отмен" not in t.status.lower(),
            text_match=round(okpd.text_match(t.title, terms), 3),
            known_customer=t.customer_inn in known,
            size_fit=_size_fit(t.price, profile),
        )
        for t in tenders
    ]
    result.sort(key=lambda c: (not c.is_open, not c.known_customer, -c.text_match, c.deadline, c.regn))
    return result

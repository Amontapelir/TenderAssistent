"""Оценка шансов по правилам.

Это не статистическая вероятность. Оценка складывается из названных факторов и выдаётся
диапазоном; ширина диапазона зависит от того, сколько данных было. Правила не откалиброваны
на проигранных закупках: в источнике данных их нет.
"""
from dataclasses import dataclass
from typing import Optional

BASE = 45
LOW, HIGH = 5, 90


@dataclass(frozen=True)
class Factor:
    name: str
    points: int
    explanation: str


@dataclass(frozen=True)
class ChanceInput:
    area_level: str  # high, medium, low, none, unknown
    text_match: Optional[float] = None  # 0..1, сходство названия с контрактами компании
    semantic_level: Optional[str] = None  # оценка области по смыслу (модель): high, medium, low, none
    customer_contracts: int = 0
    company_wins_at_customer: Optional[int] = None
    no_reduction_share: Optional[float] = None
    reduction_observations: int = 0
    average_bids: Optional[float] = None
    blocking_gaps: int = 0  # требования, которым компания не соответствует
    open_questions: int = 0  # требования, по которым соответствие неизвестно
    documents_read: bool = False
    tender_price: float = 0.0
    typical_price_max: float = 0.0  # самый крупный контракт компании


@dataclass(frozen=True)
class Chance:
    level: str  # high, medium, low, unknown
    range_low: Optional[int]
    range_high: Optional[int]
    confidence: str  # medium, low
    factors: list[Factor]
    limitations: list[str]


_AREA = {
    "high": (15, "Область закупки совпадает с контрактами компании."),
    "medium": (5, "Область закупки близка к контрактам компании."),
    "low": (-10, "Область закупки только смежная."),
    "none": (-25, "Область закупки не совпадает с контрактами компании."),
}


def estimate(data: ChanceInput) -> Chance:
    factors: list[Factor] = []
    limitations = [
        "Оценка не является статистической вероятностью: это сумма названных факторов.",
        "Правила не проверены на проигранных закупках: в источнике данных их нет.",
    ]

    area_known = data.area_level in _AREA
    if area_known:
        points, text = _AREA[data.area_level]
        # Код может не совпасть, хотя по смыслу закупка профильная: смягчаем по словам названия.
        if points < 0 and data.semantic_level in ("high", "medium"):
            factors.append(Factor("область", 5 if data.semantic_level == "high" else 0,
                                  text + " Но по смыслу закупка близка к работам компании "
                                  "(оценка модели)."))
        elif points < 0 and data.text_match is not None and data.text_match >= 0.5:
            factors.append(Factor("область", 0, text + " Но название похоже на контракты "
                                  "компании: штраф за область не применён."))
        else:
            factors.append(Factor("область", points, text))
    elif data.text_match is not None:
        points = 10 if data.text_match >= 0.5 else (0 if data.text_match >= 0.2 else -10)
        factors.append(Factor("область", points,
                              "Кода ОКПД2 нет, область оценена по словам названия."))
        area_known = True
    else:
        limitations.append("Область закупки не определена.")

    wins = data.company_wins_at_customer
    if wins is not None and data.customer_contracts > 0:
        if wins >= 3:
            factors.append(Factor("заказчик", 20, f"Компания выигрывала у этого заказчика {wins} раз."))
        elif wins >= 1:
            factors.append(Factor("заказчик", 12, f"Компания выигрывала у этого заказчика {wins} раз."))
        else:
            factors.append(Factor("заказчик", -5, "У этого заказчика компания раньше не выигрывала."))
    else:
        limitations.append("История побед у заказчика неизвестна.")

    if data.average_bids is not None:
        if data.average_bids < 1.5:
            factors.append(Factor("конкуренция", 15, "В прошлых закупках заказчика обычно одна заявка."))
        elif data.average_bids >= 4:
            factors.append(Factor("конкуренция", -10, "В прошлых закупках заказчика четыре заявки и больше."))
        else:
            factors.append(Factor("конкуренция", 0, "В прошлых закупках заказчика две-три заявки."))
    elif data.no_reduction_share is not None and data.reduction_observations >= 5:
        if data.no_reduction_share >= 0.6:
            factors.append(Factor("конкуренция", 8,
                                  "Большинство закупок заказчика прошло без снижения цены."))
        elif data.no_reduction_share <= 0.2:
            factors.append(Factor("конкуренция", -8,
                                  "Почти во всех закупках заказчика цену снижали."))
    else:
        limitations.append("Уровень конкуренции у заказчика неизвестен.")

    if data.blocking_gaps:
        factors.append(Factor("требования", -25 * min(data.blocking_gaps, 2),
                              f"Не выполнено обязательных требований: {data.blocking_gaps}."))
    if data.open_questions:
        # Не штрафуем: по публичным данным большинство требований проверить нельзя.
        factors.append(Factor("требования", 0, "Важных требований, которые нужно проверить "
                                              f"внутри компании: {data.open_questions}."))
    if not data.documents_read:
        limitations.append("Документация не прочитана: требования к участнику не учтены.")

    if data.tender_price > 0 and data.typical_price_max > 0 and data.tender_price > data.typical_price_max:
        factors.append(Factor("размер", -5, "Закупка крупнее любого известного контракта компании."))

    if not area_known and wins is None:
        return Chance("unknown", None, None, "low", factors, limitations)

    score = max(LOW, min(HIGH, BASE + sum(f.points for f in factors)))
    evidence = sum([
        data.area_level in _AREA,
        wins is not None and data.customer_contracts >= 5,
        data.average_bids is not None or data.reduction_observations >= 5,
        data.documents_read,
    ])
    # Выше средней уверенность не ставится, пока правила не проверены на прошлых закупках.
    confidence, half = ("medium", 15) if evidence >= 3 else ("low", 20)
    low, high = max(0, score - half), min(95, score + half)
    level = "high" if score >= 60 else ("medium" if score >= 35 else "low")
    if data.blocking_gaps and level == "high":
        level = "medium"
    return Chance(level, low, high, confidence, factors, limitations)

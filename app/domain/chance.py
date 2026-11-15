"""Оценка шансов по правилам.

Это не статистическая вероятность. Оценка складывается из названных факторов и выдаётся
диапазоном; ширина диапазона зависит от того, сколько данных было. Правила не откалиброваны
на проигранных закупках: в источнике данных их нет.

Все веса и пороги собраны в одном наборе RuleSet. Это нужно, чтобы после пополнения профиля
компании прогнать проверку на прошлых закупках заново и сравнить несколько наборов на одних и
тех же данных (см. backtest.compare), не правя код оценки.
"""
from dataclasses import dataclass, fields, replace
from typing import Any, Mapping, Optional

_AREA_LEVELS = ("high", "medium", "low", "none")


@dataclass(frozen=True)
class RuleSet:
    """Веса и пороги оценки. Значения по умолчанию это текущие правила."""

    name: str = "текущие"
    base: int = 45  # оценка до учёта факторов
    floor: int = 5  # оценка не бывает ниже
    ceiling: int = 90  # и выше
    # область по кодам: очки за уровень совпадения (отрицательные смягчаются смыслом и названием)
    area: tuple[tuple[str, int], ...] = (("high", 15), ("medium", 5), ("low", -10), ("none", -25))
    soften_by_text: float = 0.5  # сходство названия, с которого штраф за область не применяется
    semantic_high: int = 5  # очки, если модель назвала область близкой
    semantic_medium: int = 0
    # кода нет: область оценивается по названию
    unknown_text_high: float = 0.5
    unknown_text_mid: float = 0.2
    unknown_points: tuple[int, int, int] = (10, 0, -10)  # сходство высокое, среднее, низкое
    # победы у этого заказчика
    wins_many_from: int = 3
    wins_many: int = 20
    wins_some: int = 12
    wins_none: int = -5
    # конкуренция по числу заявок (в открытых данных его нет, поле для будущего)
    bids_one_below: float = 1.5
    bids_one: int = 15
    bids_many_from: float = 4.0
    bids_many: int = -10
    # конкуренция по снижению цены в прошлых закупках заказчика
    min_observations: int = 5
    no_reduction_high_share: float = 0.6
    no_reduction_high: int = 8
    no_reduction_low_share: float = 0.2
    no_reduction_low: int = -8
    # требования и размер
    gap_each: int = -25
    gap_max: int = 2
    larger_than_usual: int = -5
    # диапазон, уверенность, уровень
    min_customer_contracts: int = 5  # сколько контрактов заказчика нужно, чтобы считать его историю
    confident_evidence: int = 3
    half_confident: int = 15
    half_unsure: int = 20
    range_floor: int = 0
    range_ceiling: int = 95
    level_high_from: int = 60
    level_medium_from: int = 35


DEFAULT_RULES = RuleSet()


def rules_from_dict(data: Mapping[str, Any], base: Optional[RuleSet] = None) -> RuleSet:
    """Набор правил из словаря изменений (например, из запроса). Неизвестные параметры и
    значения не того вида отвергаются, а не игнорируются."""
    base = base or DEFAULT_RULES
    current = {f.name: getattr(base, f.name) for f in fields(base)}
    changes: dict[str, Any] = {}

    def number(key: str, value: Any, kind: type) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Параметр {key}: нужно число, получено {value!r}.")
        if kind is int and value != int(value):
            raise ValueError(f"Параметр {key}: нужно целое число, получено {value!r}.")
        return kind(value)

    for key, value in data.items():
        if key not in current:
            raise ValueError(f"Неизвестный параметр правил: {key}.")
        old = current[key]
        if key == "area":
            if not isinstance(value, Mapping) or set(value) - set(_AREA_LEVELS):
                raise ValueError(f"Параметр area: нужен словарь с ключами из {_AREA_LEVELS}.")
            merged = dict(old)
            merged.update({k: number("area." + k, v, int) for k, v in value.items()})
            changes[key] = tuple((k, merged[k]) for k in _AREA_LEVELS)
        elif isinstance(old, str):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Параметр {key}: нужна непустая строка.")
            changes[key] = value.strip()
        elif isinstance(old, tuple):
            if not isinstance(value, (list, tuple)) or len(value) != len(old):
                raise ValueError(f"Параметр {key}: нужен список из {len(old)} чисел.")
            changes[key] = tuple(number(key, v, int) for v in value)
        elif isinstance(old, float):
            changes[key] = number(key, value, float)
        else:
            changes[key] = number(key, value, int)
    return replace(base, **changes)


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


_AREA_TEXT = {
    "high": "Область закупки совпадает с контрактами компании.",
    "medium": "Область закупки близка к контрактам компании.",
    "low": "Область закупки только смежная.",
    "none": "Область закупки не совпадает с контрактами компании.",
}


def estimate(data: ChanceInput, rules: RuleSet = DEFAULT_RULES) -> Chance:
    factors: list[Factor] = []
    limitations = [
        "Оценка не является статистической вероятностью: это сумма названных факторов.",
        "Правила не проверены на проигранных закупках: в источнике данных их нет.",
    ]

    area = dict(rules.area)
    area_known = data.area_level in area
    if area_known:
        points, text = area[data.area_level], _AREA_TEXT[data.area_level]
        # Код может не совпасть, хотя по смыслу закупка профильная: смягчаем по словам названия.
        if points < 0 and data.semantic_level in ("high", "medium"):
            factors.append(Factor("область", rules.semantic_high if data.semantic_level == "high" else rules.semantic_medium,
                                  text + " Но по смыслу закупка близка к работам компании "
                                  "(оценка модели)."))
        elif points < 0 and data.text_match is not None and data.text_match >= rules.soften_by_text:
            factors.append(Factor("область", 0, text + " Но название похоже на контракты "
                                  "компании: штраф за область не применён."))
        else:
            factors.append(Factor("область", points, text))
    elif data.text_match is not None:
        high, mid, low = rules.unknown_points
        points = high if data.text_match >= rules.unknown_text_high else (
            mid if data.text_match >= rules.unknown_text_mid else low)
        factors.append(Factor("область", points,
                              "Кода ОКПД2 нет, область оценена по словам названия."))
        area_known = True
    else:
        limitations.append("Область закупки не определена.")

    wins = data.company_wins_at_customer
    if wins is not None and data.customer_contracts > 0:
        if wins >= rules.wins_many_from:
            factors.append(Factor("заказчик", rules.wins_many, f"Компания выигрывала у этого заказчика {wins} раз."))
        elif wins >= 1:
            factors.append(Factor("заказчик", rules.wins_some, f"Компания выигрывала у этого заказчика {wins} раз."))
        else:
            factors.append(Factor("заказчик", rules.wins_none, "У этого заказчика компания раньше не выигрывала."))
    else:
        limitations.append("История побед у заказчика неизвестна.")

    if data.average_bids is not None:
        if data.average_bids < rules.bids_one_below:
            factors.append(Factor("конкуренция", rules.bids_one, "В прошлых закупках заказчика обычно одна заявка."))
        elif data.average_bids >= rules.bids_many_from:
            factors.append(Factor("конкуренция", rules.bids_many, "В прошлых закупках заказчика четыре заявки и больше."))
        else:
            factors.append(Factor("конкуренция", 0, "В прошлых закупках заказчика две-три заявки."))
    elif data.no_reduction_share is not None and data.reduction_observations >= rules.min_observations:
        if data.no_reduction_share >= rules.no_reduction_high_share:
            factors.append(Factor("конкуренция", rules.no_reduction_high,
                                  "Большинство закупок заказчика прошло без снижения цены."))
        elif data.no_reduction_share <= rules.no_reduction_low_share:
            factors.append(Factor("конкуренция", rules.no_reduction_low,
                                  "Почти во всех закупках заказчика цену снижали."))
    else:
        limitations.append("Уровень конкуренции у заказчика неизвестен.")

    if data.blocking_gaps:
        factors.append(Factor("требования", rules.gap_each * min(data.blocking_gaps, rules.gap_max),
                              f"Не выполнено обязательных требований: {data.blocking_gaps}."))
    if data.open_questions:
        # Не штрафуем: по публичным данным большинство требований проверить нельзя.
        factors.append(Factor("требования", 0, "Важных требований, которые нужно проверить "
                                              f"внутри компании: {data.open_questions}."))
    if not data.documents_read:
        limitations.append("Документация не прочитана: требования к участнику не учтены.")

    if data.tender_price > 0 and data.typical_price_max > 0 and data.tender_price > data.typical_price_max:
        factors.append(Factor("размер", rules.larger_than_usual, "Закупка крупнее любого известного контракта компании."))

    if not area_known and wins is None:
        return Chance("unknown", None, None, "low", factors, limitations)

    score = max(rules.floor, min(rules.ceiling, rules.base + sum(f.points for f in factors)))
    evidence = sum([
        data.area_level in area,
        wins is not None and data.customer_contracts >= rules.min_customer_contracts,
        data.average_bids is not None or data.reduction_observations >= rules.min_observations,
        data.documents_read,
    ])
    # Выше средней уверенность не ставится, пока правила не проверены на прошлых закупках.
    confidence, half = ("medium", rules.half_confident) if evidence >= rules.confident_evidence else (
        "low", rules.half_unsure)
    low, high = max(rules.range_floor, score - half), min(rules.range_ceiling, score + half)
    level = "high" if score >= rules.level_high_from else ("medium" if score >= rules.level_medium_from else "low")
    if data.blocking_gaps and level == "high":
        level = "medium"
    return Chance(level, low, high, confidence, factors, limitations)

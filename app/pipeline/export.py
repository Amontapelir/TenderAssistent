"""Пакет для веб-версии: готовые файлы результата в одной папке с оглавлением.

В пакет идут только файлы, прошедшие проверку по схеме. Сырые ответы API, скачанные документы,
ответы модели и настройки в пакет не попадают: их там неоткуда взять, папка собирается заново.
"""
import shutil
import time
from pathlib import Path

from pydantic import BaseModel

from app.schemas.card import (AnalysisCard, BacktestFile, ExportCard, ExportIndex, ProfileFile,
                              TendersFile)
from app.storage.files import Store

_PARTS = (("profile", ProfileFile, "профиль компании"),
          ("tenders", TendersFile, "список закупок"),
          ("backtest", BacktestFile, "проверка на прошлых закупках"))


class SecretFound(RuntimeError):
    """В файле результата нашлось значение ключа. Пакет не собран."""


def _dump(path: Path, model: BaseModel, secrets: list[str]) -> None:
    text = model.model_dump_json(indent=2)
    for secret in secrets:
        if secret and secret in text:
            raise SecretFound(f"В файле {path.name} найдено значение ключа: пакет не собран.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build(store: Store, target: Path, company_inn: str, secrets: list[str]) -> ExportIndex:
    """Собирает пакет в папке target (она создаётся заново). secrets: значения ключей,
    которых в пакете быть не должно."""
    target = Path(target)
    work = target.with_name(target.name + ".tmp")
    if work.exists():
        shutil.rmtree(work)
    files: list[str] = []
    missing: list[str] = []
    cards: list[ExportCard] = []
    try:
        for name, schema, label in _PARTS:
            saved = store.load(name, schema)
            if saved is None:
                missing.append(f"{name}.json ({label}): ещё не построен.")
                continue
            _dump(work / f"{name}.json", saved, secrets)
            files.append(f"{name}.json")
        for path in sorted((store.out_dir / "cards").glob("*.json")):
            card = store.load_card(path.stem, AnalysisCard)
            if card is None:
                continue
            _dump(work / "cards" / path.name, card, secrets)
            files.append(f"cards/{path.name}")
            cards.append(ExportCard(
                regn=card.tender.regn, title=card.tender.title, customer=card.tender.customer_name,
                initial_price=card.tender.initial_price, chance_level=card.chance.level,
                range_low=card.chance.range_low, range_high=card.chance.range_high,
                requirements=len(card.requirements), file=f"cards/{path.name}"))
        if not cards:
            missing.append("cards/: нет ни одной карточки анализа.")
        index = ExportIndex(generated_at=time.strftime("%Y-%m-%dT%H:%M:%S"), company_inn=company_inn,
                            files=files, cards=cards, missing=missing)
        _dump(work / "index.json", index, secrets)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    if target.exists():
        shutil.rmtree(target)
    work.replace(target)
    return index

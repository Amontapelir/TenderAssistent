"""Результаты работы лежат файлами JSON: профиль, список кандидатов, карточки анализа.

Те же файлы составляют пакет для веб-версии. Запись идёт через временный файл, чтобы
прерванная запись не оставила половину документа.
"""
import re
from pathlib import Path
from typing import Optional, Type, TypeVar

from pydantic import BaseModel

M = TypeVar("M", bound=BaseModel)
_REGN = re.compile(r"^\d{8,25}$")


class Store:
    def __init__(self, data_dir: Path):
        self.out_dir = Path(data_dir) / "out"

    def _write(self, path: Path, model: BaseModel) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(model.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)
        return path

    @staticmethod
    def _read(path: Path, model: Type[M]) -> Optional[M]:
        try:
            return model.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def save(self, name: str, model: BaseModel) -> Path:
        """name: "profile" или "tenders"."""
        return self._write(self.out_dir / f"{name}.json", model)

    def load(self, name: str, model: Type[M]) -> Optional[M]:
        return self._read(self.out_dir / f"{name}.json", model)

    def card_path(self, regn: str) -> Path:
        if not _REGN.match(regn):
            raise ValueError("Номер закупки должен состоять только из цифр.")
        return self.out_dir / "cards" / f"{regn}.json"

    def save_card(self, regn: str, model: BaseModel) -> Path:
        return self._write(self.card_path(regn), model)

    def load_card(self, regn: str, model: Type[M]) -> Optional[M]:
        return self._read(self.card_path(regn), model)

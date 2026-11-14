"""Сохранённые ответы модели: повторное чтение той же части документа не тратит токены."""
import hashlib
import json
from pathlib import Path
from typing import Optional


class LlmCache:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir) / "llm"

    def _path(self, model: str, version: str, system: str, user: str) -> Path:
        digest = hashlib.sha256("\x00".join((model, version, system, user)).encode("utf-8")).hexdigest()
        return self.dir / f"{digest[:24]}.json"

    def get(self, model: str, version: str, system: str, user: str) -> Optional[dict]:
        try:
            return json.loads(self._path(model, version, system, user).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def put(self, model: str, version: str, system: str, user: str, value: dict) -> None:
        path = self._path(model, version, system, user)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

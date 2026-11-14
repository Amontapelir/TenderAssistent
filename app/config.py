"""Настройки: пути и значения из окружения или файла .env.

Ключи читаются в момент использования и нигде не печатаются.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_COMPANY_INN = "7713076301"


def data_dir() -> Path:
    return Path(os.environ.get("TENDER_DATA_DIR") or ROOT / "data")


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            values[name.strip()] = value.strip().strip("\"'")
    return values


def setting(name: str, default: str = "") -> str:
    """Значение из переменной окружения, иначе из .env в корне проекта."""
    value = os.environ.get(name)
    if value:
        return value.strip()
    return _read_env_file(ROOT / ".env").get(name, default)


def company_inn() -> str:
    return setting("COMPANY_INN", DEFAULT_COMPANY_INN)

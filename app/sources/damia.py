"""Клиент API закупок DaMIA.

- Каждый ответ сохраняется на диск. Повторный запрос с теми же параметрами берётся из
  сохранённого файла и квоту не тратит.
- Счётчик запросов по методам. Потолок ниже лимита тарифа: при достижении запрос
  не отправляется, вызов завершается ошибкой QuotaExceeded.
- Ключ читается в момент запроса, не сохраняется и вырезается из текстов ошибок.
"""
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from app import limits
from app.sources import http

BASE_URL = "https://api.damia.ru/zakupki/"
KNOWN_METHODS = ("zsearch", "zakupka", "contract", "zakupki", "contracts", "customer")


class DamiaError(RuntimeError):
    pass


class QuotaExceeded(DamiaError):
    pass


@dataclass(frozen=True)
class Reply:
    data: object
    from_cache: bool
    cache_file: str
    fetched_at: str


@dataclass(frozen=True)
class QuotaLine:
    method: str
    used: int
    cap: int
    yearly_limit: int
    left: int


def _http_get(url: str, timeout: float) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "tender-assistant/0.1"})
    # Ключ стоит в адресе: перенаправление не исполняется, чтобы он не ушёл на другой адрес.
    with http.open_no_redirect(request, timeout) as response:
        return http.read_capped(response, limits.DAMIA_MAX_RESPONSE_BYTES, "API закупок").decode("utf-8")


class DamiaClient:
    def __init__(
        self,
        data_dir: Path,
        key_provider: Callable[[], str],
        http_get: Callable[[str, float], str] = _http_get,
    ):
        self.raw_dir = Path(data_dir) / "raw"
        self.quota_file = Path(data_dir) / "quota.json"
        self._key_provider = key_provider
        self._http_get = http_get

    # --- квота ---

    @staticmethod
    def cap(method: str) -> int:
        return limits.DAMIA_CAP_BY_METHOD.get(method, limits.DAMIA_CAP)

    def _used(self) -> dict[str, int]:
        try:
            return json.loads(self.quota_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def quota(self) -> list[QuotaLine]:
        used = self._used()
        methods = list(KNOWN_METHODS) + sorted(set(used) - set(KNOWN_METHODS))
        return [
            QuotaLine(m, used.get(m, 0), self.cap(m), limits.DAMIA_YEARLY_LIMIT,
                      max(0, self.cap(m) - used.get(m, 0)))
            for m in methods
        ]

    def _spend(self, method: str) -> None:
        used = self._used()
        count, cap = used.get(method, 0), self.cap(method)
        if count >= cap:
            raise QuotaExceeded(
                f"Потолок запросов к методу {method}: израсходовано {count} из {cap} "
                f"(тариф даёт {limits.DAMIA_YEARLY_LIMIT} в год). Запрос не отправлен."
            )
        used[method] = count + 1
        self.quota_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.quota_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(used, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.quota_file)

    # --- запросы ---

    def cache_path(self, method: str, params: dict[str, str]) -> Path:
        canon = json.dumps(params, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha1(canon.encode("utf-8")).hexdigest()[:10]
        return self.raw_dir / f"{method}_{digest}.json"

    def cached(self, method: str, **params: str) -> Optional[Reply]:
        path = self.cache_path(method, {k: str(v) for k, v in params.items()})
        if not path.exists():
            return None
        saved = json.loads(path.read_text(encoding="utf-8"))
        return Reply(saved["response"], True, path.name, saved.get("fetched_at", ""))

    def call(self, method: str, **params: str) -> Reply:
        if method not in KNOWN_METHODS:
            raise DamiaError(f"Метод {method} не используется.")
        params = {k: str(v) for k, v in params.items()}
        hit = self.cached(method, **params)
        if hit:
            return hit

        key = self._key_provider()
        if not key:
            raise DamiaError("Ключ DAMIA_KEY не задан: создайте файл .env по образцу .env.example.")
        self._spend(method)  # считаем до отправки: неудачный запрос тоже мог быть засчитан
        url = f"{BASE_URL}{method}?{urllib.parse.urlencode({**params, 'key': key})}"
        try:
            body = self._http_get(url, 60)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300].replace(key, "***")
            raise DamiaError(f"HTTP {e.code} от метода {method}: {detail}") from None
        except urllib.error.URLError as e:
            raise DamiaError(f"Сеть недоступна при запросе {method}: {str(e.reason).replace(key, '***')}") from None
        except http.TooLarge as e:
            raise DamiaError(f"Метод {method}: {e}") from None
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            raise DamiaError(f"Ответ метода {method} не JSON: {body[:300].replace(key, '***')}") from None

        path = self.cache_path(method, params)
        fetched_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"method": method, "params": params, "fetched_at": fetched_at,
                        "response": data}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
        return Reply(data, False, path.name, fetched_at)

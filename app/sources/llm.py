"""Клиент языковой модели с интерфейсом как у OpenAI (Groq, Mistral и другие).

Один метод: отправить сообщения и получить ответ в JSON. При отказе по частоте запросов
и при сбое сервера запрос повторяется ограниченное число раз. Ключ не попадает в ошибки.
"""
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

from app import limits
from app.sources import http


class LlmError(RuntimeError):
    pass


class LlmNotConfigured(LlmError):
    pass


class LlmRateLimited(LlmError):
    pass


@dataclass(frozen=True)
class LlmReply:
    data: dict
    model: str
    prompt_tokens: int
    completion_tokens: int
    attempts: int
    remaining_tokens: Optional[int] = None  # сколько токенов осталось в текущей минуте
    reset_seconds: Optional[float] = None  # через сколько секунд остаток восстановится


def _post(url: str, headers: dict, body: bytes, timeout: float) -> tuple[int, dict, str]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    # Ключ стоит в заголовке: перенаправление не исполняется, чтобы он не ушёл на другой адрес.
    try:
        with http.open_no_redirect(request, timeout) as response:
            body = http.read_capped(response, limits.LLM_MAX_RESPONSE_BYTES, "Модель")
            return response.status, dict(response.headers), body.decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read(limits.LLM_MAX_RESPONSE_BYTES).decode("utf-8", "replace")


def _wait_seconds(headers: dict) -> float:
    value = _header(headers, "retry-after")
    try:
        return max(0.0, min(float(value), limits.LLM_RETRY_WAIT_MAX))
    except ValueError:
        return 5.0


def _header(headers: dict, name: str) -> str:
    return next((str(v) for k, v in headers.items() if k.lower() == name), "")


def _duration(value: str) -> Optional[float]:
    """Длительность из заголовка: "28.08s", "1m26.4s", "450ms"."""
    parts = re.findall(r"([\d.]+)(ms|s|m|h)", value)
    if not parts:
        return None
    scale = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}
    return sum(float(number) * scale[unit] for number, unit in parts)


class LlmClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        post: Callable[[str, dict, bytes, float], tuple[int, dict, str]] = _post,
        sleep: Callable[[float], None] = time.sleep,
        extra: Optional[dict] = None,
    ):
        if not (base_url and api_key and model):
            raise LlmNotConfigured(
                "Модель не настроена: задайте LLM_BASE_URL, LLM_API_KEY и LLM_MODEL в файле .env."
            )
        if not base_url.startswith("https://"):
            raise LlmNotConfigured("LLM_BASE_URL должен начинаться с https://.")
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self._key = api_key
        self._post = post
        self._sleep = sleep
        self._extra = dict(extra or {})  # дополнительные поля запроса, например reasoning_effort

    def _clean(self, text: str) -> str:
        return text.replace(self._key, "***")

    def ask_json(self, system: str, user: str, max_tokens: Optional[int] = None) -> LlmReply:
        """Ответ модели, разобранный как объект JSON."""
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
            "response_format": {"type": "json_object"},
            **self._extra,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
                   "User-Agent": "tender-assistant/0.1"}

        attempts = limits.LLM_RETRIES + 1
        for attempt in range(1, attempts + 1):
            try:
                status, reply_headers, text = self._post(self.url, headers, body, limits.LLM_TIMEOUT)
            except (urllib.error.URLError, TimeoutError) as e:
                reason = getattr(e, "reason", e)
                raise LlmError(f"Модель недоступна: {self._clean(str(reason))}") from None
            except http.TooLarge as e:
                raise LlmError(str(e)) from None
            if status == 200:
                return self._parse(text, attempt, reply_headers)
            # 400 json_validate_failed: модель не смогла собрать JSON, вторая попытка обычно удаётся
            retriable = status == 429 or status >= 500 or (status == 400 and "json_validate_failed" in text)
            if retriable and attempt < attempts:
                self._sleep(_wait_seconds(reply_headers))
                continue
            detail = self._clean(text[:300])
            if status == 429:
                raise LlmRateLimited(
                    f"Модель отказала по частоте запросов после {attempt} попыток: {detail}"
                )
            if status in (401, 403):
                raise LlmError(f"Модель отклонила ключ или доступ (код {status}): {detail}")
            raise LlmError(f"Модель ответила кодом {status}: {detail}")
        raise LlmError("Модель не ответила.")  # недостижимо: цикл всегда возвращает или падает

    def _parse(self, text: str, attempt: int, headers: dict) -> LlmReply:
        try:
            outer = json.loads(text)
            content = outer["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            raise LlmError(f"Ответ модели не разобран: {self._clean(text[:300])}") from None
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            raise LlmError(f"Модель вернула не JSON: {str(content)[:300]}") from None
        if not isinstance(data, dict):
            raise LlmError("Модель вернула JSON, но не объект.")
        usage = outer.get("usage") or {}
        remaining = _header(headers, "x-ratelimit-remaining-tokens")
        return LlmReply(data, str(outer.get("model") or self.model),
                        int(usage.get("prompt_tokens") or 0),
                        int(usage.get("completion_tokens") or 0), attempt,
                        int(remaining) if remaining.isdigit() else None,
                        _duration(_header(headers, "x-ratelimit-reset-tokens")))

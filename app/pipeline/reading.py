"""Чтение документации моделью по частям.

Каждая часть отправляется отдельным запросом. Ответы сохраняются. Между запросами
выдерживается пауза, если у ключа кончается минутный запас токенов. Часть, которую
прочитать не удалось, не теряется молча: она попадает в список непрочитанных.
"""
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from app import limits
from app.domain import documents as documents_domain
from app.domain import findings
from app.pipeline import prompts
from app.sources.llm import LlmClient, LlmError, LlmRateLimited, LlmReply
from app.storage.llm_cache import LlmCache


@dataclass(frozen=True)
class DocText:
    doc_id: str  # id источника в карточке, например "doc:ab12"
    title: str
    text: str


@dataclass
class Usage:
    requests: int = 0  # запросов к модели (без взятых из сохранённых)
    cached: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    waited_seconds: float = 0.0


@dataclass
class ReadResult:
    kept: list[findings.RawFinding] = field(default_factory=list)
    rejected: list[findings.Rejected] = field(default_factory=list)
    parts_total: int = 0
    parts_read: int = 0
    unread: list[str] = field(default_factory=list)  # «документ, часть N: причина»


class Reader:
    def __init__(self, llm: LlmClient, cache: LlmCache, usage: Usage,
                 sleep: Callable[[float], None] = time.sleep, chunk_chars: Optional[int] = None):
        self.llm, self.cache, self.usage, self._sleep = llm, cache, usage, sleep
        self.chunk_chars = chunk_chars or limits.LLM_CHUNK_CHARS
        self._pause = 0.0  # сколько ждать перед следующим запросом

    def ask(self, version: str, system: str, user: str) -> dict:
        """Ответ модели: сохранённый или новый. Перед новым запросом ждёт, если нужно."""
        saved = self.cache.get(self.llm.model, version, system, user)
        if saved is not None:
            self.usage.cached += 1
            return saved["data"]
        if self._pause > 0:
            self._sleep(self._pause)
            self.usage.waited_seconds += self._pause
            self._pause = 0.0
        reply: LlmReply = self.llm.ask_json(system, user)
        self.usage.requests += 1
        self.usage.prompt_tokens += reply.prompt_tokens
        self.usage.completion_tokens += reply.completion_tokens
        # Следующий запрос такого же размера: хватит ли остатка минутного запаса?
        need = reply.prompt_tokens + reply.completion_tokens
        if reply.remaining_tokens is not None and reply.remaining_tokens < need:
            self._pause = min(reply.reset_seconds or 60.0, limits.LLM_RETRY_WAIT_MAX)
        self.cache.put(self.llm.model, version, system, user,
                       {"data": reply.data, "model": reply.model,
                        "prompt_tokens": reply.prompt_tokens,
                        "completion_tokens": reply.completion_tokens})
        return reply.data

    def read(self, docs: Sequence[DocText],
             progress: Callable[[int, int], None] = lambda done, total: None) -> ReadResult:
        result = ReadResult()
        plan = [(doc, i, parts) for doc in docs
                for parts in [documents_domain.split_text(doc.text, self.chunk_chars)]
                for i in range(len(parts))]
        result.parts_total = len(plan)
        raw: list[findings.RawFinding] = []
        stopped = ""
        for done, (doc, i, parts) in enumerate(plan):
            label = f"{doc.title}, часть {i + 1} из {len(parts)}"
            if stopped:
                result.unread.append(f"{label}: {stopped}")
                continue
            try:
                data = self.ask(prompts.EXTRACT_VERSION, prompts.EXTRACT_SYSTEM,
                                prompts.extract_user(doc.title, i + 1, len(parts), parts[i]))
            except LlmRateLimited as e:
                stopped = "чтение остановлено: модель отказала по частоте запросов"
                result.unread.append(f"{label}: {e}")
                continue
            except LlmError as e:
                result.unread.append(f"{label}: {e}")
                continue
            found, rejected = findings.parse_extraction(data, doc.doc_id, limits.FINDINGS_PER_CHUNK)
            raw += found
            result.rejected += rejected
            result.parts_read += 1
            progress(done + 1, len(plan))
        kept, rejected = findings.verify(raw, {d.doc_id: d.text for d in docs})
        result.kept = kept
        result.rejected += rejected
        return result

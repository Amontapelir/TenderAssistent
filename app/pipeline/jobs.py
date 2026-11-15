"""Фоновые задачи в памяти процесса. Одновременно идёт одна: у модели общий минутный запас.

Сведения о задачах не переживают перезапуск сервера; готовые карточки лежат на диске.
"""
import threading
import time
import uuid
from dataclasses import dataclass, replace
from typing import Callable, Optional


class Busy(RuntimeError):
    pass


@dataclass(frozen=True)
class Job:
    id: str
    regn: str
    status: str  # running, done, failed
    step: str
    done: int
    total: int
    started_at: str
    finished_at: str = ""
    error: str = ""


class Jobs:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self, limit: int = 10) -> list[Job]:
        """Последние задачи, новые вперёд. Страница после перезагрузки находит по ним идущую."""
        with self._lock:
            return list(self._jobs.values())[::-1][:max(0, limit)]

    def _update(self, job_id: str, **changes) -> None:
        with self._lock:
            self._jobs[job_id] = replace(self._jobs[job_id], **changes)

    def start(self, regn: str, work: Callable[[Callable[[str, int, int], None]], object],
              run_in_thread: bool = True) -> Job:
        """work получает функцию для отчёта о ходе: (шаг, сделано, всего)."""
        with self._lock:
            running = next((j for j in self._jobs.values() if j.status == "running"), None)
            if running:
                raise Busy(f"Уже идёт анализ закупки {running.regn} (задача {running.id}). "
                           "Дождитесь его окончания.")
            job = Job(uuid.uuid4().hex[:12], regn, "running", "Начало", 0, 1,
                      time.strftime("%Y-%m-%dT%H:%M:%S"))
            self._jobs[job.id] = job

        def run() -> None:
            try:
                work(lambda step, done, total: self._update(job.id, step=step, done=done, total=total))
                self._update(job.id, status="done", finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
            except Exception as e:  # ошибка не теряется: её текст виден в состоянии задачи
                self._update(job.id, status="failed", error=f"{type(e).__name__}: {e}",
                             finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"))

        if run_in_thread:
            threading.Thread(target=run, name=f"analyze-{regn}", daemon=True).start()
        else:
            run()
        return self.get(job.id)  # type: ignore[return-value]

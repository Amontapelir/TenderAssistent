"""Скачивание файлов закупок.

Файл берётся только по https и только с zakupki.gov.ru. Перенаправления не выполняются
автоматически: каждый новый адрес проверяется так же, как первый. Размер ограничен.
Скачанный файл и его текст сохраняются; повторно файл не скачивается.
"""
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol

from app import limits
from app.sources import textract

ALLOWED_HOST = "zakupki.gov.ru"
_REGN = re.compile(r"^\d{8,25}$")


class DownloadRefused(RuntimeError):
    """Файл не скачан. Текст исключения показывается пользователю."""


class Response(Protocol):
    status: int
    headers: dict

    def read(self, n: int) -> bytes: ...


@dataclass(frozen=True)
class StoredDoc:
    id: str
    url: str
    title: str
    role: str
    file_name: str  # имя, которое назвал сервер
    kind: str  # формат по содержимому
    size: int
    sha256: str
    fetched_at: str
    readable: bool
    chars: int
    problem: str  # почему текст не получен; пусто, если получен


def check_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https":
        raise DownloadRefused("Адрес отклонён: разрешён только протокол https.")
    if host != ALLOWED_HOST and not host.endswith("." + ALLOWED_HOST):
        raise DownloadRefused(f"Адрес отклонён: файлы берутся только с {ALLOWED_HOST}.")
    if parts.username or parts.password or parts.port not in (None, 443):
        raise DownloadRefused("Адрес отклонён: в нём указаны учётные данные или нестандартный порт.")
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _open(url: str, timeout: float) -> Response:
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (tender-assistant)"})
    try:
        raw = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as e:
        raw = e  # ответ с кодом 3xx/4xx/5xx: разбираем сами
    raw.headers = {k.lower(): v for k, v in raw.headers.items()}  # type: ignore[assignment]
    return raw  # type: ignore[return-value]


def _file_name(headers: dict) -> str:
    value = headers.get("content-disposition", "")
    match = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", value, re.I)
    if match:
        return urllib.parse.unquote(match.group(1).strip())
    match = re.search(r'filename\s*=\s*"?([^";]+)"?', value, re.I)
    if not match:
        return ""
    name = match.group(1).strip()
    try:  # сервер нередко отдаёт имя в UTF-8, а заголовок читается как latin-1
        return name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


class Downloader:
    def __init__(self, data_dir: Path, open_url: Callable[[str, float], Response] = _open):
        self.docs_dir = Path(data_dir) / "docs"
        self._open = open_url

    def _dir(self, regn: str) -> Path:
        if not _REGN.match(regn):
            raise ValueError("Номер закупки должен состоять только из цифр.")
        return self.docs_dir / regn

    @staticmethod
    def doc_id(url: str) -> str:
        return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]

    def stored(self, regn: str, url: str) -> Optional[StoredDoc]:
        try:
            meta = json.loads((self._dir(regn) / f"{self.doc_id(url)}.json").read_text("utf-8"))
        except FileNotFoundError:
            return None
        return StoredDoc(**meta)

    def reread(self, regn: str, doc: StoredDoc) -> StoredDoc:
        """Заново извлекает текст из уже скачанного файла (сеть не нужна)."""
        data = (self._dir(regn) / f"{doc.id}.bin").read_bytes()
        return self.store(regn, doc.url, doc.title, doc.role, data, doc.file_name)

    def text(self, regn: str, doc: StoredDoc) -> str:
        return (self._dir(regn) / f"{doc.id}.txt").read_text(encoding="utf-8")

    def _download(self, url: str) -> tuple[bytes, str]:
        for _ in range(limits.DOC_MAX_REDIRECTS + 1):
            check_url(url)
            response = self._open(url, limits.DOC_TIMEOUT)
            if response.status in (301, 302, 303, 307, 308):
                target = response.headers.get("location", "")
                if not target:
                    raise DownloadRefused("Сервер перенаправил запрос, но не назвал адрес.")
                url = urllib.parse.urljoin(url, target)
                continue
            if response.status != 200:
                raise DownloadRefused(f"Сервер ответил кодом {response.status}. Файл не получен.")
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > limits.DOC_MAX_BYTES:
                raise DownloadRefused(
                    f"Файл занимает {int(declared) // 2**20} МБ, предел "
                    f"{limits.DOC_MAX_BYTES // 2**20} МБ. Файл не скачан."
                )
            chunks, total = [], 0
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limits.DOC_MAX_BYTES:
                    raise DownloadRefused(
                        f"Файл больше {limits.DOC_MAX_BYTES // 2**20} МБ. Скачивание остановлено."
                    )
                chunks.append(chunk)
            return b"".join(chunks), _file_name(response.headers)
        raise DownloadRefused(
            f"Больше {limits.DOC_MAX_REDIRECTS} перенаправлений подряд. Файл не скачан."
        )

    def fetch(self, regn: str, url: str, title: str, role: str) -> StoredDoc:
        """Скачивает файл, сохраняет его и извлечённый текст. Уже скачанный возвращает сразу."""
        known = self.stored(regn, url)
        if known:
            return known
        try:
            data, file_name = self._download(url)
        except urllib.error.URLError as e:
            raise DownloadRefused(f"Сеть недоступна: {e.reason}") from None
        return self.store(regn, url, title, role, data, file_name)

    def store(self, regn: str, url: str, title: str, role: str, data: bytes,
              file_name: str = "") -> StoredDoc:
        folder = self._dir(regn)
        folder.mkdir(parents=True, exist_ok=True)
        doc_id = self.doc_id(url)
        kind = textract.detect(data)
        text, problem = "", ""
        try:
            text = textract.extract(data, kind)
            if not text.strip():
                problem = "В файле нет текста (возможно, это скан)."
        except textract.Unreadable as e:
            problem = str(e)
        (folder / f"{doc_id}.bin").write_bytes(data)
        (folder / f"{doc_id}.txt").write_text(text, encoding="utf-8")
        doc = StoredDoc(
            id=doc_id, url=url, title=title, role=role, file_name=file_name, kind=kind,
            size=len(data), sha256=hashlib.sha256(data).hexdigest(),
            fetched_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            readable=not problem, chars=len(text), problem=problem,
        )
        tmp = folder / f"{doc_id}.tmp"
        tmp.write_text(json.dumps(asdict(doc), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(folder / f"{doc_id}.json")
        return doc

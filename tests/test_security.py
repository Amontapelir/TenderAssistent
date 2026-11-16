import asyncio
import io
import json
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from app import limits, main
from app.api import routes
from app.api.access import is_local_host_header
from app.sources import damia, http, llm, textract
from app.sources.damia import DamiaClient, DamiaError
from app.sources.llm import LlmClient, LlmError
from tests.test_documents import DOCX

KEY = "секретный-ключ-123"


def request(path, host="localhost:8765", method="GET", headers=()):
    """Запрос к приложению напрямую, без сети: возвращает (код, заголовки, тело)."""
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": ([(b"host", host.encode())] if host is not None else []) + list(headers),
             "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8765)}
    sent = []
    first = []

    async def receive():
        # Первый вызов отдаёт запрос. Дальше клиент «молчит»: отдача файла ждёт отключения клиента,
        # и мгновенные бесконечные ответы заглушки зациклили бы её.
        if not first:
            first.append(1)
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        sent.append(message)

    asyncio.run(main.app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], {k.decode(): v.decode() for k, v in start["headers"]}, body


class Guard(unittest.TestCase):
    def test_foreign_host_is_refused_for_reading_too(self):
        # подмена адреса через DNS: чужой сайт обращается к 127.0.0.1 под своим именем
        for path in ("/api/health", "/api/quota", "/api/jobs", "/api/index", "/", "/app.js", "/docs"):
            with self.subTest(path=path):
                status, headers, body = request(path, host="evil.example")
                self.assertEqual(status, 400)
                self.assertIn("этому компьютеру", json.loads(body)["detail"])
        self.assertEqual(request("/api/health", host=None)[0], 400)  # без заголовка Host
        self.assertEqual(request("/api/health", host="localhost.evil.example:8765")[0], 400)
        self.assertEqual(request("/api/health", host="evil.example:8765")[0], 400)

    def test_local_hosts_are_served(self):
        for host in ("localhost:8765", "127.0.0.1:8765", "localhost", "[::1]:8765"):
            with self.subTest(host=host):
                status, _, body = request("/api/health", host=host)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["status"], "ok")
        self.assertEqual(request("/")[0], 200)

    def test_security_headers_everywhere(self):
        for path, host in (("/", "localhost:8765"), ("/api/health", "localhost:8765"),
                           ("/app.js", "localhost:8765"), ("/нет-такого", "localhost:8765"),
                           ("/api/health", "evil.example")):
            with self.subTest(path=path, host=host):
                _, headers, _ = request(path, host=host)
                self.assertEqual(headers["x-frame-options"], "DENY")
                self.assertEqual(headers["content-security-policy"], "frame-ancestors 'none'")
                self.assertEqual(headers["x-content-type-options"], "nosniff")
                self.assertEqual(headers["referrer-policy"], "no-referrer")
                self.assertNotIn("access-control-allow-origin", headers)  # чужим сайтам чтение не разрешено

    def test_host_header_check(self):
        self.assertTrue(is_local_host_header("localhost:8765"))
        self.assertTrue(is_local_host_header("[::1]:8765"))
        self.assertFalse(is_local_host_header(""))
        self.assertFalse(is_local_host_header(None))
        self.assertFalse(is_local_host_header("127.0.0.1.evil.example"))

    def test_files_outside_the_page_folder_are_not_served(self):
        for path in ("/../.env", "/..%2f.env", "/data/../../.env", "/.git/config", "/../README.md",
                     "/../app/config.py"):
            with self.subTest(path=path):
                status, _, body = request(path)
                self.assertEqual(status, 404)
                self.assertNotIn(b"DAMIA_KEY", body)

    def test_every_changing_route_is_local_only(self):
        changing = [r for r in routes.router.routes if getattr(r, "methods", None) and r.methods - {"GET", "HEAD"}]
        # Точный список: новый изменяющий запрос придётся добавить сюда осознанно, вместе с защитой.
        self.assertEqual(sorted(r.path for r in changing), sorted([
            "/api/profile/build", "/api/tenders/search", "/api/tenders/{regn}/documents/fetch", "/api/tenders/{regn}/analyze",
            "/api/model/check", "/api/backtest/run", "/api/backtest/compare", "/api/export"]))
        for route in changing:
            with self.subTest(path=route.path):
                self.assertIn(routes.local_only, [d.call for d in route.dependant.dependencies])


class RedirectingServer:
    """Настоящий сервер на этом компьютере: /start перенаправляет на /other, остальное отвечает «ok»."""

    def __init__(self, test: unittest.TestCase):
        seen = self.seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.respond()

            def respond(self):
                seen.append((self.path, self.headers.get("Authorization")))
                if self.path.split("?")[0] == "/start":
                    self.send_response(302)
                    self.send_header("Location", "/other")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    body = b"ok"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        test.addCleanup(self.server.server_close)
        test.addCleanup(self.server.shutdown)


class RealRedirects(unittest.TestCase):
    def test_secret_is_not_resent_after_redirect(self):
        srv = RedirectingServer(self)
        req = urllib.request.Request(srv.url + "/start", headers={"Authorization": "Bearer s3cr3t"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            http.open_no_redirect(req, 5)
        self.assertEqual(caught.exception.code, 302)
        self.assertEqual([path for path, _ in srv.seen], ["/start"])  # на /other запрос не ушёл

    def test_both_clients_stop_at_redirect(self):
        srv = RedirectingServer(self)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            damia._http_get(srv.url + "/start?key=" + "k" * 20, 5)
        self.assertEqual(caught.exception.code, 302)
        status, _, _ = llm._post(srv.url + "/start", {"Authorization": "Bearer s3cr3t"}, b"{}", 5)
        self.assertEqual(status, 302)
        self.assertEqual([path.split("?")[0] for path, _ in srv.seen], ["/start", "/start"])  # дальше не ходили

    def test_plain_answer_still_works(self):
        srv = RedirectingServer(self)
        self.assertEqual(damia._http_get(srv.url + "/other", 5), "ok")
        self.assertEqual(llm._post(srv.url + "/other", {}, b"{}", 5)[0:3:2], (200, "ok"))

    def test_answers_over_the_cap_are_refused(self):
        srv = RedirectingServer(self)
        with mock.patch.object(limits, "DAMIA_MAX_RESPONSE_BYTES", 1):
            with self.assertRaises(http.TooLarge):
                damia._http_get(srv.url + "/other", 5)
        with mock.patch.object(limits, "LLM_MAX_RESPONSE_BYTES", 1):
            with self.assertRaises(http.TooLarge):
                llm._post(srv.url + "/other", {}, b"{}", 5)


class FakeResponse(io.BytesIO):
    status = 200
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class Outgoing(unittest.TestCase):
    def test_redirects_are_not_followed(self):
        handler = http._NoRedirect()
        req = urllib.request.Request("https://api.example/x?key=" + "k" * 20, headers={"Authorization": "Bearer t"})
        self.assertIsNone(handler.redirect_request(req, None, 302, "Found", {}, "http://evil.example/x"))
        self.assertTrue(any(isinstance(h, http._NoRedirect) for h in http._opener.handlers))
        # стандартный обработчик, который повторил бы запрос по новому адресу, заменён
        plain = [h for h in http._opener.handlers if type(h) is urllib.request.HTTPRedirectHandler]
        self.assertEqual(plain, [])

    def test_read_is_capped(self):
        self.assertEqual(http.read_capped(io.BytesIO(b"x" * 10), 10, "Источник"), b"x" * 10)
        with self.assertRaisesRegex(http.TooLarge, "Источник"):
            http.read_capped(io.BytesIO(b"x" * 11), 10, "Источник")

    def test_clients_use_the_safe_opener(self):
        calls = []

        def fake_open(req, timeout):
            calls.append(req.full_url)
            return FakeResponse(b'{"ok": true}')

        original = http.open_no_redirect
        http.open_no_redirect = fake_open
        try:
            self.assertEqual(damia._http_get("https://api.example/a", 5), '{"ok": true}')
            status, _, text = llm._post("https://api.example/b", {}, b"{}", 5)
            self.assertEqual((status, text), (200, '{"ok": true}'))
        finally:
            http.open_no_redirect = original
        self.assertEqual(calls, ["https://api.example/a", "https://api.example/b"])

    def test_oversized_answers_become_clear_errors(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))

        def too_big(url, timeout):
            raise http.TooLarge("API закупок: ответ больше 30 МБ, чтение остановлено.")

        with self.assertRaisesRegex(DamiaError, "больше 30 МБ"):
            DamiaClient(folder, lambda: KEY, too_big).call("contracts", inn="1")

        def big_post(url, headers, body, timeout):
            raise http.TooLarge("Модель: ответ больше 5 МБ, чтение остановлено.")

        with self.assertRaisesRegex(LlmError, "больше 5 МБ"):
            LlmClient("https://llm.example/v1", KEY, "m", post=big_post).ask_json("s", "u")

    def test_redirect_answer_is_an_error_without_the_key(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))

        def redirected(url, timeout):
            raise urllib.error.HTTPError(url, 302, "Found", {}, io.BytesIO(f"go to http://evil/?key={KEY}".encode()))

        with self.assertRaises(DamiaError) as caught:
            DamiaClient(folder, lambda: KEY, redirected).call("contracts", inn="1")
        self.assertIn("302", str(caught.exception))
        self.assertNotIn(KEY, str(caught.exception))


class ArchiveBomb(unittest.TestCase):
    @staticmethod
    def pack(count):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as z:
            for i in range(count):
                z.writestr(f"{i}.docx", DOCX)
        return buffer.getvalue()

    def test_total_size_is_capped(self):
        data = self.pack(4)
        one = textract.extract(self.pack(1), "zip")
        # предел на два файла: третий и четвёртый не читаются
        with mock.patch.object(limits, "ARCHIVE_MAX_TOTAL_BYTES", len(DOCX) * 2 + 10):
            text = textract.extract(data, "zip")
        self.assertEqual(text, one + "\n\n" + one)
        # предел меньше одного файла: читать нечего, причина названа
        with mock.patch.object(limits, "ARCHIVE_MAX_TOTAL_BYTES", 10):
            with self.assertRaisesRegex(textract.Unreadable, "нет файлов Word"):
                textract.extract(data, "zip")
        self.assertEqual(textract.extract(data, "zip").count(one), 4)  # обычный предел не мешает


class PageFiles(unittest.TestCase):
    """Страница не вставляет данные как разметку и не содержит встроенных стилей и скриптов."""

    def setUp(self):
        self.docs = main.UI_DIR

    def test_no_markup_injection_points(self):
        for name in ("app.js", "local.js"):
            text = (self.docs / name).read_text(encoding="utf-8")
            for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
                with self.subTest(file=name, bad=bad):
                    self.assertNotIn(bad, text)

    def test_csp_and_no_inline_code(self):
        html = (self.docs / "index.html").read_text(encoding="utf-8")
        csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', html).group(1)
        self.assertIn("default-src 'none'", csp)
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertNotIn("unsafe-eval", csp)
        self.assertIsNone(re.search(r"\sstyle=", html))
        self.assertIsNone(re.search(r"<script(?![^>]*\ssrc=)", html))  # скриптов без src нет
        self.assertIsNone(re.search(r"\son[a-z]+=", html))

    def test_page_stays_readable_without_animation(self):
        # В окружении без кадров анимации (фоновая вкладка, снимок, превью ссылки) страница не должна
        # остаться с нулевыми числами и невидимыми блоками.
        text = (self.docs / "app.js").read_text(encoding="utf-8")
        self.assertIn("if (started === null) started = now;", text)  # ноль ставится только по первому кадру
        self.assertNotIn("node.textContent = format(0);", text)
        self.assertIn("if (!observed) nodes.forEach", text)  # блоки показываются, если наблюдатель молчит

    def test_links_from_data_must_be_https(self):
        text = (self.docs / "app.js").read_text(encoding="utf-8")
        self.assertIn('!href.startsWith("https://")', text)
        self.assertIn('a.rel = "noopener noreferrer"', text)

    def test_published_data_has_no_secrets_or_local_paths(self):
        for path in (self.docs / "data").rglob("*.json"):
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                self.assertIsNone(re.search(r"gsk_[A-Za-z0-9]{20,}|[?&]key=[A-Za-z0-9]{12,}|Bearer [A-Za-z0-9_\-]{20,}", text))
                self.assertNotIn("C:\\\\", text)
                self.assertNotIn("/Users/", text)
                hosts = set(re.findall(r"https?://([a-zA-Z0-9.\-]+)", text))
                self.assertLessEqual(hosts, {"zakupki.gov.ru"})


if __name__ == "__main__":
    unittest.main()

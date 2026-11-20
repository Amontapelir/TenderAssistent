import json
import tempfile
import unittest
from pathlib import Path

from fastapi.staticfiles import StaticFiles
from starlette.routing import Mount

from app import main
from app.api import routes
from app.api.access import is_local_request
from app.pipeline import analyze, export, steps
from app.pipeline.jobs import Jobs
from app.schemas.card import AnalysisCard, ExportIndex
from app.sources.damia import DamiaClient
from app.sources.documents import Downloader
from app.storage.files import Store
from app.storage.llm_cache import LlmCache
from tests.test_analysis import COMPANY, HISTORY, US
from tests.test_documents import REGN, TENDER

KEY = "секретный-ключ-123"


def store_with_card(folder: Path) -> Store:
    def http(url, timeout):
        method = url.split("/zakupki/")[1].split("?")[0]
        if method == "contracts":
            return json.dumps(COMPANY if f"inn={US}" in url else HISTORY)
        return json.dumps(TENDER)

    client = DamiaClient(folder, lambda: KEY, http)
    store = Store(folder)
    steps.build_profile(client, store, US, network=True)
    analyze.analyze_tender(client, Downloader(folder), store, None, LlmCache(folder), REGN,
                           network=True, lookups=0)
    return store


class LiveIndex(unittest.TestCase):
    """Оглавление для локальной страницы совпадает с тем, что попало бы в пакет."""

    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = store_with_card(self.dir)

    def test_same_as_package(self):
        (self.dir / "out" / "cards" / "заметки.txt").write_text("не карточка", encoding="utf-8")
        live = export.describe(self.store, US)
        built = export.build(self.store, self.dir / "export", US, [])
        self.assertEqual((live.files, live.missing, live.cards), (built.files, built.missing, built.cards))
        self.assertEqual([c.regn for c in live.cards], [REGN])
        self.assertEqual(live.company_inn, US)
        card = self.store.load_card(REGN, AnalysisCard)
        row = live.cards[0]
        self.assertEqual((row.title, row.customer, row.initial_price, row.chance_level, row.requirements, row.file),
                         (card.tender.title, card.tender.customer_name, card.tender.initial_price,
                          card.chance.level, len(card.requirements), f"cards/{REGN}.json"))

    def test_nothing_is_written(self):
        before = sorted(p.relative_to(self.dir).as_posix() for p in self.dir.rglob("*"))
        export.describe(self.store, US)
        self.assertEqual(sorted(p.relative_to(self.dir).as_posix() for p in self.dir.rglob("*")), before)
        self.assertFalse((self.dir / "export").exists())

    def test_empty_store(self):
        empty = Store(Path(self.enterContext(tempfile.TemporaryDirectory())))
        live = export.describe(empty, US)
        self.assertEqual((live.files, live.cards), ([], []))
        self.assertEqual(len(live.missing), 5)  # четыре файла и карточки

    def test_route(self):
        live = routes.live_index(self.store)
        self.assertIsInstance(live, ExportIndex)
        self.assertEqual([c.regn for c in live.cards], [REGN])


class RecentJobs(unittest.TestCase):
    def test_newest_first(self):
        jobs = Jobs()

        def failing(progress):
            raise RuntimeError("не вышло")

        for regn, work in (("1", lambda progress: None), ("2", failing), ("3", lambda progress: None)):
            jobs.start(regn, work, run_in_thread=False)
        recent = jobs.recent()
        self.assertEqual([j.regn for j in recent], ["3", "2", "1"])
        self.assertEqual([j.status for j in recent], ["done", "failed", "done"])
        self.assertIn("не вышло", recent[1].error)
        self.assertEqual([j.regn for j in jobs.recent(2)], ["3", "2"])
        self.assertEqual(jobs.recent(0), [])

    def test_finds_running(self):
        jobs = Jobs()
        seen = []

        def work(progress):
            seen.extend(j.regn for j in jobs.recent() if j.status == "running")

        jobs.start("7", work, run_in_thread=False)
        self.assertEqual(seen, ["7"])  # во время работы задача видна как идущая


class Page(unittest.TestCase):
    def test_server_gives_the_page_last(self):
        mounts = [r for r in main.app.routes if isinstance(r, Mount)]
        self.assertEqual([m.name for m in mounts], ["ui"])
        self.assertIsInstance(mounts[0].app, StaticFiles)
        # страница подключена последней: запросы и документация API совпадают раньше
        self.assertIs(main.app.routes[-1], mounts[0])
        self.assertTrue(any(getattr(r, "path", "") == "/docs" for r in main.app.routes[:-1]))

    def test_api_is_still_served(self):
        paths = main.app.openapi()["paths"]
        for path in ("/api/health", "/api/index", "/api/jobs", "/api/quota", "/api/export"):
            self.assertIn(path, paths)

    def test_page_files_exist(self):
        self.assertEqual(main.UI_DIR.name, "docs")
        html = (main.UI_DIR / "index.html").read_text(encoding="utf-8")
        for name in ("app.js", "local.js", "app.css"):
            self.assertIn(name, html)
            self.assertTrue((main.UI_DIR / name).is_file())

    def test_page_posts_are_accepted_from_this_computer(self):
        # запрос со страницы с того же адреса: Host и Origin называют локальный сервер
        self.assertTrue(is_local_request("127.0.0.1", "localhost:8765", "http://localhost:8765"))
        self.assertTrue(is_local_request("127.0.0.1", "127.0.0.1:8765", "http://127.0.0.1:8765"))
        self.assertFalse(is_local_request("127.0.0.1", "localhost:8765", "https://amontapelir.github.io"))


if __name__ == "__main__":
    unittest.main()

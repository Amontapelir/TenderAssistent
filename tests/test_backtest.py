import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from app import limits
from app.domain import backtest
from app.domain.models import Contract
from app.pipeline import analyze, backtest as backtest_step, export, steps
from app.schemas.card import AnalysisCard, BacktestFile, ExportIndex, ProfileFile
from app.sources import damia_parse, textract
from app.sources.damia import DamiaClient
from app.sources.documents import Downloader
from app.storage.files import Store
from app.storage.llm_cache import LlmCache
from tests.test_analysis import COMPANY, CUSTOMER, HISTORY, US, contracts_of
from tests.test_documents import BASE, DOCX, REGN, TENDER

KEY = "секретный-ключ-123"


def contract(regn, day, customer, code="61.10.11.000", title="Услуги связи", tender="T", price=100.0):
    return Contract(regn=regn, published=day, customer_inn=customer, supplier_inns=(), price=price,
                    okpd=code, title=title, tender_regn=tender and tender + regn, tender_price=price)


class Separation(unittest.TestCase):
    def test_pairs(self):
        self.assertEqual(backtest.separation([3, 3], [1, 2]), 1.0)
        self.assertEqual(backtest.separation([1], [2, 3]), 0.0)
        self.assertEqual(backtest.separation([2], [2, 2]), 0.5)  # равные оценки: половина
        self.assertEqual(backtest.separation([2, 5], [1, 2, 9]), round((0.5 + 1 + 2) / 6, 3))
        self.assertIsNone(backtest.separation([], [1]))
        self.assertIsNone(backtest.separation([1], []))


class Evaluate(unittest.TestCase):
    def own(self):
        # три ранних контракта по связи, потом один по топливу, потом ещё один по связи
        return [contract("1", "2026-01-01", "A"), contract("2", "2026-01-02", "B"),
                contract("3", "2026-01-03", "B"),
                contract("4", "2026-02-01", "A", code="19.20.21.000", title="Поставка топлива"),
                contract("5", "2026-03-01", "A")]

    def test_only_earlier_contracts_are_known(self):
        report, rows = backtest.evaluate(US, self.own(), {}, min_history=3)
        by = {r.contract_regn: r for r in rows}
        self.assertEqual(sorted(by), ["4", "5"])  # до первых трёх истории не хватает
        self.assertEqual(report.skipped_early, 3)
        # топливо: до него компания топливом не занималась, собственный контракт в профиль не входит
        self.assertEqual(by["4"].area_level, "none")
        self.assertEqual(by["5"].area_level, "high")
        # у заказчика A до контракта 4 была одна победа, до контракта 5 две
        self.assertEqual((by["4"].wins_before, by["5"].wins_before), (1, 2))
        self.assertEqual((report.won.total, report.other.total, report.separation.value), (2, 0, None))
        self.assertEqual((report.separation.positives, report.separation.negatives), (0, 0))
        self.assertEqual(report.won_rated_low_share, 0.5)
        self.assertEqual((report.period_from, report.period_to), ("2026-02-01", "2026-03-01"))

    def test_same_day_contract_is_not_history(self):
        own = self.own() + [contract("6", "2026-03-01", "A", code="19.20.21.000")]
        _, rows = backtest.evaluate(US, own, {}, min_history=3)
        # контракт 5 опубликован в тот же день: он не «раньше»
        self.assertEqual({r.contract_regn: r.wins_before for r in rows}["6"], 2)

    def test_other_contracts_of_customer(self):
        others = {"A": [
            contract("5", "2026-03-01", "A"),  # контракт самой компании: в чужие не идёт
            contract("70", "2026-03-05", "A", code="19.20.21.000", title="Поставка топлива"),
            contract("71", "2026-03-06", "A"),
            contract("72", "2026-03-07", "A", tender=""),  # без извещения
            contract("73", "2025-12-01", "A"),  # раньше, чем у компании набралась история
        ]}
        # ещё одна победа у заказчика B, чужих контрактов которого в сравнении нет
        own = [c for c in self.own() if c.regn != "4"] + [contract("6", "2026-03-02", "B")]
        report, rows = backtest.evaluate(US, own, others, min_history=3)
        other = {r.contract_regn: r for r in rows if not r.is_company}
        self.assertEqual(report.won.total, 2)
        self.assertEqual(sorted(other), ["70", "71"])
        self.assertEqual((report.skipped_no_tender, report.skipped_early), (1, 4))
        self.assertEqual((other["70"].area_level, other["71"].area_level), ("none", "high"))
        self.assertLess(other["70"].score, other["71"].score)
        # выигранная закупка 5 оценена как чужая 71 и выше, чем 70
        self.assertEqual((report.won_at_customers.total, report.other.total), (1, 2))
        self.assertEqual(report.separation.value, 0.75)
        self.assertEqual((report.separation.positives, report.separation.negatives), (1, 2))
        lines = {(f.factor, f.value): (f.won, f.other) for f in report.factors}
        self.assertEqual(lines[("область по кодам", "high")], (1, 1))
        self.assertEqual(lines[("область по кодам", "none")], (0, 1))
        self.assertEqual(report.customers, ["A"])

    def test_customer_price_history_is_cut_by_date(self):
        # шесть закупок заказчика без снижения цены: фактор конкуренции появляется только у закупки,
        # до которой они уже были опубликованы
        cheap = [contract(f"8{i}", "2026-02-10", "A") for i in range(6)]
        late = contract("90", "2026-02-20", "A")
        early = contract("91", "2026-02-05", "A")
        own = [c for c in self.own() if c.regn != "4"]
        _, rows = backtest.evaluate(US, own, {"A": cheap + [late, early]}, min_history=3)
        score = {r.contract_regn: r.score for r in rows}
        self.assertEqual(score["90"] - score["91"], 8)


class Pages(unittest.TestCase):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.urls = []

    def client(self, answer):
        def http(url, timeout):
            self.urls.append(url)
            return json.dumps(answer(url))
        return DamiaClient(self.dir, lambda: KEY, http)

    @staticmethod
    def page(first, count):
        return contracts_of(US, [(str(first + i), "2026-05-01", "5400000001", 90, "61.10.11.120",
                                  "Услуги связи", f"T{first + i}", 100) for i in range(count)])

    def test_full_page_means_more(self):
        self.assertTrue(damia_parse.contracts_page_full(self.page(1, limits.DAMIA_CONTRACTS_PAGE), US))
        self.assertFalse(damia_parse.contracts_page_full(self.page(1, limits.DAMIA_CONTRACTS_PAGE - 1), US))
        self.assertFalse(damia_parse.contracts_page_full({"другой": {}}, US))

    def test_next_page_is_requested_after_full_one(self):
        full = limits.DAMIA_CONTRACTS_PAGE
        client = self.client(lambda url: self.page(1000, 5) if "page=2" in url else self.page(1, full))
        rows, sources, notes = steps.company_contracts(client, US, network=True, max_pages=3)
        self.assertEqual((len(rows), len(sources), len(self.urls)), (full + 5, 2, 2))
        self.assertFalse(any("более ранние" in n for n in notes))

    def test_limit_of_pages_is_reported(self):
        full = limits.DAMIA_CONTRACTS_PAGE
        client = self.client(lambda url: self.page(1 if "page=" not in url else 1000, full))
        rows, sources, notes = steps.company_contracts(client, US, network=True, max_pages=2)
        self.assertEqual(len(self.urls), 2)
        self.assertTrue(any("более ранние контракты" in n for n in notes))

    def test_offline_reads_saved_pages_only(self):
        full = limits.DAMIA_CONTRACTS_PAGE
        client = self.client(lambda url: self.page(1, full))
        steps.company_contracts(client, US, network=True, max_pages=1)
        self.urls.clear()
        with self.assertRaises(steps.NotCached):
            steps.company_contracts(client, US, max_pages=3)
        rows, sources, _ = steps.company_contracts(client, US, max_pages=3, saved_only_after_first=True)
        self.assertEqual((len(rows), len(sources), self.urls), (full, 1, []))
        with self.assertRaises(steps.NotCached):  # первой страницы другой компании нет вовсе
            steps.company_contracts(client, "7700000002", saved_only_after_first=True)


class BacktestStep(unittest.TestCase):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.dir)
        self.requests = []
        rows = [(f"9{i:02}", f"2026-01-{i + 1:02}", "A" if i % 2 else "B", 90, "61.10.11.120",
                 "Услуги связи", f"T{i}", 100) for i in range(24)]
        rows.append(("950", "2026-03-01", "C", 90, "61.10.11.120", "Услуги связи", "T50", 100))
        company = contracts_of(US, rows)
        history = {inn: contracts_of(inn, [
            (f"{inn}1", "2026-02-10", inn, 50, "19.20.21.325", "Поставка топлива", f"H{inn}1", 100),
            (f"{inn}2", "2026-02-11", inn, 99, "61.10.11.120", "Услуги связи", f"H{inn}2", 100),
        ]) for inn in "ABC"}

        def http(url, timeout):
            inn = url.split("inn=")[1].split("&")[0]
            self.requests.append(inn)
            return json.dumps(company if inn == US else history[inn])

        self.client = DamiaClient(self.dir, lambda: KEY, http)

    def test_offline_uses_saved_only(self):
        steps.company_contracts(self.client, US, network=True)
        self.client.call("contracts", inn="B", fz="44", role="1", format="2")
        self.requests.clear()
        result = backtest_step.run(self.client, self.store, US)
        self.assertEqual(self.requests, [])
        self.assertEqual(result.report.customers, ["B"])
        self.assertTrue(any("пропущенных" in t and ": 2." in t for t in result.limitations))
        self.assertEqual(self.store.load("backtest", BacktestFile).report.other.total, 2)

    def test_network_takes_most_frequent_customers(self):
        result = backtest_step.run(self.client, self.store, US, network=True, customers=2)
        # A и B по 12 закупок, C одна: при пределе 2 запрошены A и B
        self.assertEqual(self.requests, [US, "A", "B"])
        self.assertEqual(result.report.customers, ["A", "B"])
        self.assertEqual((result.report.won.total, result.report.other.total), (5, 4))
        self.assertTrue(any("2 из 3" in t for t in result.limitations))
        self.assertTrue(any(s.id == "history:A" for s in result.sources))
        self.assertGreater(result.report.separation.value, 0.5)

    def test_limit_of_customers(self):
        result = backtest_step.run(self.client, self.store, US, network=True,
                                   customers=limits.BACKTEST_CUSTOMERS + 50)
        self.assertEqual(len(self.requests), 4)  # заказчиков всего три
        self.assertEqual(backtest_step.run(self.client, self.store, US, customers=0).report.customers, [])
        with mock.patch.object(limits, "BACKTEST_CUSTOMERS", 1):  # просьба больше предела не проходит
            self.assertEqual(backtest_step.run(self.client, self.store, US, customers=3).report.customers, ["A"])


class Archive(unittest.TestCase):
    @staticmethod
    def pack(files):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as z:
            for name, data in files:
                z.writestr(name, data)
        return buffer.getvalue()

    def test_word_inside_zip(self):
        data = self.pack([("Требования.docx", DOCX), ("Требования.docx.sig", b"\x01\x02"),
                          ("Требования.docx.pdf", b"%PDF-1.7"), ("вложенный.zip", self.pack([("a.docx", DOCX)]))])
        self.assertEqual(textract.detect(data), "zip")
        self.assertEqual(textract.extract(data, "zip"), textract.extract(DOCX, "docx"))

    def test_two_documents_are_joined(self):
        data = self.pack([("1.docx", DOCX), ("2.docx", DOCX)])
        text = textract.extract(DOCX, "docx")
        self.assertEqual(textract.extract(data, "zip"), text + "\n\n" + text)

    def test_zip_without_word(self):
        data = self.pack([("скан.pdf", b"%PDF-1.7"), ("подпись.sig", b"\x01")])
        with self.assertRaisesRegex(textract.Unreadable, "нет файлов Word.*файлов в архиве: 2"):
            textract.extract(data, "zip")

    def test_too_many_files(self):
        data = self.pack([(f"{i}.docx", DOCX) for i in range(limits.ARCHIVE_MAX_FILES + 1)])
        with self.assertRaisesRegex(textract.Unreadable, "предел"):
            textract.extract(data, "zip")
        ok = self.pack([(f"{i}.docx", DOCX) for i in range(limits.ARCHIVE_MAX_FILES)])
        self.assertTrue(textract.extract(ok, "zip"))

    def test_rar_is_named(self):
        self.assertEqual(textract.detect(b"Rar!\x1a\x07\x00" + b"\x00" * 20), "archive")
        with self.assertRaisesRegex(textract.Unreadable, "RAR"):
            textract.extract(b"Rar!", "archive")

    def test_reread_after_download(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        downloader = Downloader(folder)
        data = self.pack([("Требования.docx", DOCX)])
        doc = downloader.store(REGN, BASE + "Z", "Требования.docx.zip", "other", data, "Требования.docx.zip")
        self.assertTrue(doc.readable)
        # файл, сохранённый до появления чтения архивов: текста нет
        meta = folder / "docs" / REGN / f"{doc.id}.txt"
        meta.write_text("", encoding="utf-8")
        again = downloader.reread(REGN, doc)
        self.assertEqual((again.id, again.chars, again.file_name), (doc.id, doc.chars, doc.file_name))
        self.assertEqual(downloader.text(REGN, again), textract.extract(DOCX, "docx"))


class Cutoff(unittest.TestCase):
    """Закупка, приём заявок по которой закончился, оценивается по данным до её объявления."""

    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        company = contracts_of(US, [
            ("9001", "2026-09-11", CUSTOMER, 880, "", "Услуги передачи информации", REGN, 1000),
            ("9002", "2026-05-01", "5400000001", 90, "61.10.11.120", "Услуги местной телефонной связи", "T2", 100),
            ("9003", "2026-10-01", CUSTOMER, 90, "63.99.10.000", "Услуги передачи информации", "T3", 100),
            ("9004", "2026-08-19", "5400000001", 90, "63.99.10.000", "Услуги передачи информации", "T4", 100),
        ])
        history = dict(HISTORY)
        history[CUSTOMER] = dict(HISTORY[CUSTOMER])
        history[CUSTOMER].update(contracts_of(CUSTOMER, [
            ("9003", "2026-10-01", CUSTOMER, 90, "63.99.10.000", "Услуги передачи информации", "T3", 100),
        ])[CUSTOMER])

        def http(url, timeout):
            method = url.split("/zakupki/")[1].split("?")[0]
            if method == "contracts":
                return json.dumps(company if f"inn={US}" in url else history)
            return json.dumps(TENDER)

        self.client = DamiaClient(self.dir, lambda: KEY, http)
        self.store = Store(self.dir)
        steps.build_profile(self.client, self.store, US, network=True)

    def card(self, today):
        return analyze.analyze_tender(self.client, Downloader(self.dir), self.store, None,
                                      LlmCache(self.dir), REGN, network=True, lookups=0, today=today)

    def test_finished_tender_knows_nothing_later(self):
        card = self.card("2026-11-14")  # объявлена 19.08, приём заявок до 27.08
        self.assertEqual(card.customer_history.company_wins, 0)
        self.assertEqual(card.customer_history.contracts, 3)
        self.assertTrue(any("до объявления закупки (2026-08-19)" in t and "убрано 3" in t
                            for t in card.limitations))
        self.assertTrue(any(s.id == "profile" and "Контрактов: 1" in s.comment for s in card.sources))

    def test_open_tender_uses_everything(self):
        card = self.card("2026-08-27")  # последний день приёма заявок: закупка ещё идёт
        self.assertTrue(any(s.id == "profile" and "Контрактов: 3" in s.comment for s in card.sources))
        self.assertEqual(card.customer_history.company_wins, 1)
        self.assertEqual(card.customer_history.contracts, 4)
        self.assertFalse(any("до объявления закупки" in t for t in card.limitations))
        self.assertTrue(any("исход неизвестен" in t for t in card.limitations))


class Export(unittest.TestCase):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.dir)

        def http(url, timeout):
            method = url.split("/zakupki/")[1].split("?")[0]
            if method == "contracts":
                return json.dumps(COMPANY if f"inn={US}" in url else HISTORY)
            return json.dumps(TENDER)

        self.client = DamiaClient(self.dir, lambda: KEY, http)
        steps.build_profile(self.client, self.store, US, network=True)
        analyze.analyze_tender(self.client, Downloader(self.dir), self.store, None,
                               LlmCache(self.dir), REGN, network=True, lookups=0)
        self.target = self.dir / "export"

    def test_package(self):
        (self.dir / "out" / "cards" / "заметки.txt").write_text("не карточка", encoding="utf-8")
        index = export.build(self.store, self.target, US, [KEY])
        files = sorted(p.relative_to(self.target).as_posix() for p in self.target.rglob("*") if p.is_file())
        self.assertEqual(files, [f"cards/{REGN}.json", "index.json", "profile.json"])
        self.assertEqual(sorted(index.files), [f"cards/{REGN}.json", "profile.json"])
        self.assertEqual([(c.regn, c.file) for c in index.cards], [(REGN, f"cards/{REGN}.json")])
        self.assertEqual(len(index.missing), 2)  # списка закупок и проверки ещё нет
        saved = ExportIndex.model_validate_json((self.target / "index.json").read_text("utf-8"))
        self.assertEqual(saved.cards[0].title, index.cards[0].title)
        AnalysisCard.model_validate_json((self.target / index.cards[0].file).read_text("utf-8"))
        ProfileFile.model_validate_json((self.target / "profile.json").read_text("utf-8"))

    def test_old_package_is_replaced(self):
        self.target.mkdir()
        (self.target / "старый.json").write_text("{}", encoding="utf-8")
        export.build(self.store, self.target, US, [])
        self.assertFalse((self.target / "старый.json").exists())
        self.assertFalse(self.target.with_name("export.tmp").exists())

    def test_secret_stops_export(self):
        self.target.mkdir()
        (self.target / "прежний.json").write_text("{}", encoding="utf-8")
        card = self.store.load_card(REGN, AnalysisCard)  # профиль к этому времени уже записан
        card.limitations.append(f"служебная строка {KEY}")
        self.store.save_card(REGN, card)
        with self.assertRaises(export.SecretFound):
            export.build(self.store, self.target, US, ["", KEY])
        # прежний пакет цел, недособранного нет
        self.assertEqual([p.name for p in self.target.iterdir()], ["прежний.json"])
        self.assertFalse(self.target.with_name("export.tmp").exists())


if __name__ == "__main__":
    unittest.main()

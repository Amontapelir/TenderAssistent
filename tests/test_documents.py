import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from app import limits
from app.domain import documents
from app.domain.models import DocRef
from app.pipeline import steps
from app.sources import damia_parse, textract
from app.sources.damia import DamiaClient
from app.sources.documents import Downloader, DownloadRefused, check_url
from app.sources.llm import LlmClient, LlmError, LlmNotConfigured, LlmRateLimited

REGN = "0100000000000000001"
BASE = "https://zakupki.gov.ru/44fz/filestore/public/1.0/download/priz/file.html?uid="
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def docx(body: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", f"<w:document {W}><w:body>{body}</w:body></w:document>")
    return buffer.getvalue()


def p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


DOCX = docx(
    p("Участник должен иметь лицензию.") + "<w:p/>"
    + "<w:tbl><w:tr><w:tc>" + p("Показатель") + "</w:tc><w:tc>" + p("Значение") + "</w:tc></w:tr>"
    + "<w:tr><w:tc>" + p("Срок") + "</w:tc><w:tc>" + p("10 дней") + "</w:tc></w:tr></w:tbl>"
    + "<w:p><w:r><w:t>Обеспечение</w:t></w:r><w:r><w:tab/><w:t>5%</w:t></w:r></w:p>"
)


def ref(title, group="Извещение о проведении электронного аукциона", active=True, uid="A",
        date="2026-08-19"):
    return DocRef(group, date, active, title, BASE + uid)


TENDER = {REGN: {
    "Регион": "Курганская область", "ФЗ": 44, "ДатаПубл": "2026-08-19", "ДатаОконч": "2026-08-27",
    "Заказчик": [{"ИНН": "4500000001", "НаимСокр": "ГКУ"}],
    "СпособРазм": "Электронный аукцион",
    "НачЦена": {"Сумма": 1000}, "ОбеспУчаст": {"Сумма": 50}, "ОбеспИсп": {"Сумма": 300},
    "Продукт": {"ОКПД": "63.99.10.000", "Название": "Услуги передачи информации",
                "ОбъектыЗак": [{"ОКПД": "63.99.10.000"}, {"ОКПД": "61.10.11.120"}]},
    "Протокол": {"Заявки": [{"Номер": "151", "ИНН": "", "Сумма": 880, "Результат": "Заявка отклонена"},
                            {"Номер": "140", "ИНН": "", "Сумма": 1000, "Результат": "Победитель"}]},
    "Контракты": {"2450000000000000026": {"ДатаПодп": "2026-09-11", "Цена": {"Сумма": 880},
                  "Поставщики": {"ЮЛ": [{"ИНН": "7700000001", "НаимСокр": "ПАО"}], "ИП": [], "ФЛ": []}}},
    "Документы": [
        {"Название": "Извещение о проведении электронного аукциона", "ДатаРазм": "2026-07-08",
         "Редакция": "Недействующая",
         "Файлы": [{"Название": "Проект контракта", "Url": BASE + "OLD"}]},
        {"Название": "Изменение извещения о проведении электронного аукциона", "ДатаРазм": "2026-08-19",
         "Редакция": "Действующая",
         "Файлы": [{"Название": "Проект контракта", "Url": BASE + "C"},
                   {"Название": "Требования к содержанию и составу заявки", "Url": BASE + "R"}]},
        {"Название": "Протокол подведения итогов", "ДатаРазм": "2026-08-31", "Редакция": "Действующая",
         "Файлы": [{"Название": "Электронный документ.pdf", "Url": BASE + "P"}]},
    ],
    "Статус": {"Статус": "Закупка завершена"},
}}


class Resp:
    def __init__(self, status=200, body=b"", headers=None):
        self.status, self.headers, self._body = status, headers or {}, io.BytesIO(body)

    def read(self, n):
        return self._body.read(n)


class Net:
    """Поддельная сеть: {адрес: ответ}. Запоминает, куда ходили."""

    def __init__(self, pages):
        self.pages, self.visited = pages, []

    def __call__(self, url, timeout):
        self.visited.append(url)
        return self.pages[url]


class Select(unittest.TestCase):
    def test_order_and_reasons(self):
        chosen, skipped = documents.select([
            ref("Обоснование начальной (максимальной) цены контракта", uid="1"),
            ref("Проект контракта", uid="2"),
            ref("Описание объекта закупки (Техническое задание)", uid="3"),
            ref("Требования к содержанию и составу заявки на участие в аукционе", uid="4"),
            ref("Проект контракта", active=False, uid="5"),
            ref("Электронный документ.pdf", group="Протокол подведения итогов", uid="6"),
            ref("Ответ на запрос", group="Разъяснения положений извещения", uid="7"),
            ref("Проект контракта", uid="2"),
        ], limit=3)
        self.assertEqual([c.role for c in chosen], ["requirements", "contract_draft", "specification"])
        reasons = {s.doc.url[-1] + ":" + s.reason for s in skipped}
        self.assertIn("5:недействующая редакция", reasons)
        self.assertIn("2:тот же файл уже отобран", reasons)
        self.assertIn("7:предел 3 файлов на закупку", reasons)
        self.assertTrue(any(r.startswith("6:протокол") for r in reasons))
        self.assertTrue(any(r.startswith("1:обоснование цены") for r in reasons))
        self.assertEqual(len(chosen) + len(skipped), 8)  # ни один файл не потерян

    def test_newer_first_inside_role(self):
        chosen, _ = documents.select([
            ref("Ответ 1", group="Разъяснения положений", uid="1", date="2026-07-16"),
            ref("Ответ 2", group="Разъяснения положений", uid="2", date="2026-07-24"),
        ], limit=1)
        self.assertEqual(chosen[0].doc.title, "Ответ 2")


class Split(unittest.TestCase):
    def test_parts_fit_and_nothing_is_lost(self):
        text = "\n".join(f"Абзац номер {i}: " + "слово " * 40 for i in range(50))
        parts = documents.split_text(text, 1000)
        self.assertGreater(len(parts), 5)
        self.assertTrue(all(len(part) <= 1000 for part in parts))
        self.assertEqual("\n".join(parts).split(), text.split())

    def test_long_paragraph_is_cut(self):
        parts = documents.split_text("слово " * 1000, 500)
        self.assertTrue(all(len(part) <= 500 for part in parts))
        self.assertEqual(sum(part.count("слово") for part in parts), 1000)


class Text(unittest.TestCase):
    def test_docx_paragraphs_and_tables(self):
        self.assertEqual(textract.detect(DOCX), "docx")
        self.assertEqual(textract.extract(DOCX, "docx").split("\n"), [
            "Участник должен иметь лицензию.", "Показатель | Значение", "Срок | 10 дней",
            "Обеспечение\t5%",
        ])

    def test_formats(self):
        self.assertEqual(textract.detect(b"%PDF-1.7 ..."), "pdf")
        self.assertEqual(textract.detect(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"), "doc")
        self.assertEqual(textract.detect(b"<html><body>captcha</body></html>"), "html")
        with self.assertRaises(textract.Unreadable) as caught:
            textract.extract(b"%PDF-1.7", "pdf")
        self.assertIn("PDF", str(caught.exception))

    def test_docx_with_entities_is_refused(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                "word/document.xml",
                f'<!DOCTYPE x [<!ENTITY a "текст">]><w:document {W}><w:body>{p("&a;")}</w:body></w:document>')
        with self.assertRaises(textract.Unreadable):
            textract.extract(buffer.getvalue(), "docx")


class Download(unittest.TestCase):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_only_expected_addresses(self):
        check_url(BASE + "A")
        for bad in ("http://zakupki.gov.ru/file", "https://evil.example/file",
                    "https://zakupki.gov.ru.evil.example/file", "https://zakupki.gov.ru@evil.example/f",
                    "https://zakupki.gov.ru:8443/file", "file:///etc/passwd"):
            with self.assertRaises(DownloadRefused, msg=bad):
                check_url(bad)

    def test_saved_once(self):
        net = Net({BASE + "A": Resp(body=DOCX, headers={
            "content-disposition": "attachment; filename*=UTF-8''%D0%A2%D0%97.docx"})})
        loader = Downloader(self.dir, net)
        first = loader.fetch(REGN, BASE + "A", "ТЗ", "specification")
        second = loader.fetch(REGN, BASE + "A", "ТЗ", "specification")
        self.assertEqual(len(net.visited), 1)
        self.assertEqual(first, second)
        self.assertEqual((first.kind, first.readable, first.file_name), ("docx", True, "ТЗ.docx"))
        self.assertIn("лицензию", loader.text(REGN, first))

    def test_redirect_to_foreign_host_is_refused(self):
        net = Net({BASE + "A": Resp(302, headers={"location": "https://evil.example/x"})})
        with self.assertRaises(DownloadRefused):
            Downloader(self.dir, net).fetch(REGN, BASE + "A", "ТЗ", "other")
        self.assertEqual(net.visited, [BASE + "A"])

    def test_redirect_inside_site_is_followed(self):
        net = Net({BASE + "A": Resp(302, headers={"location": "/44fz/real"}),
                   "https://zakupki.gov.ru/44fz/real": Resp(body=DOCX)})
        self.assertTrue(Downloader(self.dir, net).fetch(REGN, BASE + "A", "ТЗ", "other").readable)

    def test_redirect_loop_stops(self):
        net = Net({BASE + "A": Resp(302, headers={"location": BASE + "A"})})
        with self.assertRaises(DownloadRefused):
            Downloader(self.dir, net).fetch(REGN, BASE + "A", "ТЗ", "other")
        self.assertEqual(len(net.visited), limits.DOC_MAX_REDIRECTS + 1)

    def test_too_big(self):
        big = Resp(headers={"content-length": str(limits.DOC_MAX_BYTES + 1)})
        with self.assertRaises(DownloadRefused):
            Downloader(self.dir, Net({BASE + "A": big})).fetch(REGN, BASE + "A", "ТЗ", "other")

        class Endless:  # сервер не назвал размер и отдаёт данные без конца
            status, headers, served = 200, {}, 0

            def read(self, n):
                self.served += n
                return b"x" * n

        endless = Endless()
        with self.assertRaises(DownloadRefused):
            Downloader(self.dir, Net({BASE + "B": endless})).fetch(REGN, BASE + "B", "ТЗ", "other")
        self.assertLessEqual(endless.served, limits.DOC_MAX_BYTES + 2**20)  # остановились у предела

    def test_unreadable_is_kept_with_reason(self):
        net = Net({BASE + "A": Resp(body=b"%PDF-1.7 scan")})
        doc = Downloader(self.dir, net).fetch(REGN, BASE + "A", "Скан", "other")
        self.assertFalse(doc.readable)
        self.assertIn("PDF", doc.problem)


class TenderSteps(unittest.TestCase):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.client = DamiaClient(self.dir, lambda: "ключ", lambda url, t: json.dumps(TENDER))
        self.client.call("zakupka", regn=REGN, actual="1")

    def test_card(self):
        card = damia_parse.parse_tender(TENDER, REGN)
        self.assertEqual((card.initial_price, card.bid_security, card.contract_security), (1000, 50, 300))
        self.assertEqual(card.okpd_codes, ("63.99.10.000", "61.10.11.120"))
        self.assertEqual(card.contracts[0].supplier_inns, ("7700000001",))
        self.assertEqual([b.amount for b in card.bids], [880, 1000])
        self.assertEqual(len(card.documents), 4)
        self.assertIsNone(damia_parse.parse_tender({}, REGN))

    def test_listing_downloads_nothing(self):
        net = Net({})
        result = steps.tender_documents(self.client, Downloader(self.dir, net), REGN)
        self.assertEqual(net.visited, [])
        self.assertEqual([i.role for i in result.items], ["requirements", "contract_draft"])
        self.assertEqual(len(result.skipped), 2)
        self.assertTrue(any("ещё не скачаны: 2" in t for t in result.limitations))

    def test_fetch_reports_each_failure(self):
        net = Net({BASE + "R": Resp(body=DOCX), BASE + "C": Resp(404)})
        result = steps.tender_documents(self.client, Downloader(self.dir, net), REGN, download=True)
        by_role = {i.role: i for i in result.items}
        self.assertTrue(by_role["requirements"].stored.readable)
        self.assertIsNone(by_role["contract_draft"].stored)
        self.assertIn("404", by_role["contract_draft"].error)
        self.assertEqual(result.readable, 1)


def chat(content, status=200, headers=None):
    body = json.dumps({"model": "m", "choices": [{"message": {"content": content}}],
                       "usage": {"prompt_tokens": 7, "completion_tokens": 3}})
    return status, headers or {}, body


class Llm(unittest.TestCase):
    KEY = "sk-секрет"

    def client(self, replies):
        self.calls, self.waits = [], []
        queue = list(replies)

        def post(url, headers, body, timeout):
            self.calls.append((url, headers, json.loads(body)))
            return queue.pop(0)

        return LlmClient("https://api.example/v1/", self.KEY, "m", post, self.waits.append)

    def test_json_answer(self):
        reply = self.client([chat('{"ответ": "Москва"}')]).ask_json("система", "вопрос")
        self.assertEqual(reply.data, {"ответ": "Москва"})
        url, headers, payload = self.calls[0]
        self.assertEqual(url, "https://api.example/v1/chat/completions")
        self.assertEqual(headers["Authorization"], f"Bearer {self.KEY}")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["temperature"], 0)

    def test_retry_after_rate_limit(self):
        client = self.client([chat("", 429, {"Retry-After": "2"}), chat('{"a": 1}')])
        self.assertEqual(client.ask_json("s", "u").attempts, 2)
        self.assertEqual(self.waits, [2.0])

    def test_wait_is_capped_and_retries_end(self):
        client = self.client([chat("", 429, {"retry-after": "9999"})] * (limits.LLM_RETRIES + 1))
        with self.assertRaises(LlmRateLimited):
            client.ask_json("s", "u")
        self.assertEqual(len(self.calls), limits.LLM_RETRIES + 1)
        self.assertEqual(set(self.waits), {float(limits.LLM_RETRY_WAIT_MAX)})

    def test_key_is_cut_from_errors(self):
        client = self.client([(401, {}, f"bad key {self.KEY}")])
        with self.assertRaises(LlmError) as caught:
            client.ask_json("s", "u")
        self.assertNotIn(self.KEY, str(caught.exception))
        self.assertEqual(len(self.calls), 1)  # отказ по ключу не повторяется

    def test_not_json_is_an_error(self):
        with self.assertRaises(LlmError):
            self.client([chat("Конечно! Вот ответ: Москва")]).ask_json("s", "u")

    def test_must_be_configured(self):
        with self.assertRaises(LlmNotConfigured):
            LlmClient("", "", "")
        with self.assertRaises(LlmNotConfigured):
            LlmClient("http://api.example/v1", "k", "m")

    def test_check_model(self):
        self.assertTrue(steps.check_model(self.client([chat('{"ответ": "Москва"}')])).ok)
        self.assertFalse(steps.check_model(self.client([chat('{"ответ": "Париж"}')])).ok)


if __name__ == "__main__":
    unittest.main()

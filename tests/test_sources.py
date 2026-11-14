import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from app import limits
from app.api.access import is_local_request
from app.pipeline import steps
from app.schemas.card import ProfileFile
from app.sources import damia_parse
from app.sources.damia import DamiaClient, DamiaError, QuotaExceeded
from app.storage.files import Store

KEY = "секретный-ключ-123"
INN = "7700000001"

CONTRACTS = {INN: {
    "1000000000000000001": {
        "ДатаПубл": "2026-11-09", "ДатаОконч": "2027-12-31", "ЗаказчикИНН": "5400000001",
        "Цена": {"Сумма": 80, "ВалютаКод": "RUB"},
        "Продукт": {"ОКПД": "61.20.11.000", "Название": "Услуги подвижной связи"},
        "Закупка": {"НомерИзвещения": "0100000000000000001", "Сумма": 100},
        "Статус": {"Статус": "Исполнение"},
    },
    "1000000000000000002": {
        "ДатаПубл": "2026-11-06", "ДатаОконч": "2027-12-31", "ЗаказчикИНН": "5400000001",
        "Цена": {"Сумма": 15600.5, "ВалютаКод": "RUB"},
        "Продукт": {"ОКПД": "", "Название": "Услуги местной телефонной связи"},
        "Закупка": {"НомерИзвещения": "", "Сумма": 0},
        "Статус": {"Статус": "Исполнение"},
    },
    "1000000000000000003": "мусор",
}}

SEARCH = {
    "44": {"0100000000000000009": {
        "ДатаПубл": "2026-11-02", "ДатаОконч": "2026-11-30", "ВремяОконч": "09:00:00",
        "Продукт": "Оказание услуг подвижной связи",
        "Заказчик": {"ИНН": "5400000001", "НаимПолн": "УЧРЕЖДЕНИЕ"},
        "Сумма": 571435.35, "Статус": ""}},
    "223": {}, "615": [], "next_page": False,
}


class FakeHttp:
    def __init__(self, body):
        self.body, self.urls = body, []

    def __call__(self, url, timeout):
        self.urls.append(url)
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class Parse(unittest.TestCase):
    def test_contracts(self):
        contracts, bad = damia_parse.parse_contracts(CONTRACTS, INN, "0")
        self.assertEqual((len(contracts), bad), (2, 1))
        first, second = contracts
        self.assertEqual(first.supplier_inns, (INN,))
        self.assertEqual((first.price, first.tender_price), (80.0, 100.0))
        self.assertEqual(first.tender_regn, "0100000000000000001")
        self.assertEqual((second.okpd, second.tender_regn, second.tender_price), ("", "", 0.0))

    def test_search(self):
        tenders, by_law = damia_parse.parse_search(SEARCH)
        self.assertEqual(by_law, {"44": 1, "223": 0, "615": 0})
        self.assertEqual(tenders[0].customer_inn, "5400000001")
        self.assertEqual(tenders[0].deadline, "2026-11-30")

    def test_next_page_as_text(self):
        self.assertFalse(damia_parse.has_next_page({"next_page": "False"}))
        self.assertTrue(damia_parse.has_next_page({"next_page": "True"}))


class Client(unittest.TestCase):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def client(self, body):
        http = FakeHttp(body)
        return DamiaClient(self.dir, lambda: KEY, http), http

    def test_second_call_is_free(self):
        client, http = self.client(json.dumps(CONTRACTS))
        first = client.call("contracts", inn=INN, role="0")
        second = client.call("contracts", inn=INN, role="0")
        self.assertFalse(first.from_cache)
        self.assertTrue(second.from_cache)
        self.assertEqual(len(http.urls), 1)
        used = {q.method: q.used for q in client.quota()}
        self.assertEqual(used["contracts"], 1)

    def test_key_is_not_stored(self):
        client, _ = self.client(json.dumps(CONTRACTS))
        client.call("contracts", inn=INN)
        for path in self.dir.rglob("*"):
            if path.is_file():
                self.assertNotIn(KEY, path.read_text(encoding="utf-8"))

    def test_cap_stops_before_request(self):
        client, http = self.client(json.dumps({}))
        (self.dir / "quota.json").write_text(json.dumps({"zakupka": limits.DAMIA_CAP}), "utf-8")
        with self.assertRaises(QuotaExceeded):
            client.call("zakupka", regn="1")
        self.assertEqual(http.urls, [])

    def test_key_is_cut_from_errors(self):
        client, _ = self.client(urllib.error.URLError(f"нет связи с ...key={KEY}"))
        with self.assertRaises(DamiaError) as caught:
            client.call("customer", req=INN)
        self.assertNotIn(KEY, str(caught.exception))

    def test_unknown_method(self):
        client, http = self.client("{}")
        with self.assertRaises(DamiaError):
            client.call("zdelete", inn=INN)
        self.assertEqual(http.urls, [])


class Steps(unittest.TestCase):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.dir)

    def test_offline_refuses_to_spend(self):
        http = FakeHttp(json.dumps(CONTRACTS))
        client = DamiaClient(self.dir, lambda: KEY, http)
        with self.assertRaises(steps.NotCached):
            steps.build_profile(client, self.store, INN, network=False)
        self.assertEqual(http.urls, [])

    def test_profile_then_search(self):
        client = DamiaClient(self.dir, lambda: KEY, FakeHttp(json.dumps(CONTRACTS)))
        built = steps.build_profile(client, self.store, INN, network=True)
        self.assertEqual(built.profile.contracts, 2)
        self.assertTrue(any("не удалось разобрать: 1" in t for t in built.profile.limitations))
        self.assertEqual(self.store.load("profile", ProfileFile).profile.inn, INN)

        client = DamiaClient(self.dir, lambda: KEY, FakeHttp(json.dumps(SEARCH)))
        found = steps.search_tenders(client, self.store, "услуги связи", "2026-11-01",
                                     network=True, today="2026-11-14")
        self.assertEqual((found.total, found.open_total), (1, 1))
        self.assertTrue(found.items[0].known_customer)
        self.assertGreater(found.items[0].text_match, 0)

    def test_card_name_cannot_leave_folder(self):
        with self.assertRaises(ValueError):
            self.store.card_path("../../.env")


class Access(unittest.TestCase):
    def test_local(self):
        self.assertTrue(is_local_request("127.0.0.1", "localhost:8765", None))
        self.assertTrue(is_local_request("127.0.0.1", "127.0.0.1:8765", "http://localhost:3000"))
        self.assertTrue(is_local_request("::1", "[::1]:8765", "http://[::1]:8765"))

    def test_foreign_client(self):
        self.assertFalse(is_local_request("192.168.1.5", "localhost:8765", None))
        self.assertFalse(is_local_request(None, "localhost:8765", None))

    def test_foreign_site_in_owner_browser(self):
        self.assertFalse(is_local_request("127.0.0.1", "localhost:8765", "https://evil.example"))
        self.assertFalse(is_local_request("127.0.0.1", "localhost:8765", "null"))

    def test_dns_rebinding(self):
        self.assertFalse(is_local_request("127.0.0.1", "evil.example:8765", None))
        self.assertFalse(is_local_request("127.0.0.1", None, None))


if __name__ == "__main__":
    unittest.main()

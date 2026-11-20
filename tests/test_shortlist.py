import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pydantic import ValidationError


from app import limits
from app.api import routes
from app.domain import backtest, candidates, okpd, shortlist
from app.domain import profile as profile_domain
from app.domain.models import TenderBrief
from app.pipeline import export, shortlist as shortlist_step, steps
from app.schemas.card import ShortlistFile
from app.sources.damia import DamiaClient, QuotaExceeded
from app.storage.files import Store
from tests.test_analysis import US, contracts_of
from tests.test_backtest import contract

KEY = "секретный-ключ-123"
TODAY = "2026-11-20"


def brief(regn, title, customer="A", price=100.0, deadline="2026-11-30", status=""):
    return TenderBrief(regn=regn, law="44", title=title, customer_inn=customer, customer_name="Заказчик " + customer,
                       price=price, published="2026-11-10", deadline=deadline, status=status)


def company_profile():
    """Компания делает связь; у заказчика A у неё три контракта, у B один."""
    rows = [contract("1", "2026-01-01", "A", title="Услуги телефонной связи"),
            contract("2", "2026-01-02", "A", title="Услуги телефонной связи"),
            contract("3", "2026-01-03", "A", title="Услуги местной телефонной связи"),
            contract("4", "2026-01-04", "B", title="Услуги связи")]
    return profile_domain.build_profile(US, rows)


class Words(unittest.TestCase):
    def test_most_common_whole_words(self):
        titles = ["Оказание услуг телефонной связи", "Услуги телефонной связи", "Услуги местной связи",
                  "Поставка топлива", "Нужды учреждения"]
        self.assertEqual(okpd.title_words(titles, 5), ["связи", "телефонной"])  # остальное реже двух раз или шум

    def test_limit_and_empty(self):
        titles = ["телефонной связи"] * 3
        self.assertEqual(okpd.title_words(titles, 1), ["связи"])
        self.assertEqual(okpd.title_words(titles, 0), [])
        self.assertEqual(okpd.title_words([], 5), [])

    def test_most_frequent_form_wins(self):
        self.assertEqual(okpd.title_words(["сотовая связь", "сотовая связь", "сотовой связи"], 3),
                         ["сотовая", "связь"])


class Score(unittest.TestCase):
    def test_parts(self):
        self.assertEqual(shortlist.score(1.0, 0, False), 70)
        self.assertEqual(shortlist.score(1.0, 1, False), 85)
        self.assertEqual(shortlist.score(1.0, 3, False), 95)
        self.assertEqual(shortlist.score(0.5, 0, True), 20)
        self.assertEqual(shortlist.score(0.0, 0, True), 0)  # ниже нуля не бывает
        self.assertEqual(shortlist.score(2.0, 9, False), 100)  # и выше ста тоже


class Pick(unittest.TestCase):
    def setUp(self):
        self.profile = company_profile()

    def rank(self, tenders):
        return candidates.rank(tenders, self.profile, TODAY)

    def test_only_open_ones_sorted_by_score(self):
        ranked = self.rank([
            brief("1", "Услуги телефонной связи", customer="B"),
            brief("2", "Услуги телефонной связи", customer="A"),  # тот же заказчик с тремя контрактами
            brief("3", "Услуги телефонной связи", customer="A", deadline="2026-11-01"),  # закончен
            brief("4", "Услуги телефонной связи", customer="A", status="Отменена"),
            brief("5", "Поставка топлива", customer="C")])
        picks = shortlist.pick(ranked, self.profile, {"2": ["связи"]})
        self.assertEqual([p.candidate.regn for p in picks], ["2", "1", "5"])
        self.assertGreater(picks[0].score, picks[1].score)
        self.assertGreater(picks[1].score, picks[2].score)
        self.assertEqual((picks[0].wins_at_customer, picks[1].wins_at_customer), (3, 1))
        self.assertEqual(picks[0].found_by, ("связи",))
        self.assertTrue(any("контрактов: 3" in r for r in picks[0].reasons))
        self.assertTrue(any("не похоже" in r for r in picks[2].reasons))
        self.assertTrue(any("общих слов" in r for r in picks[0].reasons))

    def test_larger_than_usual_is_marked_and_lowers_score(self):
        ranked = self.rank([brief("1", "Услуги телефонной связи", price=1e12),
                            brief("2", "Услуги телефонной связи", price=100.0)])
        picks = {p.candidate.regn: p for p in shortlist.pick(ranked, self.profile, {})}
        self.assertTrue(picks["1"].larger_than_usual)
        self.assertFalse(picks["2"].larger_than_usual)
        self.assertEqual(picks["2"].score - picks["1"].score, 15)

    def test_top_is_cut(self):
        ranked = self.rank([brief(str(i), "Услуги телефонной связи") for i in range(30)])
        self.assertEqual(len(shortlist.pick(ranked, self.profile, {})), limits.SHORTLIST_TOP)
        self.assertEqual(len(shortlist.pick(ranked, self.profile, {}, top=3)), 3)


def feature(regn, is_company, text_match, wins=0, price=100.0, typical=1000.0):
    return backtest.Features(
        contract_regn=regn, tender_regn="T" + regn, published="2026-05-01", customer_inn="A",
        is_company=is_company, area_level="unknown", text_match=text_match, wins_before=wins,
        customer_contracts=5, no_reduction_share=None, observations=0, tender_price=price,
        typical_price_max=typical)


STAMP = backtest.ProfileStamp(contracts=1, period_from="2026-01-01", period_to="2026-02-01", fingerprint="x")


class Check(unittest.TestCase):
    def test_separates_when_won_look_like_company_work(self):
        won = [feature(f"w{i}", True, 0.8) for i in range(12)]
        other = [feature(f"o{i}", False, 0.1) for i in range(30)]
        q = shortlist.check(won + other, STAMP)
        self.assertEqual((q.won, q.other), (12, 30))
        self.assertEqual(q.separation.value, 1.0)
        self.assertEqual(q.verdict, backtest.WORKS)
        self.assertEqual((q.top_n, q.top_won_share, q.base_won_share), (10, 1.0, round(12 / 42, 3)))

    def test_does_not_separate_when_scores_are_equal(self):
        rows = [feature(f"w{i}", True, 0.5) for i in range(12)] + [feature(f"o{i}", False, 0.5) for i in range(12)]
        q = shortlist.check(rows, STAMP)
        self.assertEqual(q.separation.value, 0.5)
        self.assertEqual(q.verdict, backtest.INDISTINGUISHABLE)

    def test_nothing_to_compare(self):
        self.assertIsNone(shortlist.check([feature("w", True, 0.5)], STAMP))
        self.assertIsNone(shortlist.check([feature("o", False, 0.5)], STAMP))
        self.assertIsNone(shortlist.check([], STAMP))

    def test_too_few_is_not_a_verdict(self):
        q = shortlist.check([feature("w", True, 0.9), feature("o", False, 0.1)], STAMP)
        self.assertEqual(q.verdict, backtest.TOO_FEW)


def search_answer(items):
    return {"44": {regn: {"ДатаПубл": "2026-11-10", "ДатаОконч": "2026-11-30", "Продукт": title,
                          "Заказчик": {"ИНН": customer, "НаимПолн": "Заказчик " + customer},
                          "Сумма": 100.0, "Статус": ""} for regn, title, customer in items},
            "223": {}, "615": [], "next_page": False}


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.urls = []
        self.search = {"связи": [("111", "Услуги телефонной связи", "A"), ("222", "Поставка топлива", "C")],
                       "телефонной": [("111", "Услуги телефонной связи", "A"), ("333", "Услуги связи", "B")]}
        self.client = DamiaClient(self.dir, lambda: KEY, self.http)
        self.store = Store(self.dir)
        rows = [("1", "2026-01-01", "A", 90, "61.10.11.120", "Услуги телефонной связи", "T1", 100),
                ("2", "2026-01-02", "A", 90, "61.10.11.120", "Услуги телефонной связи", "T2", 100),
                ("3", "2026-01-03", "B", 90, "61.10.11.120", "Услуги связи", "T3", 100)]
        self.company = contracts_of(US, rows)
        steps.build_profile(self.client, self.store, US, network=True)

    def http(self, url, timeout):
        self.urls.append(url)
        query = parse_qs(urlparse(url).query)
        if "/contracts?" in url:
            return json.dumps(self.company)
        word = query["q"][0]
        return json.dumps(search_answer(self.search.get(word, [])))

    def searches(self):
        return [u for u in self.urls if "/zsearch?" in u]

    def test_nothing_saved_and_no_permission(self):
        with self.assertRaises(steps.NotCached):
            shortlist_step.run(self.client, self.store, US, network=False, today=TODAY)
        self.assertEqual(self.searches(), [])
        self.assertIsNone(self.store.load("shortlist", ShortlistFile))

    def test_profile_is_required(self):
        empty = Store(Path(self.enterContext(tempfile.TemporaryDirectory())))
        with self.assertRaises(steps.NotCached):
            shortlist_step.run(self.client, empty, US, network=True, today=TODAY)

    def test_plan_names_the_price_before_spending(self):
        plan = shortlist_step.plan(self.client, US, today=TODAY)
        self.assertEqual([(w.word, w.cached) for w in plan.words], [("связи", False), ("телефонной", False)])
        self.assertEqual((plan.to_spend, plan.budget, plan.from_date), (2, limits.SHORTLIST_QUERIES, "2026-10-21"))
        self.assertEqual(plan.quota_left, limits.DAMIA_CAP)
        self.assertEqual(self.searches(), [])
        shortlist_step.run(self.client, self.store, US, network=True, today=TODAY)
        again = shortlist_step.plan(self.client, US, today=TODAY)
        self.assertEqual((again.to_spend, again.quota_left), (0, limits.DAMIA_CAP - 2))
        self.assertTrue(all(w.cached for w in again.words))

    def test_run_merges_searches_and_spends_one_request_per_word(self):
        result = shortlist_step.run(self.client, self.store, US, network=True, today=TODAY)
        self.assertEqual(len(self.searches()), 2)
        self.assertEqual(result.requests_spent, 2)
        self.assertEqual(result.found_total, 3)  # 111 нашли оба слова
        by = {p.candidate.regn: p for p in result.items}
        self.assertEqual(by["111"].found_by, ("связи", "телефонной"))
        self.assertEqual([p.candidate.regn for p in result.items][0], "111")  # лучший: похоже и заказчик знаком
        self.assertEqual(self.store.load("shortlist", ShortlistFile), result)
        used = {q.method: q.used for q in self.client.quota()}
        self.assertEqual(used["zsearch"], 2)

    def test_repeat_is_free(self):
        shortlist_step.run(self.client, self.store, US, network=True, today=TODAY)
        before = len(self.urls)
        again = shortlist_step.run(self.client, self.store, US, network=True, today=TODAY)
        self.assertEqual((len(self.urls), again.requests_spent), (before, 0))
        self.assertTrue(all(q.from_cache for q in again.queries))

    def test_offline_run_uses_only_saved_searches(self):
        shortlist_step.run(self.client, self.store, US, network=True, today=TODAY)
        before = len(self.urls)
        again = shortlist_step.run(self.client, self.store, US, network=False, today=TODAY)
        self.assertEqual((len(self.urls), again.requests_spent), (before, 0))

    def test_other_day_means_new_search_and_needs_permission(self):
        shortlist_step.run(self.client, self.store, US, network=True, today=TODAY)
        with self.assertRaises(steps.NotCached):
            shortlist_step.run(self.client, self.store, US, network=False, today="2026-11-21")

    def test_budget_is_a_hard_cap(self):
        result = shortlist_step.run(self.client, self.store, US, network=True, today=TODAY, queries=1)
        self.assertEqual((len(self.searches()), len(result.queries)), (1, 1))
        self.assertEqual(shortlist_step.plan(self.client, US, today=TODAY, queries=999).budget, limits.SHORTLIST_QUERIES)
        # схема запроса: больше потолка не принимается
        with self.assertRaises(ValidationError):
            routes.RunShortlist(queries=limits.SHORTLIST_QUERIES + 1)
        with self.assertRaises(ValidationError):
            routes.RunShortlist(queries=0)

    def test_plan_never_names_more_words_than_the_cap(self):
        many = contracts_of(US, [(str(i), "2026-01-01", "A", 90, "61.10.11.120", chr(1072 + i % 13) * 6,
                                  f"T{i}", 100) for i in range(26)])
        self.company = many
        client = DamiaClient(Path(self.enterContext(tempfile.TemporaryDirectory())), lambda: KEY, self.http)
        steps.build_profile(client, Store(client.raw_dir.parent), US, network=True)
        plan = shortlist_step.plan(client, US, today=TODAY, queries=999)  # просят больше потолка
        self.assertEqual(len(plan.words), limits.SHORTLIST_QUERIES)
        self.assertEqual(len(shortlist_step.plan(client, US, today=TODAY, queries=3).words), 3)

    def test_quota_runs_out_midway_keeps_what_was_found(self):
        (self.dir / "quota.json").write_text(json.dumps({"zsearch": limits.DAMIA_CAP - 1, "contracts": 1}), "utf-8")
        result = shortlist_step.run(self.client, self.store, US, network=True, today=TODAY)
        self.assertEqual((len(result.queries), result.requests_spent), (1, 1))
        self.assertTrue(any("Поиск остановлен" in n for n in result.limitations))
        self.assertGreater(len(result.items), 0)

    def test_timeout_on_a_later_word_keeps_what_was_found(self):
        original = self.http

        def slow_second(url, timeout):
            if "/zsearch?" in url and len(self.searches()) == 1:
                self.urls.append(url)
                raise TimeoutError("The read operation timed out")
            return original(url, timeout)
        self.client._http_get = slow_second
        result = shortlist_step.run(self.client, self.store, US, network=True, today=TODAY)
        self.assertEqual((len(result.queries), result.requests_spent), (1, 1))
        self.assertTrue(any("не ответил" in n for n in result.limitations))

    def test_quota_already_empty_is_an_error_not_an_empty_list(self):
        (self.dir / "quota.json").write_text(json.dumps({"zsearch": limits.DAMIA_CAP, "contracts": 1}), "utf-8")
        with self.assertRaises(QuotaExceeded):
            shortlist_step.run(self.client, self.store, US, network=True, today=TODAY)
        self.assertEqual(self.searches(), [])
        self.assertIsNone(self.store.load("shortlist", ShortlistFile))

    def test_limitations_say_what_is_cut(self):
        result = shortlist_step.run(self.client, self.store, US, network=True, today=TODAY)
        text = " ".join(result.limitations)
        for part in ("Порядок предварительный", "не вероятность победы", "опубликованные с 2026-10-21",
                     f"не больше {limits.SHORTLIST_TOP}", "главным заказчикам профиля"):
            self.assertIn(part, text)

    def test_more_pages_are_reported(self):
        original = self.http

        def with_next(url, timeout):
            data = json.loads(original(url, timeout))
            if "/zsearch?" in url:
                data["next_page"] = True
            return json.dumps(data)
        client = DamiaClient(Path(self.enterContext(tempfile.TemporaryDirectory())), lambda: KEY, with_next)
        store = Store(client.raw_dir.parent)
        steps.build_profile(client, store, US, network=True)
        result = shortlist_step.run(client, store, US, network=True, today=TODAY)
        self.assertTrue(all(q.more_pages for q in result.queries))
        self.assertIn("следующая страница", " ".join(result.limitations))

    def test_package_includes_the_shortlist(self):
        shortlist_step.run(self.client, self.store, US, network=True, today=TODAY)
        index = export.build(self.store, self.dir / "export", US, [KEY])
        self.assertIn("shortlist.json", index.files)
        ShortlistFile.model_validate_json((self.dir / "export" / "shortlist.json").read_text("utf-8"))


if __name__ == "__main__":
    unittest.main()

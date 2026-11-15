import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from app import limits
from app.api import routes
from app.domain import backtest, chance
from app.pipeline import backtest as backtest_step, steps
from app.schemas.card import BacktestFile
from app.sources.damia import DamiaClient
from app.storage.files import Store
from tests.test_analysis import contracts_of
from tests.test_backtest import KEY, contract

US = "7700000001"


class Rules(unittest.TestCase):
    CASE = dict(area_level="high", customer_contracts=10, company_wins_at_customer=4,
                average_bids=1.0, documents_read=True)

    def test_current_weights_are_unchanged(self):
        # Веса не менялись по итогам проверки (решение владельца): этот тест это закрепляет.
        r = chance.DEFAULT_RULES
        self.assertEqual((r.base, r.floor, r.ceiling), (45, 5, 90))
        self.assertEqual(dict(r.area), {"high": 15, "medium": 5, "low": -10, "none": -25})
        self.assertEqual((r.wins_many, r.wins_some, r.wins_none), (20, 12, -5))
        self.assertEqual((r.no_reduction_high, r.no_reduction_low, r.larger_than_usual, r.gap_each), (8, -8, -5, -25))
        self.assertEqual((r.level_high_from, r.level_medium_from, r.half_confident, r.half_unsure), (60, 35, 15, 20))

    def test_known_scores(self):
        # 45 + 15 (область) + 20 (три победы и больше) + 15 (одна заявка) = 95, потолок 90, ±15
        strong = chance.estimate(chance.ChanceInput(**self.CASE))
        self.assertEqual((strong.range_low, strong.range_high, strong.level), (75, 95, "high"))
        # 45 - 25 - 5 = 15, диапазон ±20: 0..35
        weak = chance.estimate(chance.ChanceInput(area_level="none", text_match=0.0, customer_contracts=3,
                                                  company_wins_at_customer=0))
        self.assertEqual((weak.range_low, weak.range_high, weak.level), (0, 35, "low"))

    def test_text_softens_code_mismatch_from_threshold(self):
        t = chance.DEFAULT_RULES.soften_by_text
        at = chance.estimate(chance.ChanceInput(area_level="none", text_match=t))
        below = chance.estimate(chance.ChanceInput(area_level="none", text_match=t - 0.01))
        self.assertEqual(at.factors[0].points, 0)  # порог включительно
        self.assertEqual(below.factors[0].points, -25)
        stricter = chance.rules_from_dict({"soften_by_text": 0.9})
        self.assertEqual(chance.estimate(chance.ChanceInput(area_level="none", text_match=t), stricter).factors[0].points, -25)

    def test_penalty_for_unmet_requirements_is_capped(self):
        one, two, three = (chance.estimate(chance.ChanceInput(**self.CASE, blocking_gaps=n)) for n in (1, 2, 3))
        self.assertEqual(one.factors[-1].points, -25)
        self.assertEqual((two.factors[-1].points, three.factors[-1].points), (-50, -50))  # не больше двух
        wide = chance.rules_from_dict({"gap_max": 3})
        self.assertEqual(chance.estimate(chance.ChanceInput(**self.CASE, blocking_gaps=3), wide).factors[-1].points, -75)

    def test_override_changes_the_result(self):
        data = chance.ChanceInput(area_level="high", customer_contracts=10, company_wins_at_customer=0)
        base = chance.estimate(data)
        softer = chance.estimate(data, chance.rules_from_dict({"wins_none": 0}))
        self.assertEqual(softer.range_low - base.range_low, 5)
        self.assertEqual(chance.estimate(data, chance.DEFAULT_RULES).range_low, base.range_low)
        # область может быть переопределена по одному уровню, остальные сохраняются
        rules = chance.rules_from_dict({"area": {"none": -10}})
        self.assertEqual(dict(rules.area), {"high": 15, "medium": 5, "low": -10, "none": -10})
        self.assertEqual(dict(chance.DEFAULT_RULES.area)["none"], -25)  # исходный набор не тронут

    def test_validation(self):
        bad = [({"нет_такого": 1}, "Неизвестный параметр"), ({"base": "много"}, "нужно число"),
               ({"base": True}, "нужно число"), ({"wins_none": 1.5}, "целое"),
               ({"unknown_points": [1, 2]}, "список из 3"), ({"area": {"huge": 1}}, "area"),
               ({"area": [1]}, "area"), ({"name": "  "}, "строка")]
        for changes, text in bad:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, text):
                    chance.rules_from_dict(changes)
        ok = chance.rules_from_dict({"soften_by_text": 1, "unknown_points": [3, 2, 1], "name": " свой "})
        self.assertEqual((ok.soften_by_text, ok.unknown_points, ok.name), (1.0, (3, 2, 1), "свой"))
        self.assertIsInstance(ok.soften_by_text, float)


class Comparable(unittest.TestCase):
    def test_boundary(self):
        t = limits.COMPARABLE_TEXT_MATCH
        self.assertTrue(backtest.is_comparable("none", t))  # порог включительно
        self.assertFalse(backtest.is_comparable("none", t - 0.01))
        self.assertTrue(backtest.is_comparable("high", 0.0))
        self.assertTrue(backtest.is_comparable("medium", 0.0))
        self.assertFalse(backtest.is_comparable("low", 0.0))
        self.assertFalse(backtest.is_comparable("unknown", 0.0))


class Interval(unittest.TestCase):
    def test_perfect_and_empty(self):
        s = backtest.separation_with_interval([9, 8, 7], [1, 2, 3, 4])
        self.assertEqual((s.value, s.low, s.high, s.positives, s.negatives), (1.0, 1.0, 1.0, 3, 4))
        none = backtest.separation_with_interval([], [1])
        self.assertEqual((none.value, none.low, none.high, none.positives, none.negatives), (None, None, None, 0, 1))

    def test_repeatable_and_ordered(self):
        won, other = [5, 6, 9, 3, 7, 8, 2, 6], [1, 4, 6, 5, 3, 7, 2, 4, 5, 8]
        a, b = backtest.separation_with_interval(won, other), backtest.separation_with_interval(won, other)
        self.assertEqual(a, b)  # зерно фиксировано
        # значения закреплены: порча пересэмплирования или границ интервала изменит их
        self.assertEqual((a.low, a.value, a.high, a.positives, a.negatives), (0.388, 0.662, 0.894, 8, 10))
        self.assertLessEqual(a.low, a.value)
        self.assertLessEqual(a.value, a.high)
        self.assertGreater(a.high - a.low, 0.1)  # на малой выборке интервал широкий

    def test_wider_with_fewer_observations(self):
        big = backtest.separation_with_interval([i % 7 + 2 for i in range(60)], [i % 7 for i in range(60)])
        small = backtest.separation_with_interval([i % 7 + 2 for i in range(6)], [i % 7 for i in range(6)])
        self.assertGreater(small.high - small.low, big.high - big.low)


class Verdict(unittest.TestCase):
    def sep(self, value, low, high, pos=20, neg=40):
        return backtest.Separation(value, low, high, pos, neg)

    def test_cases(self):
        v = backtest.verdict
        self.assertEqual(v(self.sep(0.8, 0.7, 0.9)), backtest.WORKS)
        self.assertEqual(v(self.sep(0.2, 0.1, 0.3)), backtest.INVERTED)
        self.assertEqual(v(self.sep(0.55, 0.4, 0.7)), backtest.INDISTINGUISHABLE)  # интервал захватывает 0,5
        self.assertEqual(v(self.sep(0.504, 0.501, 0.508)), backtest.INDISTINGUISHABLE)  # узкий интервал вокруг пустяка
        self.assertEqual(v(self.sep(0.496, 0.492, 0.499)), backtest.INDISTINGUISHABLE)
        self.assertEqual(v(self.sep(0.8, 0.7, 0.9, pos=9)), backtest.TOO_FEW)
        self.assertEqual(v(self.sep(0.8, 0.7, 0.9, neg=9)), backtest.TOO_FEW)
        self.assertEqual(v(self.sep(None, None, None, 0, 0)), backtest.TOO_FEW)
        self.assertEqual(v(self.sep(0.8, 0.7, 0.9, pos=10, neg=10)), backtest.WORKS)  # граница включительно


def history_rows(n_telecom_others=12):
    """Компания выигрывает связь у заказчика A; у него же чужие контракты: связь и дороги."""
    own = [contract(f"o{i:02}", f"2026-01-{i + 1:02}", "A", tender=f"T{i}") for i in range(25)]
    others = [contract(f"x{i:02}", f"2026-02-{i + 1:02}", "A", tender=f"X{i}") for i in range(n_telecom_others)]
    others += [contract(f"r{i:02}", f"2026-02-{i + 1:02}", "A", code="42.11.20.300", title="Ремонт дороги", tender=f"R{i}")
               for i in range(12)]
    return own, others


class EvaluateNew(unittest.TestCase):
    def test_comparable_and_stamp(self):
        own, others = history_rows()
        extra = own + [contract("w1", "2026-03-01", "A", tender="TW1"), contract("w2", "2026-03-02", "A", tender="TW2"),
                       contract("w3", "2026-03-03", "A", code="42.11.20.300", title="Ремонт дороги", tender="TW3")]
        report, rows = backtest.evaluate(US, extra, {"A": others}, min_history=20)
        by = {r.contract_regn: r for r in rows}
        self.assertTrue(by["x00"].comparable)  # связь, как у компании
        self.assertFalse(by["r00"].comparable)  # дорога: другой код и название
        self.assertEqual(sum(r.comparable and not r.is_company for r in rows), 12)
        # среди похожих учтены только похожие выигранные и чужие
        s = report.separation_comparable
        self.assertEqual(s.negatives, 12)
        self.assertEqual(s.positives, sum(r.comparable and r.is_company and r.customer_inn == "A" for r in rows))
        self.assertEqual(report.separation.negatives, 24)
        # все признаки видны в строках
        self.assertGreater(by["x00"].text_match, 0)
        self.assertEqual(by["x00"].area_level, "high")
        self.assertEqual(report.profile.contracts, len(extra))
        self.assertEqual((report.profile.period_from, report.profile.period_to), ("2026-01-01", "2026-03-03"))
        self.assertEqual(report.rules.name, "текущие")
        self.assertEqual([c.factor for c in report.factor_checks],
                         ["область по кодам ОКПД2", "сходство названия с контрактами компании", "победы у заказчика раньше",
                          "доля закупок заказчика без снижения цены", "размер закупки: не крупнее обычного"])
        self.assertTrue(all(c.comparable.negatives == 12 for c in report.factor_checks))
        # выигранная закупка не из области компании (w3) входит во все закупки, но не в «похожие»
        self.assertTrue(all(c.overall.positives == c.comparable.positives + 1 for c in report.factor_checks))
        self.assertEqual(report.separation.positives, report.separation_comparable.positives + 1)
        self.assertTrue(any("независимыми" in t for t in report.limitations))

    def test_fingerprint(self):
        own, others = history_rows()
        a = backtest.evaluate(US, own, {"A": others}, 20)[0].profile
        b = backtest.evaluate(US, list(reversed(own)), {"A": others}, 20)[0].profile
        c = backtest.evaluate(US, own + [contract("new", "2026-03-05", "A", tender="TN")], {"A": others}, 20)[0].profile
        self.assertEqual(a.fingerprint, b.fingerprint)  # порядок контрактов не важен
        self.assertNotEqual(a.fingerprint, c.fingerprint)  # новый контракт меняет отпечаток
        self.assertEqual((a.contracts, c.contracts), (25, 26))

    def test_rules_are_used_and_recorded(self):
        own, others = history_rows()
        base, rows = backtest.evaluate(US, own, {"A": others}, 20)
        low_rules = chance.rules_from_dict({"base": -50, "name": "всё низко"})
        report, rated = backtest.evaluate(US, own, {"A": others}, 20, low_rules)
        self.assertEqual(report.rules.name, "всё низко")
        self.assertEqual(report.won_rated_low_share, 1.0)
        self.assertNotEqual(base.won_rated_low_share, 1.0)
        self.assertEqual({r.contract_regn for r in rows}, {r.contract_regn for r in rated})  # те же закупки


class Compare(unittest.TestCase):
    def test_same_data_different_rules(self):
        own, others = history_rows()
        rules = {"текущие правила": chance.DEFAULT_RULES, "всё низко": chance.rules_from_dict({"base": -50}),
                 "всё высоко": chance.rules_from_dict({"base": 90, "ceiling": 95})}
        rows = backtest.compare(US, own, {"A": others}, 20, rules)
        self.assertEqual([r.name for r in rows], list(rules))
        report, _ = backtest.evaluate(US, own, {"A": others}, 20)
        self.assertEqual(rows[0].separation, report.separation)  # то же, что даёт проверка
        self.assertEqual(rows[0].separation_comparable, report.separation_comparable)
        self.assertEqual((rows[0].won, rows[0].other), (5, 24))  # оцениваются победы после первых 20 контрактов
        self.assertEqual(rows[1].won_rated_low_share, 1.0)
        self.assertEqual(rows[2].other_rated_high_share, 1.0)
        self.assertEqual(rows[2].won_rated_low_share, 0.0)


class CompareStep(unittest.TestCase):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.dir)
        self.urls = []
        rows = [(f"9{i:02}", f"2026-01-{i + 1:02}", "A" if i % 2 else "B", 90, "61.10.11.120",
                 "Услуги связи", f"T{i}", 100) for i in range(24)]
        company = contracts_of(US, rows)
        history = {inn: contracts_of(inn, [
            (f"{inn}1", "2026-02-10", inn, 50, "19.20.21.325", "Поставка топлива", f"H{inn}1", 100),
            (f"{inn}2", "2026-02-11", inn, 99, "61.10.11.120", "Услуги связи", f"H{inn}2", 100),
        ]) for inn in "AB"}

        def http(url, timeout):
            self.urls.append(url)
            inn = url.split("inn=")[1].split("&")[0]
            return json.dumps(company if inn == US else history[inn])

        self.client = DamiaClient(self.dir, lambda: KEY, http)
        backtest_step.run(self.client, self.store, US, network=True)
        self.urls.clear()

    def test_never_calls_the_api(self):
        result = backtest_step.compare(self.client, US, {"мягче": {"wins_none": 0}})
        self.assertEqual(self.urls, [])
        self.assertEqual([r.name for r in result.rows], ["текущие правила", "мягче"])
        self.assertEqual(result.rules["мягче"].wins_none, 0)
        self.assertEqual(result.rules["текущие правила"].wins_none, -5)
        self.assertEqual(result.profile.contracts, 24)
        self.assertTrue(any("не выполняются" in t for t in result.limitations))

    def test_does_not_spend_even_for_missing_customers(self):
        fresh = DamiaClient(self.dir / "empty", lambda: KEY, lambda url, timeout: self.urls.append(url) or "{}")
        with self.assertRaises(steps.NotCached):
            backtest_step.compare(fresh, US, {})
        self.assertEqual(self.urls, [])

    def test_exactly_the_limit_of_variants_is_allowed(self):
        result = backtest_step.compare(self.client, US, {f"v{i}": {} for i in range(limits.COMPARE_MAX_VARIANTS)})
        self.assertEqual(len(result.rows), limits.COMPARE_MAX_VARIANTS + 1)  # плюс текущие правила

    def test_bad_input(self):
        with self.assertRaisesRegex(ValueError, "Неизвестный параметр"):
            backtest_step.compare(self.client, US, {"плохой": {"нет_такого": 1}})
        with self.assertRaisesRegex(ValueError, "уникальным"):
            backtest_step.compare(self.client, US, {"текущие правила": {}})
        with self.assertRaisesRegex(ValueError, "уникальным"):
            backtest_step.compare(self.client, US, {" ": {}})
        with self.assertRaisesRegex(ValueError, "не больше"):
            backtest_step.compare(self.client, US, {f"v{i}": {} for i in range(limits.COMPARE_MAX_VARIANTS + 1)})

    def test_run_saves_new_fields(self):
        saved = self.store.load("backtest", BacktestFile)
        self.assertEqual(saved.report.profile.contracts, 24)
        self.assertEqual(saved.report.rules.name, "текущие")
        self.assertEqual(len(saved.report.factor_checks), 5)
        self.assertEqual(saved.items[0].text_match, saved.items[0].text_match)  # поле читается с диска

    def test_run_uses_given_rules(self):
        mine = chance.rules_from_dict({"name": "свои", "base": -50})
        backtest_step.run(self.client, self.store, US, rules=mine)
        saved = self.store.load("backtest", BacktestFile)
        self.assertEqual((saved.report.rules.name, saved.report.rules.base), ("свои", -50))
        self.assertEqual(saved.report.won_rated_low_share, 1.0)

    @mock.patch.dict(os.environ, {"COMPANY_INN": US})
    def test_route_turns_bad_input_into_422(self):
        body = routes.CompareRules(variants={"плохой": {"нет_такого": 1}})
        with self.assertRaises(HTTPException) as caught:
            routes.backtest_compare(body, self.client)
        self.assertEqual(caught.exception.status_code, 422)
        good = routes.backtest_compare(routes.CompareRules(variants={"мягче": {"wins_none": 0}}), self.client)
        self.assertEqual(len(good.rows), 2)


if __name__ == "__main__":
    unittest.main()

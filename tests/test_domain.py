import unittest

from app import limits
from app.domain import candidates, chance, customer_history, okpd, prices, profile, quotes
from app.domain.models import Contract, TenderBrief

US = "7700000001"


def contract(regn, price, tender_price, *, tender="", customer="5400000001", code="61.20.11.000",
             title="Оказание услуг подвижной связи", suppliers=(US,), date="2026-05-01"):
    return Contract(regn, date, customer, suppliers, price, code, title,
                    tender or (f"T{regn}" if tender_price else ""), tender_price)


class Prices(unittest.TestCase):
    def test_reduction(self):
        self.assertAlmostEqual(prices.reduction(80, 100), 0.2)
        self.assertEqual(prices.reduction(100, 100), 0.0)

    def test_reduction_unknown(self):
        self.assertIsNone(prices.reduction(80, 0))
        self.assertIsNone(prices.reduction(120, 100))  # контракт дороже закупки

    def test_contracts_of_one_tender_are_summed(self):
        values, skipped = prices.tender_reductions([
            contract("1", 30, 100, tender="A"), contract("2", 50, 100, tender="A"),
            contract("3", 10, 0),
        ])
        self.assertEqual(skipped, 0)
        self.assertAlmostEqual(values["A"], 0.2)
        self.assertEqual(len(values), 1)

    def test_stats(self):
        stats = prices.reduction_stats([0.0, 0.005, 0.2, 0.6, 0.995])
        self.assertEqual(stats.observations, 5)
        self.assertAlmostEqual(stats.median, 0.2)
        self.assertAlmostEqual(stats.no_reduction_share, 0.4)
        self.assertAlmostEqual(stats.deep_share, 0.4)

    def test_estimate_needs_history(self):
        few = prices.reduction_stats([0.1] * (limits.MIN_PRICE_OBSERVATIONS - 1))
        result = prices.estimate_price(1000, few)
        self.assertIsNone(result.expected_price)
        self.assertEqual(result.method, "insufficient_history")

    def test_estimate_and_margin(self):
        stats = prices.reduction_stats([0.0, 0.1, 0.2, 0.3, 0.4])
        result = prices.estimate_price(1000, stats, cost=600)
        self.assertEqual(result.expected_price, 800)
        self.assertEqual((result.low_price, result.high_price), (700, 900))
        self.assertEqual(result.margin, 200)
        self.assertAlmostEqual(result.margin_share, 0.25)

    def test_no_margin_without_cost(self):
        stats = prices.reduction_stats([0.0, 0.1, 0.2, 0.3, 0.4])
        result = prices.estimate_price(1000, stats)
        self.assertIsNone(result.margin)
        self.assertIn("себестоимость не задана", result.note)


class Okpd(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(okpd.normalize(" 61.20.11.000 "), "61.20.11.000")
        self.assertEqual(okpd.normalize("услуги"), "")

    def test_levels(self):
        mine = {"61.20.11.000": 14, "61.10.11.120": 5, "43.21.10.120": 1}
        self.assertEqual(okpd.match_area(["61.20.11.000"], mine).level, "high")
        self.assertEqual(okpd.match_area(["61.20.30.120"], mine).level, "high")  # группа, 70% профиля
        self.assertEqual(okpd.match_area(["43.21.90.000"], mine).level, "medium")  # группа, 5% профиля
        self.assertEqual(okpd.match_area(["61.90.10.160"], mine).level, "low")
        self.assertEqual(okpd.match_area(["63.99.10.000"], mine).level, "none")
        self.assertEqual(okpd.match_area([""], mine).level, "unknown")

    def test_text_match(self):
        terms = okpd.title_terms(["Оказание услуг подвижной связи", "Услуги подвижной связи",
                                  "Доступ в Интернет"], top=10)
        self.assertGreater(okpd.text_match("Услуги сотовой связи", terms), 0)
        self.assertEqual(okpd.text_match("Поставка картофеля", terms), 0.0)


class Quotes(unittest.TestCase):
    DOC = "Участник должен иметь\nлицензию  ФСБ – на «шифровальные» средства."

    def test_survives_spaces_quotes_dashes(self):
        self.assertTrue(quotes.contains(self.DOC, 'иметь лицензию ФСБ - на "шифровальные" средства'))

    def test_rejects_invented(self):
        self.assertFalse(quotes.contains(self.DOC, "иметь лицензию ФСТЭК на шифровальные средства"))

    def test_rejects_short(self):
        self.assertFalse(quotes.contains(self.DOC, "лицензию"))

    LONG = ("Подать заявку вправе только соответствующий пункту 4 части 1 статьи 3 закона "
            "зарегистрированный в единой информационной системе участник закупки. "
            + "Прочие условия. " * 200 + "Обеспечение исполнения контракта составляет 30 процентов.")

    def test_quote_with_gap(self):
        self.assertTrue(quotes.contains(
            self.LONG, "Подать заявку вправе только ... зарегистрированный в единой информационной системе"))
        self.assertTrue(quotes.contains(
            self.LONG, "Подать заявку вправе только […] участник закупки."))

    def test_gap_does_not_hide_invention(self):
        # второй фрагмент выдуман
        self.assertFalse(quotes.contains(
            self.LONG, "Подать заявку вправе только ... аккредитованный на площадке участник"))
        # фрагменты настоящие, но в обратном порядке
        self.assertFalse(quotes.contains(
            self.LONG, "зарегистрированный в единой информационной системе ... Подать заявку вправе только"))
        # фрагменты настоящие, но из разных концов документа
        self.assertFalse(quotes.contains(
            self.LONG, "Подать заявку вправе только ... Обеспечение исполнения контракта составляет"))
        # за пропуском прячется слишком короткий фрагмент
        self.assertFalse(quotes.contains(self.LONG, "Подать заявку вправе только ... закона"))

    def test_dropped_are_returned_with_reason(self):
        items = [("a", "должен иметь лицензию ФСБ", "doc:1"),
                 ("b", "опыт не менее пяти лет", "doc:1"),
                 ("c", "должен иметь лицензию ФСБ", "doc:9")]
        kept, dropped = quotes.split_by_quote(items, {"doc:1": self.DOC},
                                              quote_of=lambda i: i[1], doc_of=lambda i: i[2])
        self.assertEqual([i[0] for i in kept], ["a"])
        self.assertEqual([d.item[0] for d in dropped], ["b", "c"])
        self.assertIn("нет в указанном документе", dropped[0].reason)
        self.assertIn("не найден", dropped[1].reason)


class Profile(unittest.TestCase):
    def setUp(self):
        self.contracts = [
            contract("1", 100, 100), contract("2", 80, 100), contract("3", 50, 0, code=""),
            contract("4", 10, 100, customer="7800000002", code="61.10.11.120"),
        ]

    def test_profile(self):
        p = profile.build_profile(US, self.contracts)
        self.assertEqual(p.contracts, 4)
        self.assertEqual(p.okpd_missing, 1)
        self.assertEqual(p.okpd_codes[0].code, "61.20.11.000")
        self.assertAlmostEqual(p.okpd_codes[0].share, 2 / 3)
        self.assertEqual(p.customers[0].inn, "5400000001")
        self.assertEqual(p.customers[0].contracts, 3)
        self.assertEqual(p.reductions.observations, 3)
        self.assertEqual(p.contracts_with_tender, 3)
        self.assertTrue(any("нет кода ОКПД2" in text for text in p.limitations))

    def test_empty(self):
        p = profile.build_profile(US, [])
        self.assertEqual(p.contracts, 0)
        self.assertTrue(p.limitations)


class History(unittest.TestCase):
    CUSTOMER = "5400000001"

    def rows(self):
        other = dict(suppliers=(), customer=self.CUSTOMER)
        return [
            contract("2", 100, 100, code="63.99.10.190", title="Фиксация нарушений", date="2026-02-01", **other),
            contract("1", 90, 100, code="63.99.10.190", title="Фиксация нарушений", date="2026-03-01", **other),
            contract("3", 50, 100, code="19.20.21.325", title="Поставка дизельного топлива", **other),
            contract("4", 880, 1000, tender="THIS", code="63.99.10.000", title="Фиксация нарушений", **other),
            contract("5", 1, 100, customer="7800000002", code="63.99.10.190", suppliers=()),
        ]

    def summary(self, **kwargs):
        return customer_history.summarize(self.CUSTOMER, self.rows(), US, tender_codes=["63.99.10.000"],
                                          tender_title="Услуги фиксации нарушений", exclude_tender="THIS",
                                          **kwargs)

    def test_similar_contracts_and_own_tender_excluded(self):
        h = self.summary()
        self.assertEqual(h.contracts, 3)  # чужой заказчик и сама закупка не входят
        self.assertEqual(h.relevant_contracts, 2)
        self.assertEqual([e.contract_regn for e in h.examples], ["1", "2"])  # свежие вперёд
        self.assertEqual(h.relevant_reductions.observations, 2)
        self.assertEqual(h.reductions.observations, 3)
        self.assertEqual((h.suppliers_known_for, h.winners), (0, []))
        self.assertEqual(customer_history.lookup_candidates(h, 1), ["1"])

    def test_winners_from_details(self):
        rival = "5000000009"
        known = contract("1", 90, 0, suppliers=(rival,), customer=self.CUSTOMER)
        known = known.__class__(**{**known.__dict__, "supplier_names": ("ПАО Соперник",)})
        h = self.summary(details={"1": known})
        self.assertEqual(h.suppliers_known_for, 1)
        self.assertEqual((h.winners[0].inn, h.winners[0].name, h.winners[0].is_company),
                         (rival, "ПАО Соперник", False))
        self.assertEqual(customer_history.lookup_candidates(h, 5), ["2"])
        self.assertTrue(any("известен у 1 из 2" in t for t in h.limitations))

    def test_company_wins_come_from_its_own_contracts(self):
        mine = [contract("2", 100, 100, customer=self.CUSTOMER),
                contract("4", 880, 1000, tender="THIS", customer=self.CUSTOMER),
                contract("9", 5, 5, customer="7800000002")]
        h = self.summary(company_contracts=mine)
        self.assertEqual(h.company_wins, 1)  # контракт самой закупки не считается
        self.assertTrue(h.winners[0].is_company)

    def test_no_history(self):
        h = customer_history.summarize("1", [], US)
        self.assertEqual(h.contracts, 0)
        self.assertTrue(h.limitations)


class Chance(unittest.TestCase):
    def test_strong_case(self):
        result = chance.estimate(chance.ChanceInput(
            area_level="high", customer_contracts=10, company_wins_at_customer=4,
            average_bids=1.0, documents_read=True))
        self.assertEqual(result.level, "high")
        self.assertEqual(result.confidence, "medium")  # выше не ставится без проверки на истории
        self.assertLess(result.range_low, result.range_high)

    def test_blocking_gap_lowers(self):
        base = dict(area_level="high", customer_contracts=10, company_wins_at_customer=4,
                    average_bids=1.0, documents_read=True)
        ok = chance.estimate(chance.ChanceInput(**base))
        gap = chance.estimate(chance.ChanceInput(**base, blocking_gaps=1))
        self.assertLess(gap.range_high, ok.range_high)
        self.assertNotEqual(gap.level, "high")

    def test_unknown_requirements_are_not_penalized(self):
        base = dict(area_level="high", customer_contracts=10, company_wins_at_customer=4)
        plain = chance.estimate(chance.ChanceInput(**base))
        asked = chance.estimate(chance.ChanceInput(**base, open_questions=30))
        self.assertEqual((asked.range_low, asked.range_high), (plain.range_low, plain.range_high))
        self.assertTrue(any("проверить внутри компании: 30" in f.explanation for f in asked.factors))

    def test_no_data_is_unknown(self):
        result = chance.estimate(chance.ChanceInput(area_level="unknown"))
        self.assertEqual(result.level, "unknown")
        self.assertIsNone(result.range_low)

    def test_text_softens_code_mismatch(self):
        strict = chance.estimate(chance.ChanceInput(area_level="none", text_match=0.0))
        soft = chance.estimate(chance.ChanceInput(area_level="none", text_match=0.8))
        self.assertGreater(soft.range_high, strict.range_high)

    def test_always_says_it_is_not_probability(self):
        result = chance.estimate(chance.ChanceInput(area_level="high"))
        self.assertTrue(any("не является статистической вероятностью" in t for t in result.limitations))


class Candidates(unittest.TestCase):
    def test_rank(self):
        p = profile.build_profile(US, [contract("1", 100, 100), contract("2", 500, 500)])

        def brief(regn, title, deadline, status="", customer="9900000000", price=200.0):
            return TenderBrief(regn, "44", title, customer, "Заказчик", price, "2026-11-01",
                               deadline, status)

        ranked = candidates.rank([
            brief("1", "Поставка картофеля", "2026-11-20"),
            brief("2", "Услуги подвижной связи", "2026-11-20"),
            brief("3", "Услуги подвижной связи", "2026-11-01"),
            brief("4", "Услуги подвижной связи", "2026-11-20", status="Закупка отменена"),
            brief("5", "Поставка картофеля", "2026-11-20", customer="5400000001", price=900.0),
        ], p, today="2026-11-14")
        self.assertEqual([c.regn for c in ranked[:3]], ["5", "2", "1"])
        self.assertEqual({c.regn for c in ranked if not c.is_open}, {"3", "4"})
        self.assertEqual(ranked[0].size_fit, "large")
        self.assertTrue(ranked[0].known_customer)


if __name__ == "__main__":
    unittest.main()

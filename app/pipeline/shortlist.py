"""Подборка закупок: поиски по словам из профиля, склейка, порядок и проверка на прошлых закупках.

Бюджет запросов жёсткий: не больше limits.SHORTLIST_QUERIES поисков за запуск, и только первая
страница каждого. Ответ, уже лежащий на диске, запрос не тратит. Без разрешения network=True
программа читает только сохранённое.
"""
import time
from datetime import date, timedelta

from app import limits
from app.domain import backtest as backtest_domain
from app.domain import candidates, okpd, shortlist as shortlist_domain
from app.domain.models import TenderBrief
from app.pipeline import backtest as backtest_pipeline
from app.pipeline import steps
from app.schemas.card import PlanLine, ProfileFile, ShortlistFile, ShortlistPlan
from app.sources import damia_parse
from app.sources.damia import DamiaClient, DamiaError, QuotaExceeded
from app.storage.files import Store


def _from_date(today: str) -> str:
    return (date.fromisoformat(today) - timedelta(days=limits.SHORTLIST_DAYS)).isoformat()


def _words(client: DamiaClient, inn: str, count: int) -> list[str]:
    """Слова запроса из названий контрактов компании. Контракты берутся только с диска."""
    own, _, _ = steps.company_contracts(client, inn, network=False, max_pages=limits.PROFILE_MAX_PAGES,
                                        saved_only_after_first=True)
    return okpd.title_words((c.title for c in own), max(0, min(count, limits.SHORTLIST_QUERIES)))


def plan(client: DamiaClient, inn: str, *, today: str = "", queries: int = limits.SHORTLIST_QUERIES
         ) -> ShortlistPlan:
    """Что сделает запуск и сколько запросов потратит. Сеть не используется."""
    today = today or date.today().isoformat()
    from_date = _from_date(today)
    words = _words(client, inn, queries)
    lines = [PlanLine(word=w, cached=client.cached("zsearch", q=w, from_date=from_date) is not None)
             for w in words]
    left = next((q.left for q in client.quota() if q.method == "zsearch"), 0)
    return ShortlistPlan(from_date=from_date, words=lines, to_spend=sum(not line.cached for line in lines),
                         quota_left=left, budget=min(queries, limits.SHORTLIST_QUERIES))


def _quality(client: DamiaClient, inn: str):
    """Проверка балла на сохранённых прошлых закупках. К API не обращается."""
    got = backtest_pipeline.gather(client, inn, network=False)
    collected = backtest_domain.collect(inn, got.own, got.histories, limits.BACKTEST_MIN_HISTORY)
    return shortlist_domain.check(collected.features, collected.profile)


def run(client: DamiaClient, store: Store, inn: str, *, network: bool = False, today: str = "",
        queries: int = limits.SHORTLIST_QUERIES) -> ShortlistFile:
    saved = store.load("profile", ProfileFile)
    if saved is None:
        raise steps.NotCached("Сначала постройте профиль компании.")
    today = today or date.today().isoformat()
    from_date = _from_date(today)
    words = _words(client, inn, queries)
    if not words:
        raise steps.NotCached("В названиях контрактов компании нет слов для поиска: профиль слишком мал.")

    notes: list[str] = []
    lines: list[shortlist_domain.QueryLine] = []
    tenders: dict[str, TenderBrief] = {}
    found_by: dict[str, list[str]] = {}
    spent = not_saved = 0
    failure: Exception | None = None
    for word in words:
        reply = client.cached("zsearch", q=word, from_date=from_date)
        from_cache = reply is not None
        if reply is None:
            if not network:
                not_saved += 1
                continue
            try:
                reply = client.call("zsearch", q=word, from_date=from_date)
            except (QuotaExceeded, DamiaError) as e:
                failure = e
                notes.append(f"Поиск остановлен на слове «{word}»: {e}")
                break
            spent += 1
        found, _ = damia_parse.parse_search(reply.data)
        lines.append(shortlist_domain.QueryLine(word, len(found), from_cache, damia_parse.has_next_page(reply.data)))
        for t in found:
            tenders.setdefault(t.regn, t)
            found_by.setdefault(t.regn, []).append(word)

    if not lines:
        if failure is not None:
            raise failure
        raise steps.NotCached(
            "Ни одного поиска подборки нет среди сохранённых. Повторите с разрешением на запросы к API: "
            f"он потратит до {len(words)} запросов метода zsearch.")

    ranked = candidates.rank(list(tenders.values()), saved.profile, today)
    items = shortlist_domain.pick(ranked, saved.profile, found_by)
    open_total = sum(c.is_open for c in ranked)

    notes += [
        "Порядок предварительный: в результатах поиска нет кода ОКПД2, балл считается по словам названия, "
        "опыту компании у заказчика и размеру закупки. Это не вероятность победы.",
        f"Искались закупки, опубликованные с {from_date} (последние {limits.SHORTLIST_DAYS} дней); "
        "открытой считается закупка, срок подачи заявок по которой не раньше даты запуска и которая не отменена.",
        f"В подборке не больше {limits.SHORTLIST_TOP} закупок с открытым приёмом заявок "
        f"(скрыто {max(0, open_total - len(items))}).",
        "Опыт компании у заказчика известен только по главным заказчикам профиля "
        f"(до {limits.TOP_CUSTOMERS} по числу контрактов).",
    ]
    if any(line.more_pages for line in lines):
        n = sum(line.more_pages for line in lines)
        notes.append(f"У {n} из {len(lines)} поисков есть следующая страница: прочитана только первая.")
    if not_saved:
        notes.append(f"Слов, поиск по которым пропущен (ответа нет на диске, запрос не разрешён): {not_saved}.")
    quality = _quality(client, inn)
    if quality is None:
        notes.append("Проверить балл на прошлых закупках не удалось: сохранённых данных для сравнения нет.")

    result = ShortlistFile(
        generated_at=steps._now(), from_date=from_date, queries=lines, requests_spent=spent,
        found_total=len(tenders), open_total=open_total, items=items, quality=quality, limitations=notes)
    store.save("shortlist", result)
    return result

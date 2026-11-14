"""Сценарии первого этапа: профиль компании и список кандидатов.

Параметр network: False — работать только с уже сохранёнными ответами и квоту не тратить.
"""
import time
from datetime import date

from app import limits
from app.domain import candidates, profile as profile_domain
from app.domain.models import Contract
from app.schemas.card import ProfileFile, Source, TendersFile
from app.sources import damia_parse
from app.sources.damia import DamiaClient, Reply
from app.storage.files import Store


class NotCached(RuntimeError):
    """Нужного ответа нет среди сохранённых, а обращаться к сети не разрешено."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _get(client: DamiaClient, network: bool, method: str, **params: str) -> Reply:
    if network:
        return client.call(method, **params)
    reply = client.cached(method, **params)
    if reply is None:
        raise NotCached(
            f"Ответа метода {method} с такими параметрами нет среди сохранённых. "
            "Повторите с разрешением на запрос к API: он потратит один запрос квоты."
        )
    return reply


def build_profile(client: DamiaClient, store: Store, inn: str, *, network: bool = False,
                  max_pages: int = 1) -> ProfileFile:
    """Профиль из контрактов компании как поставщика (44-ФЗ)."""
    pages = max(1, min(max_pages, limits.PROFILE_MAX_PAGES))
    contracts: list[Contract] = []
    sources: list[Source] = []
    notes: list[str] = []
    bad_total = 0
    more = False
    for page in range(1, pages + 1):
        params = {"inn": inn, "fz": "44", "role": "0", "format": "2"}
        if page > 1:
            params["page"] = str(page)
        reply = _get(client, network, "contracts", **params)
        chunk, bad = damia_parse.parse_contracts(reply.data, inn, "0")
        contracts += chunk
        bad_total += bad
        sources.append(Source(
            id=f"contracts:{inn}:{page}", kind="contract",
            title=f"Контракты компании как поставщика, 44-ФЗ, страница {page}",
            used_for=["profile"],
            comment=f"Получено {reply.fetched_at}, записей: {len(chunk)}.",
        ))
        more = damia_parse.has_next_page(reply.data)
        if not chunk or not more:
            break
    if more:
        notes.append(
            f"Прочитано страниц контрактов: {len(sources)} (предел {limits.PROFILE_MAX_PAGES}). "
            "У компании есть более ранние контракты, в профиль они не вошли."
        )
    if bad_total:
        notes.append(f"Записей, которые не удалось разобрать: {bad_total}.")
    notes.append("Учтены только контракты по 44-ФЗ.")

    seen: dict[str, Contract] = {c.regn: c for c in contracts}
    built = profile_domain.build_profile(inn, list(seen.values()))
    built.limitations.extend(notes)
    result = ProfileFile(generated_at=_now(), profile=built, sources=sources)
    store.save("profile", result)
    return result


def search_tenders(client: DamiaClient, store: Store, query: str, from_date: str, *,
                   network: bool = False, today: str = "") -> TendersFile:
    """Кандидаты по запросу. Нужен построенный профиль."""
    saved = store.load("profile", ProfileFile)
    if saved is None:
        raise NotCached("Сначала постройте профиль компании.")
    reply = _get(client, network, "zsearch", q=query, from_date=from_date)
    tenders, by_law = damia_parse.parse_search(reply.data)
    today = today or date.today().isoformat()
    items = candidates.rank(tenders, saved.profile, today)
    notes = [
        "Порядок предварительный: в результатах поиска нет кода ОКПД2, сходство посчитано "
        "по словам названия.",
        "Открытой считается закупка, срок подачи заявок по которой не раньше "
        f"{today} и которая не отменена.",
    ]
    if damia_parse.has_next_page(reply.data):
        notes.append("Прочитана только первая страница результатов поиска: есть ещё.")
    result = TendersFile(
        generated_at=_now(), query=query, from_date=from_date, total=len(items),
        open_total=sum(c.is_open for c in items), by_law=by_law, items=items, limitations=notes,
    )
    store.save("tenders", result)
    return result

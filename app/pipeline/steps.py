"""Сценарии первого этапа: профиль компании и список кандидатов.

Параметр network: False — работать только с уже сохранёнными ответами и квоту не тратить.
"""
import time
from datetime import date

from app import limits
from app.domain import candidates, documents as documents_domain, profile as profile_domain
from app.domain.models import Contract, TenderCard
from app.schemas.card import (DocumentsFile, DocumentState, ModelCheck, ProfileFile,
                              SkippedDocument, Source, TendersFile)
from app.sources import damia_parse
from app.sources.damia import DamiaClient, Reply
from app.sources.documents import Downloader, DownloadRefused
from app.sources.llm import LlmClient
from app.storage.files import Store


class NotCached(RuntimeError):
    """Нужного ответа нет среди сохранённых, а обращаться к сети не разрешено."""


class NotFound(RuntimeError):
    pass


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


def company_contracts(client: DamiaClient, inn: str, *, network: bool = False, max_pages: int = 1,
                      saved_only_after_first: bool = False
                      ) -> tuple[list[Contract], list[Source], list[str]]:
    """Контракты компании как поставщика (44-ФЗ), свежие вперёд, по страницам.

    saved_only_after_first: если следующей страницы нет на диске, а запрос не разрешён,
    остановиться на прочитанном, а не отказывать.
    """
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
        try:
            reply = _get(client, network, "contracts", **params)
        except NotCached:
            if page == 1 or not saved_only_after_first:
                raise
            break
        chunk, bad = damia_parse.parse_contracts(reply.data, inn, "0")
        contracts += chunk
        bad_total += bad
        sources.append(Source(
            id=f"contracts:{inn}:{page}", kind="contract",
            title=f"Контракты компании как поставщика, 44-ФЗ, страница {page}",
            used_for=["profile"],
            comment=f"Получено {reply.fetched_at}, записей: {len(chunk)}.",
        ))
        more = damia_parse.contracts_page_full(reply.data, inn)
        if not chunk or not more:
            break
    if more:
        notes.append(
            f"Прочитано страниц контрактов: {len(sources)} (предел {limits.PROFILE_MAX_PAGES}). "
            "Последняя прочитанная страница заполнена целиком: у компании, вероятно, есть "
            "более ранние контракты, в профиль они не вошли."
        )
    if bad_total:
        notes.append(f"Записей, которые не удалось разобрать: {bad_total}.")
    notes.append("Учтены только контракты по 44-ФЗ.")
    return list({c.regn: c for c in contracts}.values()), sources, notes


def build_profile(client: DamiaClient, store: Store, inn: str, *, network: bool = False,
                  max_pages: int = 1) -> ProfileFile:
    """Профиль из контрактов компании как поставщика (44-ФЗ)."""
    contracts, sources, notes = company_contracts(client, inn, network=network, max_pages=max_pages)
    built = profile_domain.build_profile(inn, contracts)
    built.limitations.extend(notes)
    result = ProfileFile(generated_at=_now(), profile=built, sources=sources)
    store.save("profile", result)
    return result


def load_tender(client: DamiaClient, regn: str, *, network: bool = False) -> TenderCard:
    reply = _get(client, network, "zakupka", regn=regn, actual="1")
    card = damia_parse.parse_tender(reply.data, regn)
    if card is None:
        raise NotFound(f"Закупка {regn} в источнике данных не найдена.")
    return card


def tender_documents(client: DamiaClient, downloader: Downloader, regn: str, *,
                     network: bool = False, download: bool = False) -> DocumentsFile:
    """Файлы закупки, отобранные для чтения, и их состояние.

    network: разрешить запрос карточки закупки к API. download: разрешить скачивание файлов.
    """
    card = load_tender(client, regn, network=network)
    chosen, skipped = documents_domain.select(card.documents, limits.DOCS_PER_TENDER)
    items: list[DocumentState] = []
    for pick in chosen:
        doc, error = downloader.stored(regn, pick.doc.url), ""
        if doc is None and download:
            try:
                doc = downloader.fetch(regn, pick.doc.url, pick.doc.title, pick.role)
            except DownloadRefused as e:
                error = str(e)
        items.append(DocumentState(
            title=pick.doc.title, group=pick.doc.group, group_date=pick.doc.group_date,
            role=pick.role, role_label=documents_domain.ROLES[pick.role][1], url=pick.doc.url,
            stored=doc, error=error,
        ))
    notes = [f"По одной закупке читается не больше {limits.DOCS_PER_TENDER} файлов."]
    unread = [i for i in items if i.stored is not None and not i.stored.readable]
    if unread:
        notes.append(f"Файлов, из которых текст не получен: {len(unread)}. Причина указана у каждого.")
    waiting = sum(1 for i in items if i.stored is None)
    if waiting:
        notes.append(f"Файлов, которые ещё не скачаны: {waiting}.")
    return DocumentsFile(
        regn=regn, tender_title=card.title, items=items,
        skipped=[SkippedDocument(title=s.doc.title, group=s.doc.group, reason=s.reason)
                 for s in skipped],
        readable=sum(1 for i in items if i.stored and i.stored.readable),
        total_chars=sum(i.stored.chars for i in items if i.stored),
        limitations=notes,
    )


def check_model(llm: LlmClient) -> ModelCheck:
    """Короткий запрос к модели: работает ли ключ и умеет ли модель отвечать в JSON по-русски."""
    started = time.monotonic()
    reply = llm.ask_json(
        "Ты отвечаешь только объектом JSON.",
        'Верни JSON вида {"ответ": "..."}: одним словом, как называется столица России.',
    )  # без предела длины ответа: у рассуждающих моделей он съедается рассуждением
    answer = str(reply.data.get("ответ", ""))
    return ModelCheck(
        ok="моск" in answer.lower(), model=reply.model, answer=answer,
        prompt_tokens=reply.prompt_tokens, completion_tokens=reply.completion_tokens,
        attempts=reply.attempts, seconds=round(time.monotonic() - started, 2),
    )


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

"""Анализ одной закупки: от карточки и документов до карточки анализа.

Каждый вывод ссылается на источник из списка sources. Всё, чего сделать не удалось
(нет истории, файл не прочитан, модель не настроена), записывается в limitations.
"""
import time
from dataclasses import replace
from typing import Callable, Optional

from app import limits
from app.domain import chance as chance_domain
from app.domain import customer_history, findings, okpd, prices
from app.domain import profile as profile_domain
from app.domain.models import Contract, TenderCard
from app.pipeline import prompts, steps
from app.pipeline.reading import DocText, Reader, Usage
from app.schemas.card import (AnalysisCard, AnalysisStats, Attention, DroppedFinding, Hypothesis,
                              ProfileFile, Requirement, Source, TenderInfo)
from app.sources import damia_parse
from app.sources.damia import DamiaClient
from app.sources.documents import Downloader
from app.sources.llm import LlmClient, LlmError
from app.storage.files import Store
from app.storage.llm_cache import LlmCache

Progress = Callable[[str, int, int], None]
TENDER_URL = "https://zakupki.gov.ru/epz/order/extendedsearch/results.html?searchString={}"
CONTRACT_URL = "https://zakupki.gov.ru/epz/contract/contractCard/common-info.html?reestrNumber={}"


class _Api:
    """Обёртка над клиентом: считает новые запросы и умеет работать без сети."""

    def __init__(self, client: DamiaClient, network: bool):
        self.client, self.network, self.spent = client, network, {}

    def get(self, method: str, **params: str):
        """Ответ или None, если его нет среди сохранённых, а сеть не разрешена."""
        reply = self.client.cached(method, **params)
        if reply is None and self.network:
            reply = self.client.call(method, **params)
            if not reply.from_cache:
                self.spent[method] = self.spent.get(method, 0) + 1
        return reply


def _money(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".00", "") + " руб."


def _rule_attention(card: TenderCard, tender_id: str) -> list[Attention]:
    items = []
    if card.deadline:
        items.append(Attention(text=f"Срок подачи заявок: до {card.deadline}.", source_id=tender_id))
    if card.bid_security:
        items.append(Attention(
            text=f"Обеспечение заявки: {_money(card.bid_security)}"
                 + (f" ({card.bid_security / card.initial_price:.1%} начальной цены)."
                    if card.initial_price else "."),
            source_id=tender_id))
    if card.contract_security:
        share = card.contract_security / card.initial_price if card.initial_price else 0
        text = f"Обеспечение исполнения контракта: {_money(card.contract_security)}"
        text += f" ({share:.0%} начальной цены)." if share else "."
        if share >= 0.2:
            text += " Это много: деньги или банковская гарантия замораживаются на срок контракта."
        items.append(Attention(text=text, source_id=tender_id))
    return items


def _history(api: _Api, card: TenderCard, company_inn: str, company_contracts: list[Contract],
             lookups: int, sources: dict[str, Source], notes: list[str]
             ) -> customer_history.CustomerHistory:
    kwargs = dict(tender_codes=card.okpd_codes, tender_title=card.title, exclude_tender=card.regn,
                  company_contracts=company_contracts)
    reply = api.get("contracts", inn=card.customer_inn, fz="44", role="1", format="2")
    if reply is None:
        notes.append("История заказчика не запрошена: ответа нет среди сохранённых, "
                     "а запрос к API не разрешён.")
        return customer_history.summarize(card.customer_inn, [], company_inn, **kwargs)
    contracts, bad = damia_parse.parse_contracts(reply.data, card.customer_inn, "1")
    if bad:
        notes.append(f"Контрактов заказчика, которые не удалось разобрать: {bad}.")
    if damia_parse.has_next_page(reply.data):
        notes.append("У заказчика есть более ранние контракты: прочитана только первая страница.")
    notes.append("История заказчика: только контракты по 44-ФЗ.")
    sources["history"] = Source(
        id="history", kind="contract",
        title=f"Контракты заказчика (ИНН {card.customer_inn}), 44-ФЗ",
        used_for=["customer_history", "price"],
        comment=f"Получено {reply.fetched_at}, контрактов: {len(contracts)}.")

    first = customer_history.summarize(card.customer_inn, contracts, company_inn, **kwargs)
    details: dict[str, Contract] = {}
    wanted = customer_history.lookup_candidates(first, max(0, min(lookups, limits.HISTORY_LOOKUPS)))
    missing = 0
    for regn in wanted:
        answer = api.get("contract", regn=regn)
        parsed = damia_parse.parse_contract(answer.data, regn) if answer else None
        if parsed:
            details[regn] = parsed
        else:
            missing += 1
    if missing:
        notes.append(f"Поставщик не выяснен по {missing} похожим контрактам: "
                     "ответа нет среди сохранённых или запрос к API не разрешён.")
    history = customer_history.summarize(card.customer_inn, contracts, company_inn,
                                         details=details, **kwargs)
    for example in history.examples:
        sources[f"contract:{example.contract_regn}"] = Source(
            id=f"contract:{example.contract_regn}", kind="contract",
            title=f"Контракт от {example.published}: {example.title[:120]}",
            url=CONTRACT_URL.format(example.contract_regn), used_for=["customer_history"],
            comment=("Поставщик: " + ", ".join(example.supplier_names) + ". "
                     if example.supplier_names else "Поставщик не выяснен. ")
                    + f"Цена {_money(example.price)}")
    return history


def _price(card: TenderCard, history: customer_history.CustomerHistory,
           profile: profile_domain.CompanyProfile, cost: Optional[float]) -> prices.PriceEstimate:
    options = [
        (history.relevant_reductions, "по похожим закупкам этого заказчика", "history"),
        (history.reductions, "по всем закупкам этого заказчика", "history"),
        (profile.reductions, "по закупкам, которые выигрывала компания (у разных заказчиков)", "profile"),
    ]
    for stats, basis, _ in options:
        if stats.observations >= limits.MIN_PRICE_OBSERVATIONS:
            estimate = prices.estimate_price(card.initial_price, stats, cost)
            return replace(estimate, note=f"Снижение цены взято {basis}: "
                                          f"{stats.observations} наблюдений. {estimate.note}")
    return prices.estimate_price(card.initial_price, history.relevant_reductions, cost)


def _facts(card: TenderCard, profile: profile_domain.CompanyProfile, area: okpd.AreaMatch) -> dict:
    """Публичные факты о компании и закупке для модели, каждый со своим id источника."""
    size = profile.contract_price
    return {
        "company_profile": {
            "source_id": "profile",
            "contracts": profile.contracts, "period": [profile.period_from, profile.period_to],
            "okpd_codes": [{"code": c.code, "contracts": c.contracts} for c in profile.okpd_codes[:10]],
            "frequent_words_in_contract_titles": [t.stem for t in profile.title_terms[:20]],
            "contract_price": ({"median": size.median, "max": size.max} if size else None),
            "share_of_tenders_without_price_reduction": profile.reductions.no_reduction_share,
        },
        "tender": {
            "source_id": f"tender:{card.regn}", "title": card.title, "okpd_codes": card.okpd_codes,
            "initial_price": card.initial_price, "placing_way": card.placing_way,
            "bid_security": card.bid_security, "contract_security": card.contract_security,
        },
        "area_by_okpd_codes": area.level,
    }


def _history_facts(history: customer_history.CustomerHistory) -> dict:
    return {
        "customer_history": {
            "source_id": "history", "contracts": history.contracts,
            "period": [history.period_from, history.period_to],
            "similar_contracts": history.relevant_contracts,
            "company_contracts_with_this_customer": history.company_wins,
            "share_without_price_reduction_all": history.reductions.no_reduction_share,
            "median_price_reduction_similar": history.relevant_reductions.median,
            "similar_contracts_list": [
                {"source_id": f"contract:{e.contract_regn}", "date": e.published, "title": e.title[:160],
                 "price": e.price, "initial_price": e.tender_price or None,
                 "price_reduction": e.reduction, "supplier": list(e.supplier_names) or None,
                 "placing_way": e.placing_way or None}
                for e in history.examples],
        },
    }


def analyze_tender(
    client: DamiaClient,
    downloader: Downloader,
    store: Store,
    llm: Optional[LlmClient],
    cache: LlmCache,
    regn: str,
    *,
    network: bool = False,
    download: bool = False,
    cost: Optional[float] = None,
    lookups: int = limits.HISTORY_LOOKUPS,
    progress: Progress = lambda step, done, total: None,
    sleep: Callable[[float], None] = time.sleep,
) -> AnalysisCard:
    started = time.monotonic()
    saved = store.load("profile", ProfileFile)
    if saved is None:
        raise steps.NotCached("Сначала постройте профиль компании.")
    company_inn = saved.profile.inn
    api = _Api(client, network)
    notes: list[str] = []
    dropped: list[DroppedFinding] = []
    sources: dict[str, Source] = {}

    progress("Карточка закупки", 0, 1)
    reply = api.get("zakupka", regn=regn, actual="1")
    if reply is None:
        raise steps.NotCached(
            "Карточки закупки нет среди сохранённых. Повторите с разрешением на запрос к API: "
            "он потратит один запрос квоты.")
    card = damia_parse.parse_tender(reply.data, regn)
    if card is None:
        raise steps.NotFound(f"Закупка {regn} в источнике данных не найдена.")
    tender_id = f"tender:{regn}"
    sources[tender_id] = Source(id=tender_id, kind="tender", title=f"Закупка №{regn}: {card.title[:120]}",
                                url=TENDER_URL.format(regn), used_for=["tender", "attention"],
                                comment=f"Получено {reply.fetched_at}.")

    # Профиль без контракта самой закупки: оценка не должна знать исход.
    profile = saved.profile
    company_contracts: list[Contract] = []
    own = client.cached("contracts", inn=company_inn, fz="44", role="0", format="2")
    if own:
        company_contracts, _ = damia_parse.parse_contracts(own.data, company_inn, "0")
        without = [c for c in company_contracts if c.tender_regn != regn]
        if len(without) != len(company_contracts):
            profile = profile_domain.build_profile(company_inn, without)
            notes.append("Компания уже выиграла эту закупку. Её контракт исключён из профиля и из "
                         "истории заказчика: оценка сделана так, как будто исход неизвестен.")
            company_contracts = without
    sources["profile"] = Source(
        id="profile", kind="profile",
        title=f"Профиль компании (ИНН {company_inn}) по её контрактам",
        used_for=["area_match", "chance", "requirements"],
        comment=f"Контрактов: {profile.contracts}, период {profile.period_from} — {profile.period_to}.")

    area = okpd.match_area(card.okpd_codes, profile.code_counts())
    text_match = okpd.text_match(card.title, profile.terms())

    progress("История заказчика", 0, 1)
    history = _history(api, card, company_inn, company_contracts, lookups, sources, notes)

    progress("Документы", 0, 1)
    docs = steps.tender_documents(client, downloader, regn, download=download)
    notes += [n for n in docs.limitations if "не больше" not in n]
    texts: list[DocText] = []
    not_for_model = 0
    for item in docs.items:
        if item.stored is None:
            continue
        doc_id = f"doc:{item.stored.id}"
        readable = item.stored.readable
        for_model = readable and item.role in limits.LLM_READ_ROLES
        sources[doc_id] = Source(
            id=doc_id, kind="document", title=f"{item.title} ({item.group_date})", url=item.url,
            used_for=["requirements", "attention"] if for_model else [],
            comment=(f"Прочитан моделью, знаков: {item.stored.chars}." if for_model else
                     (f"Не передан модели: {item.role_label}." if readable else item.stored.problem)))
        if for_model:
            texts.append(DocText(doc_id, item.title, downloader.text(regn, item.stored)))
        elif readable:
            not_for_model += 1
    if not_for_model:
        notes.append(f"Файлов, которые модель не читала (описание объекта закупки): {not_for_model}. "
                     "Требования к предмету закупки в карточке не разобраны.")
    if docs.skipped:
        notes.append(f"Файлов закупки, которые не отобраны для чтения: {len(docs.skipped)} "
                     f"(недействующие редакции, протоколы, сверх предела {limits.DOCS_PER_TENDER}).")

    usage = Usage()
    requirements: list[Requirement] = []
    attention = _rule_attention(card, tender_id)
    hypotheses: list[Hypothesis] = []
    area_comment, semantic = "", None
    stats = AnalysisStats(documents_read=len(texts))
    if llm is None:
        notes.append("Модель не настроена: документация не прочитана, требования к участнику "
                     "и гипотезы в карточке отсутствуют.")
    elif not texts:
        notes.append("Нет текстов документации для чтения: файлы не скачаны или не прочитаны.")
    if llm is not None:
        reader = Reader(llm, cache, usage, sleep)
        read = reader.read(texts, lambda done, total: progress("Чтение документации", done, total))
        stats.parts_total, stats.parts_read = read.parts_total, read.parts_read
        stats.findings_kept = len(read.kept)
        stats.findings_total = len(read.kept) + len(read.rejected)
        dropped += [DroppedFinding(block=r.block, text=r.text, reason=r.reason) for r in read.rejected]
        if read.unread:
            notes.append(f"Частей документации, которые модель не прочитала: {len(read.unread)} "
                         f"из {read.parts_total}. " + " | ".join(read.unread[:3]))
        found = [f for f in read.kept if f.block == "requirements"]
        attention += [Attention(text=f.text, source_id=f.doc_id, quote=f.quote)
                      for f in read.kept if f.block == "attention"]

        progress("Сопоставление с профилем", 0, 2)
        facts = _facts(card, profile, area)
        assessments = [findings.Assessment("unknown", "", "", False) for _ in found]
        if found:
            try:
                answer = reader.ask(prompts.ASSESS_VERSION, prompts.ASSESS_SYSTEM, prompts.assess_user({
                    **facts, "requirements": [{"id": f"r{i}", "category": r.category, "requirement": r.text}
                                              for i, r in enumerate(found, 1)]}))
                assessments = findings.parse_assessments(answer, found)
                forced = sum(a.forced for a in assessments)
                if forced:
                    notes.append(f"Требований, которые модель сочла выполненными без подтверждения "
                                 f"в публичных данных: {forced}. Они помечены как «неизвестно».")
            except LlmError as e:
                notes.append(f"Сопоставление требований с профилем не выполнено: {e}")
        progress("Сопоставление с профилем", 1, 2)
        try:
            answer = reader.ask(prompts.CONTEXT_VERSION, prompts.CONTEXT_SYSTEM,
                                prompts.assess_user({**facts, **_history_facts(history)}))
            raw_hypotheses, rejected = findings.parse_hypotheses(answer, sources.keys())
            hypotheses = [Hypothesis(text=h.text, based_on=list(h.based_on), confidence=h.confidence)
                          for h in raw_hypotheses]
            dropped += [DroppedFinding(block=r.block, text=r.text, reason=r.reason) for r in rejected]
            level = answer.get("area_level")
            if level in ("high", "medium", "low", "none") and isinstance(answer.get("area_comment"), str):
                semantic, area_comment = level, " ".join(answer["area_comment"].split())[:600]
        except LlmError as e:
            notes.append(f"Оценка области по смыслу и гипотезы не получены: {e}")
        requirements = [
            Requirement(category=f.category, requirement=f.text, quote=f.quote, source_id=f.doc_id,
                        company_status=a.status, importance=a.importance, gap=a.gap, action=a.action)
            for f, a in zip(found, assessments)]
        order = {"high": 0, "medium": 1, "low": 2}
        requirements.sort(key=lambda r: order[r.importance])  # внутри группы порядок документа
        for h in hypotheses:
            for ref in h.based_on:
                if "winning_hypotheses" not in sources[ref].used_for:
                    sources[ref].used_for.append("winning_hypotheses")

    price = _price(card, history, profile, cost)
    size = profile.contract_price
    chance = chance_domain.estimate(chance_domain.ChanceInput(
        area_level=area.level, text_match=text_match, semantic_level=semantic,
        customer_contracts=history.contracts, company_wins_at_customer=history.company_wins,
        no_reduction_share=history.reductions.no_reduction_share,
        reduction_observations=history.reductions.observations,
        blocking_gaps=sum(r.company_status == "not_met" for r in requirements),
        open_questions=sum(r.company_status == "unknown" and r.importance == "high"
                           for r in requirements),
        documents_read=bool(requirements) or (llm is not None and stats.parts_read > 0),
        tender_price=card.initial_price, typical_price_max=size.max if size else 0.0,
    ))
    notes.append("Число заявок в прошлых закупках заказчика не запрашивалось: "
                 "конкуренция оценена по снижению цены.")

    stats.seconds = round(time.monotonic() - started, 1)
    stats.api_requests = dict(api.spent)
    stats.model_requests, stats.model_answers_reused = usage.requests, usage.cached
    stats.prompt_tokens, stats.completion_tokens = usage.prompt_tokens, usage.completion_tokens
    stats.waited_seconds = round(usage.waited_seconds, 1)
    result = AnalysisCard(
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%S"), company_inn=company_inn,
        tender=TenderInfo(
            regn=regn, law=card.law, title=card.title, customer_inn=card.customer_inn,
            customer_name=card.customer_name, region=card.region, initial_price=card.initial_price,
            deadline=card.deadline, status=card.status, placing_way=card.placing_way,
            okpd_codes=list(card.okpd_codes), bid_security=card.bid_security or None,
            contract_security=card.contract_security or None, url=TENDER_URL.format(regn)),
        area_match=area, area_comment=area_comment, area_level_by_meaning=semantic or "",
        customer_history=history,
        winning_hypotheses=hypotheses, requirements=requirements, price=price, chance=chance,
        attention=attention, sources=list(sources.values()), limitations=notes,
        dropped_findings=dropped, model=llm.model if llm is not None else "", stats=stats,
    )
    store.save_card(regn, result)
    progress("Готово", 1, 1)
    return result

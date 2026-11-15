import re
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app import __version__, config, limits
from app.api.access import is_local_request
from app.pipeline import analyze, backtest, export, steps
from app.pipeline.jobs import Busy, Job, Jobs
from app.schemas.card import (AnalysisCard, BacktestFile, CompareFile, DocumentsFile, ExportIndex,
                              ModelCheck, ProfileFile, TendersFile)
from app.sources.damia import DamiaClient, DamiaError, QuotaExceeded, QuotaLine
from app.sources.documents import Downloader
from app.sources.llm import LlmClient, LlmError, LlmNotConfigured, LlmRateLimited
from app.storage.files import Store
from app.storage.llm_cache import LlmCache

router = APIRouter(prefix="/api")

_INN = re.compile(r"^\d{10}(\d{2})?$")
_REGN = re.compile(r"^\d{8,25}$")
jobs = Jobs()
_DATE = r"^\d{4}-\d{2}-\d{2}$"


def get_client() -> DamiaClient:
    return DamiaClient(config.data_dir(), lambda: config.setting("DAMIA_KEY"))


def get_store() -> Store:
    return Store(config.data_dir())


def local_only(request: Request) -> None:
    client_host = request.client.host if request.client else None
    if not is_local_request(client_host, request.headers.get("host"), request.headers.get("origin")):
        raise HTTPException(403, "Это действие доступно только с компьютера, где запущено приложение.")


def get_downloader() -> Downloader:
    return Downloader(config.data_dir())


def _check_regn(regn: str) -> None:
    if not _REGN.match(regn):
        raise HTTPException(422, "Номер закупки должен состоять только из цифр.")


def get_llm() -> Optional[LlmClient]:
    """Клиент модели или None, если модель не настроена."""
    names = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")
    if not all(config.setting(n) for n in names):
        return None
    effort = config.setting("LLM_REASONING_EFFORT")
    return LlmClient(*(config.setting(n) for n in names),
                     extra={"reasoning_effort": effort} if effort else None)


class Analyze(BaseModel):
    network: bool = False  # разрешить запросы к API закупок (тратят квоту)
    download: bool = False  # разрешить скачивание файлов закупки с zakupki.gov.ru
    cost: Optional[float] = Field(None, ge=0)  # себестоимость для расчёта маржи (допущение)
    history_lookups: int = Field(limits.HISTORY_LOOKUPS, ge=0, le=limits.HISTORY_LOOKUPS)


class FetchDocuments(BaseModel):
    network: bool = False  # разрешить запрос карточки закупки к API, если её нет на диске


def _run(action):
    try:
        return action()
    except steps.NotCached as e:
        raise HTTPException(409, str(e))
    except steps.NotFound as e:
        raise HTTPException(404, str(e))
    except Busy as e:
        raise HTTPException(409, str(e))
    except LlmNotConfigured as e:
        raise HTTPException(409, str(e))
    except LlmRateLimited as e:
        raise HTTPException(429, str(e))
    except LlmError as e:
        raise HTTPException(502, str(e))
    except QuotaExceeded as e:
        raise HTTPException(429, str(e))
    except DamiaError as e:
        raise HTTPException(502, str(e))
    except export.SecretFound as e:
        raise HTTPException(500, str(e))


class Health(BaseModel):
    status: str
    version: str
    company_inn: str
    damia_key_set: bool
    model_set: bool


class QuotaReport(BaseModel):
    yearly_limit: int
    cap: int
    note: str
    methods: list[QuotaLine]


class BuildProfile(BaseModel):
    inn: Optional[str] = None
    network: bool = False  # разрешить запрос к API (тратит квоту), если ответа нет на диске
    max_pages: int = Field(1, ge=1, le=limits.PROFILE_MAX_PAGES)


class RunBacktest(BaseModel):
    network: bool = False  # разрешить запросы к API: один на каждого нового заказчика
    customers: int = Field(limits.BACKTEST_CUSTOMERS, ge=0, le=limits.BACKTEST_CUSTOMERS)
    max_pages: int = Field(limits.PROFILE_MAX_PAGES, ge=1, le=limits.PROFILE_MAX_PAGES)


class CompareRules(BaseModel):
    # имя набора -> изменения относительно текущих правил; параметры: см. chance.RuleSet
    variants: dict[str, dict[str, Any]] = Field(default_factory=dict, max_length=limits.COMPARE_MAX_VARIANTS)


class SearchTenders(BaseModel):
    query: str = Field(min_length=2, max_length=200)
    from_date: str = Field(pattern=_DATE)
    network: bool = False


@router.get("/health", response_model=Health)
def health() -> Health:
    return Health(
        status="ok", version=__version__, company_inn=config.company_inn(),
        damia_key_set=bool(config.setting("DAMIA_KEY")),
        model_set=all(config.setting(n) for n in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")),
    )


@router.get("/quota", response_model=QuotaReport)
def quota(client: DamiaClient = Depends(get_client)) -> QuotaReport:
    return QuotaReport(
        yearly_limit=limits.DAMIA_YEARLY_LIMIT, cap=limits.DAMIA_CAP,
        note="Тариф даёт запросы на год. Приложение останавливается на потолке, не доходя "
             "до лимита. Сохранённые ответы квоту не тратят.",
        methods=client.quota(),
    )


@router.get("/profile", response_model=ProfileFile)
def profile(store: Store = Depends(get_store)) -> ProfileFile:
    saved = store.load("profile", ProfileFile)
    if saved is None:
        raise HTTPException(404, "Профиль ещё не построен: POST /api/profile/build.")
    return saved


@router.post("/profile/build", response_model=ProfileFile, dependencies=[Depends(local_only)])
def profile_build(body: BuildProfile, client: DamiaClient = Depends(get_client),
                  store: Store = Depends(get_store)) -> ProfileFile:
    inn = body.inn or config.company_inn()
    if not _INN.match(inn):
        raise HTTPException(422, "ИНН должен состоять из 10 или 12 цифр.")
    return _run(lambda: steps.build_profile(client, store, inn, network=body.network,
                                            max_pages=body.max_pages))


@router.get("/tenders/{regn}/documents", response_model=DocumentsFile)
def documents(regn: str, client: DamiaClient = Depends(get_client),
              downloader: Downloader = Depends(get_downloader)) -> DocumentsFile:
    """Отобранные файлы закупки и их состояние. Ничего не скачивает и квоту не тратит."""
    _check_regn(regn)
    return _run(lambda: steps.tender_documents(client, downloader, regn))


@router.post("/tenders/{regn}/documents/fetch", response_model=DocumentsFile,
             dependencies=[Depends(local_only)])
def documents_fetch(regn: str, body: FetchDocuments, client: DamiaClient = Depends(get_client),
                    downloader: Downloader = Depends(get_downloader)) -> DocumentsFile:
    """Скачивает отобранные файлы с zakupki.gov.ru и извлекает текст."""
    _check_regn(regn)
    return _run(lambda: steps.tender_documents(client, downloader, regn, network=body.network,
                                               download=True))


@router.post("/tenders/{regn}/analyze", response_model=Job, status_code=202,
             dependencies=[Depends(local_only)])
def tender_analyze(regn: str, body: Analyze, client: DamiaClient = Depends(get_client),
                   downloader: Downloader = Depends(get_downloader),
                   store: Store = Depends(get_store)) -> Job:
    """Запускает анализ закупки в фоне. Ход: GET /api/jobs/{id}, результат: GET /api/cards/{regn}."""
    _check_regn(regn)
    llm, cache = get_llm(), LlmCache(config.data_dir())
    return _run(lambda: jobs.start(regn, lambda progress: analyze.analyze_tender(
        client, downloader, store, llm, cache, regn, network=body.network, download=body.download,
        cost=body.cost, lookups=body.history_lookups, progress=progress)))


@router.get("/jobs", response_model=list[Job])
def jobs_recent() -> list[Job]:
    return jobs.recent()


@router.get("/index", response_model=ExportIndex)
def live_index(store: Store = Depends(get_store)) -> ExportIndex:
    """Что сейчас лежит в результатах: профиль, список закупок, проверка, карточки."""
    return export.describe(store, config.company_inn())


@router.get("/jobs/{job_id}", response_model=Job)
def job_state(job_id: str) -> Job:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Задача не найдена. Сведения о задачах не переживают перезапуск сервера.")
    return job


@router.get("/cards/{regn}", response_model=AnalysisCard)
def card(regn: str, store: Store = Depends(get_store)) -> AnalysisCard:
    _check_regn(regn)
    saved = store.load_card(regn, AnalysisCard)
    if saved is None:
        raise HTTPException(404, "Карточки анализа для этой закупки ещё нет.")
    return saved


@router.post("/model/check", response_model=ModelCheck, dependencies=[Depends(local_only)])
def model_check() -> ModelCheck:
    """Один короткий запрос к модели: проверка ключа и ответа в JSON."""
    llm = get_llm()
    if llm is None:
        raise HTTPException(409, "Модель не настроена: задайте LLM_BASE_URL, LLM_API_KEY и LLM_MODEL в .env.")
    return _run(lambda: steps.check_model(llm))


@router.get("/tenders", response_model=TendersFile)
def tenders(store: Store = Depends(get_store)) -> TendersFile:
    saved = store.load("tenders", TendersFile)
    if saved is None:
        raise HTTPException(404, "Список закупок ещё не получен: POST /api/tenders/search.")
    return saved


@router.post("/tenders/search", response_model=TendersFile, dependencies=[Depends(local_only)])
def tenders_search(body: SearchTenders, client: DamiaClient = Depends(get_client),
                   store: Store = Depends(get_store)) -> TendersFile:
    return _run(lambda: steps.search_tenders(client, store, body.query, body.from_date,
                                             network=body.network))


@router.get("/backtest", response_model=BacktestFile)
def backtest_result(store: Store = Depends(get_store)) -> BacktestFile:
    saved = store.load("backtest", BacktestFile)
    if saved is None:
        raise HTTPException(404, "Проверка ещё не запускалась: POST /api/backtest/run.")
    return saved


@router.post("/backtest/run", response_model=BacktestFile, dependencies=[Depends(local_only)])
def backtest_run(body: RunBacktest, client: DamiaClient = Depends(get_client),
                 store: Store = Depends(get_store)) -> BacktestFile:
    return _run(lambda: backtest.run(client, store, config.company_inn(), network=body.network,
                                     max_pages=body.max_pages, customers=body.customers))


@router.post("/backtest/compare", response_model=CompareFile, dependencies=[Depends(local_only)])
def backtest_compare(body: CompareRules, client: DamiaClient = Depends(get_client)) -> CompareFile:
    """Сравнивает наборы правил на сохранённых данных. К API закупок не обращается."""
    try:
        return _run(lambda: backtest.compare(client, config.company_inn(), body.variants))
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.post("/export", response_model=ExportIndex, dependencies=[Depends(local_only)])
def export_package(store: Store = Depends(get_store)) -> ExportIndex:
    """Собирает пакет для веб-версии в папке data/export (папка создаётся заново)."""
    secrets = [config.setting(n) for n in ("DAMIA_KEY", "LLM_API_KEY")]
    return _run(lambda: export.build(store, config.data_dir() / "export", config.company_inn(),
                                     [s for s in secrets if s]))


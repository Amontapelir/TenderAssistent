import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app import __version__, config, limits
from app.api.access import is_local_request
from app.pipeline import steps
from app.schemas.card import ProfileFile, TendersFile
from app.sources.damia import DamiaClient, DamiaError, QuotaExceeded, QuotaLine
from app.storage.files import Store

router = APIRouter(prefix="/api")

_INN = re.compile(r"^\d{10}(\d{2})?$")
_DATE = r"^\d{4}-\d{2}-\d{2}$"


def get_client() -> DamiaClient:
    return DamiaClient(config.data_dir(), lambda: config.setting("DAMIA_KEY"))


def get_store() -> Store:
    return Store(config.data_dir())


def local_only(request: Request) -> None:
    client_host = request.client.host if request.client else None
    if not is_local_request(client_host, request.headers.get("host"), request.headers.get("origin")):
        raise HTTPException(403, "Это действие доступно только с компьютера, где запущено приложение.")


def _run(action):
    try:
        return action()
    except steps.NotCached as e:
        raise HTTPException(409, str(e))
    except QuotaExceeded as e:
        raise HTTPException(429, str(e))
    except DamiaError as e:
        raise HTTPException(502, str(e))


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

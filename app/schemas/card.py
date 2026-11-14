"""Карточка анализа закупки и файлы пакета для веб-версии.

Правило договора: каждый вывод ссылается на источник из списка `sources` по его `id`.
Вывод без источника в карточку не попадает.
"""
from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.domain.candidates import Candidate
from app.domain.chance import Chance
from app.domain.customer_history import CustomerHistory
from app.domain.okpd import AreaMatch
from app.domain.prices import PriceEstimate
from app.domain.profile import CompanyProfile
from app.sources.documents import StoredDoc

SCHEMA_VERSION = 1

Level = Literal["high", "medium", "low", "none", "unknown"]


class Source(BaseModel):
    id: str  # например "tender:0843500000226003988", "contract:…", "doc:1"
    kind: Literal["tender", "contract", "document", "search", "profile"]
    title: str
    url: str = ""
    used_for: list[str] = Field(default_factory=list)  # какие блоки карточки опираются на источник
    comment: str = ""


class TenderInfo(BaseModel):
    regn: str
    law: str
    title: str
    customer_inn: str
    customer_name: str
    region: str = ""
    initial_price: float = 0.0
    deadline: str = ""
    status: str = ""
    placing_way: str = ""
    okpd_codes: list[str] = Field(default_factory=list)
    bid_security: Optional[float] = None
    contract_security: Optional[float] = None
    url: str = ""


class Hypothesis(BaseModel):
    """Предположение о причине прошлых побед. Не факт."""

    text: str
    based_on: list[str]  # id источников
    confidence: Literal["high", "medium", "low"]


class Requirement(BaseModel):
    category: Literal["experience", "license", "security", "deadline", "staff",
                      "equipment", "documents", "restriction", "other"]
    requirement: str
    quote: str  # дословно из документа
    source_id: str
    company_status: Literal["met", "not_met", "unknown"]
    gap: str = ""  # чего не хватает
    action: str = ""  # что можно сделать


class Attention(BaseModel):
    text: str
    source_id: str
    quote: str = ""


class DroppedFinding(BaseModel):
    """Вывод модели, который не подтвердился цитатой и в карточку не попал."""

    block: str
    text: str
    reason: str


class AnalysisCard(BaseModel):
    schema_version: int = SCHEMA_VERSION
    generated_at: str
    company_inn: str
    tender: TenderInfo
    area_match: AreaMatch
    area_comment: str = ""  # оценка по смыслу, если её дала модель
    customer_history: CustomerHistory
    winning_hypotheses: list[Hypothesis] = Field(default_factory=list)
    requirements: list[Requirement] = Field(default_factory=list)
    price: PriceEstimate
    chance: Chance
    attention: list[Attention] = Field(default_factory=list)
    sources: list[Source]
    limitations: list[str] = Field(default_factory=list)
    dropped_findings: list[DroppedFinding] = Field(default_factory=list)
    model: str = ""  # какая модель читала документацию; пусто, если не читала


class DocumentState(BaseModel):
    title: str
    group: str
    group_date: str
    role: str
    role_label: str
    url: str
    stored: Optional[StoredDoc] = None  # None: файл ещё не скачан
    error: str = ""  # почему файл не получен при последней попытке


class SkippedDocument(BaseModel):
    title: str
    group: str
    reason: str


class DocumentsFile(BaseModel):
    schema_version: int = SCHEMA_VERSION
    regn: str
    tender_title: str
    items: list[DocumentState]
    skipped: list[SkippedDocument]
    readable: int  # из скольких файлов получен текст
    total_chars: int
    limitations: list[str] = Field(default_factory=list)


class ModelCheck(BaseModel):
    ok: bool
    model: str
    answer: str
    prompt_tokens: int
    completion_tokens: int
    attempts: int
    seconds: float


class ProfileFile(BaseModel):
    schema_version: int = SCHEMA_VERSION
    generated_at: str
    profile: CompanyProfile
    sources: list[Source]


class TendersFile(BaseModel):
    schema_version: int = SCHEMA_VERSION
    generated_at: str
    query: str
    from_date: str
    total: int
    open_total: int
    by_law: dict[str, int]
    items: list[Candidate]
    limitations: list[str] = Field(default_factory=list)

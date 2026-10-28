# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Capture every physical page of an offline PDF as a stable Source.

The parser keeps one Source per physical page so that an answer can always be
traced back to the original document revision and page.  Retrieval runs fully
offline over the pages already stored in a scoped Memory and returns exact
citations; it never invents pages or reads pre-parsed text.
"""

from __future__ import annotations

import hashlib
import io
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from itertools import pairwise
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote

from pydantic import BaseModel, Field
from pypdf import PdfReader

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    AdapterSourceDefinition,
    Source,
    SourceDefinition,
    SourceMaterialization,
    TextEvidence,
)

if TYPE_CHECKING:
    from powercontext.artifacts import MemoryCitation

PDF_PAGE_SOURCE_NAME = "pdf-page"
PDF_PAGE_DEFINITION_VERSION = "1"
_SHA256_HEX_LENGTH = 64


class PDFDocumentInput(BaseModel):
    """One complete offline PDF document to capture page by page."""

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    """The exact original text of one physical PDF page."""

    document_id: str
    document_sha256: str
    page_number: int = Field(ge=1)
    text: str


class PDFPageSource(Source):
    """One captured physical page identified by its document revision and page."""

    materialization: SourceMaterialization = SourceMaterialization.CAPTURED
    source_id: str
    document_id: str
    document_sha256: str
    page_number: int
    text: str


class PDFPageSourceAdapter:
    """Resolve one page input into its durable identity and read the page back."""

    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        source_id = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=source_id,
            source_id=source_id,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


PDF_PAGE_SOURCE_ADAPTER = PDFPageSourceAdapter()


class PDFPageTextEvidenceProjection:
    """Expose one captured page through the standard text-evidence projection."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.source_id,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_DEFINITION: SourceDefinition[PDFPageInput, PDFPageSource, PDFPageInput] = AdapterSourceDefinition(
    PDF_PAGE_SOURCE_ADAPTER,
    version=PDF_PAGE_DEFINITION_VERSION,
    projections=(PDFPageTextEvidenceProjection(),),
)


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Parse every physical page of one complete PDF, in order, or fail atomically."""

    _validate_document_id(value.document_id)
    content = value.content
    if not isinstance(content, (bytes, bytearray)):
        raise ValueError("PDF content must be bytes")  # noqa: TRY003, TRY004
    raw = bytes(content)
    if not raw:
        raise ValueError("PDF content must not be empty")  # noqa: TRY003
    document_sha256 = hashlib.sha256(raw).hexdigest()
    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise ValueError("PDF cannot be opened without a user password")  # noqa: TRY003, TRY301
        pages: list[PDFPageInput] = []
        for page_number, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            pages.append(
                PDFPageInput(
                    document_id=value.document_id,
                    document_sha256=document_sha256,
                    page_number=page_number,
                    text=text if text.strip() else "",
                )
            )
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("PDF document could not be parsed completely") from error  # noqa: TRY003
    return tuple(pages)


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """Return the stable, bounded identity of one PDF page Source."""

    _validate_document_id(document_id)
    _validate_page(document_sha256, page_number)
    direct = f"pdf:{quote(document_id, safe='')}:{document_sha256}:{page_number}"
    if len(direct) <= MAX_SOURCE_ID_LENGTH:
        return direct
    digest = hashlib.sha256(f"{document_id}\x00{document_sha256}\x00{page_number}".encode()).hexdigest()
    return f"pdf-h:{digest}:{page_number}"


def _validate_document_id(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("document_id must be a non-empty, non-blank string")  # noqa: TRY003


def _validate_page(document_sha256: str, page_number: int) -> None:
    if (
        not isinstance(document_sha256, str)
        or len(document_sha256) != _SHA256_HEX_LENGTH
        or document_sha256.lower() != document_sha256
        or any(character not in "0123456789abcdef" for character in document_sha256)
    ):
        raise ValueError("document_sha256 must be a lowercase SHA-256 hex digest")  # noqa: TRY003
    if not isinstance(page_number, int) or isinstance(page_number, bool) or page_number < 1:
        raise ValueError("page_number must be a positive integer")  # noqa: TRY003


# ---------------------------------------------------------------------------
# Offline retrieval over stored page entries
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r"[a-z]+|\d+")


@dataclass(frozen=True)
class _StoredPage:
    text: str
    citation: MemoryCitation


@dataclass
class _Collection:
    """Page entries currently stored in one scoped Memory, grouped by document."""

    documents: dict[str, list[_StoredPage]]
    corpora: dict[str, _Corpus] = field(default_factory=dict)

    def corpus(self, document: str) -> _Corpus:
        corpus = self.corpora.get(document)
        if corpus is None:
            corpus = _Corpus([page.text for page in self.documents[document]])
            self.corpora[document] = corpus
        return corpus


_COLLECTION_CACHE: dict[tuple[str, str], _Collection] = {}
_COLLECTION_CACHE_LIMIT = 2


async def search_pdf_pages(memory: Any, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Return up to ``limit`` exact citations for distinct pages relevant to ``question``.

    Only pages already stored in the supplied scoped Memory are considered, and
    every returned citation is taken verbatim from a stored entry record.
    """

    if not isinstance(question, str) or not question.strip():
        return ()
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return ()

    collection = await _load_collection(memory)
    if collection is None:
        return await _search_fallback(memory, question, limit)

    document = _select_document(question, tuple(collection.documents))
    if document is not None:
        corpus = collection.corpus(document)
        ranked = corpus.rank(question, limit)
        return tuple(collection.documents[document][index].citation for _score, index in ranked)

    merged: list[tuple[float, str, int]] = []
    for name in sorted(collection.documents):
        for score, index in collection.corpus(name).rank(question, limit):
            merged.append((score, name, index))
    merged.sort(key=lambda item: (-item[0], item[1], item[2]))
    return tuple(collection.documents[name][index].citation for _score, name, index in merged[:limit])


async def _load_collection(memory: Any) -> _Collection | None:  # noqa: C901 - snapshot validation and grouping
    lister = getattr(memory, "list", None)
    if lister is None:
        return None
    try:
        page = await lister()
    except Exception:
        return None
    entries = tuple(page.entries or ())
    if not entries:
        return None

    memory_key = page.memory_ref.model_dump_json() if page.memory_ref is not None else ""
    digest = hashlib.sha256()
    for record in entries:
        digest.update(record.entry.entry_version_id.encode())
        digest.update(b"\x00")
        for reference in record.entry.sources:
            digest.update(reference.source_type.encode())
            digest.update(b":")
            digest.update(reference.source_id.encode())
            digest.update(b"\x00")
        digest.update(b"\x01")
    cache_key = (memory_key, digest.hexdigest())
    cached = _COLLECTION_CACHE.get(cache_key)
    if cached is not None:
        return cached

    documents: dict[str, list[_StoredPage]] = {}
    seen: set[str] = set()
    for record in entries:
        for reference in record.entry.sources:
            if reference.source_type != PDF_PAGE_SOURCE_NAME or reference.source_id in seen:
                continue
            document, text = _stored_page_content(record.entry.text, reference.source_id)
            seen.add(reference.source_id)
            documents.setdefault(document, []).append(_StoredPage(text=text, citation=record.citation))
    if not documents:
        return None

    collection = _Collection(documents=documents)
    if len(_COLLECTION_CACHE) >= _COLLECTION_CACHE_LIMIT:
        _COLLECTION_CACHE.pop(next(iter(_COLLECTION_CACHE)))
    _COLLECTION_CACHE[cache_key] = collection
    return collection


def _stored_page_content(entry_text: str, source_id: str) -> tuple[str, str]:
    """Recover the document identity and page text from a stored page entry."""

    if not source_id.startswith("pdf:"):
        return source_id, entry_text
    parts = source_id[len("pdf:") :].rsplit(":", 2)
    if len(parts) != 3:
        return source_id, entry_text
    document = unquote(parts[0])
    text = entry_text
    if document and text.startswith(document):
        remainder = text[len(document) :]
        if remainder.startswith("\n"):
            remainder = remainder[1:]
        text = remainder
    return document, text


def _select_document(question: str, documents: tuple[str, ...]) -> str | None:
    if not documents:
        return None
    if len(documents) == 1:
        return documents[0]
    compact_question = re.sub(r"[^a-z0-9]+", "", question.lower())
    question_tokens = set(_TOKEN.findall(question.lower()))
    scored = [(doc, _document_score(doc, compact_question, question_tokens)) for doc in documents]
    best = max(score for _doc, score in scored)
    if best <= 0:
        return None
    winners = [doc for doc, score in scored if score == best]
    return winners[0] if len(winners) == 1 else None


def _document_score(document: str, compact_question: str, question_tokens: set[str]) -> float:
    slug = unquote(document).lower()
    compact = re.sub(r"[^a-z0-9]+", "", slug)
    base = re.sub(r"(?:19|20)\d{2}.*$", "", slug).strip("_- ")
    base_compact = re.sub(r"[^a-z0-9]+", "", base)
    score = 0.0
    if base_compact and base_compact in compact_question:
        score += 10.0 + min(len(base_compact), 20) / 10.0
    elif compact and compact in compact_question:
        score += 8.0
    base_tokens = {token for token in _TOKEN.findall(base) if len(token) >= 2 and not token.isdigit()}
    score += 3.0 * sum(1 for token in base_tokens if token in question_tokens)
    return score


# --- offline ranking -------------------------------------------------------

_STOP = frozenset([
    "a",
    "an",
    "the",
    "and",
    "or",
    "for",
    "of",
    "to",
    "in",
    "on",
    "at",
    "by",
    "from",
    "with",
    "as",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "what",
    "which",
    "who",
    "how",
    "why",
    "when",
    "where",
    "do",
    "does",
    "did",
    "can",
    "could",
    "should",
    "would",
    "i",
    "we",
    "you",
    "it",
    "its",
    "our",
    "your",
    "this",
    "that",
    "these",
    "those",
    "please",
    "has",
    "have",
    "had",
    "report",
    "reported",
    "reports",
    "year",
    "years",
    "fiscal",
    "fy",
    "company",
    "inc",
    "form",
    "annual",
    "following",
    "according",
    "based",
    "used",
    "using",
    "use",
    "only",
    "shown",
    "set",
    "forth",
    "respectively",
    "primarily",
    "directly",
    "outlined",
    "stated",
    "given",
    "detail",
    "details",
    "name",
    "value",
    "values",
    "number",
    "amount",
    "total",
    "percentage",
    "percent",
    "any",
    "important",
    "state",
    "then",
    "if",
    "not",
    "define",
    "defined",
    "calculated",
    "calculate",
    "give",
    "given",
    "response",
    "respond",
    "question",
    "assuming",
    "assume",
    "assumed",
    "perspective",
    "investment",
    "analyst",
    "rely",
    "relying",
    "within",
    "usd",
    "billions",
    "millions",
    "million",
    "shown",
])

# Generic finance thesaurus applied to question concepts (values are stems).
_THE: dict[str, list[str]] = {
    "geograph": ["geograph", "region", "countr", "segment", "market", "international", "worldwide", "area"],
    "operat": ["operat", "segment", "division", "business", "facilit", "plant", "countr"],
    "legal": ["legal", "litigat", "proceed", "claim", "disput", "investigat", "contingenc"],
    "battl": ["litigat", "proceed", "claim", "legal", "disput"],
    "litigat": ["litigat", "legal", "proceed", "claim", "disput"],
    "lawsuit": ["litigat", "legal", "proceed", "claim"],
    "restructur": ["restructur", "charg", "impair", "productiv", "severanc"],
    "customer": ["customer", "client", "airlin", "govern", "contract", "distributor", "retail", "concentrat"],
    "client": ["customer", "client", "contract"],
    "product": ["product", "servic", "offer", "brand", "microprocessor", "semiconductor", "technolog"],
    "servic": ["servic", "product", "offer", "support"],
    "segment": ["segment", "division", "report"],
    "divison": ["segment", "division"],
    "acquisit": ["acquisit", "merger", "divestitur", "purchas", "combine"],
    "merger": ["merger", "acquisit", "divestitur"],
    "cost": ["cost", "expens", "charg"],
    "incom": ["incom", "profit", "earn", "revenu"],
    "revenu": ["revenu", "sale", "net"],
    "sale": ["sale", "revenu"],
    "ebitda": ["ebitda", "operat", "profit", "incom", "depreci", "amortiz", "earn"],
    "capex": ["capital", "spending", "expenditur", "purchas", "propert", "plant", "equip"],
    "depreci": ["depreci", "amortiz"],
    "amortiz": ["amortiz", "depreci"],
    "margin": ["margin", "profit", "profitab", "incom", "revenu", "percent"],
    "cash": ["cash", "flow", "activ"],
    "flow": ["flow", "cash", "activ"],
    "liquidity": ["liquid", "current", "asset", "liabilit", "cash", "receiv", "inventori", "payabl"],
    "liquid": ["liquid", "current", "asset", "liabilit", "cash", "receiv", "inventori", "payabl"],
    "debt": ["debt", "borrow", "note", "oblig"],
    "dividend": ["dividend", "payout", "share"],
    "share": ["share", "stock", "equity", "common"],
    "tax": ["tax", "provision"],
    "interest": ["interest", "expens"],
    "liabilit": ["liabilit", "oblig"],
    "inventori": ["inventori", "cogs", "cost"],
    "receiv": ["receiv", "account"],
    "payabl": ["payabl", "account"],
    "asset": ["asset"],
    "equity": ["equity", "shareholder", "stock"],
    "leas": ["leas", "rent"],
    "pension": ["pension", "retire", "benefit"],
    "employe": ["employe", "compens", "headcount"],
    "impair": ["impair", "charg", "writedown"],
    "goodwil": ["goodwil", "intang"],
    "cyclical": ["cyclical", "cycle", "downturn", "economic", "seasonal"],
    "production": ["production", "rate", "deliver", "build", "manufactur"],
    "concentration": ["concentrat", "customer", "major", "significant"],
    "competition": ["competit", "rival", "competitor"],
    "supply": ["supply", "chain", "supplier"],
    "backlog": ["backlog", "order"],
    "risk": ["risk", "uncertain", "exposur"],
    "growth": ["growth", "grow", "increase", "decline", "decreas"],
    "categor": ["segment", "product", "service", "division", "business"],
    "categori": ["segment", "product", "service", "division", "business"],
}

_STATEMENT_PATTERNS = {
    "income": re.compile(
        r"(?:consolidated\s+)?statement(?:s)?\s+of\s+(?:income|operations|earnings)|"
        r"statement\s+of\s+(?:income|operations)|"
        r"income\s+statement|p\s*&\s*l|profit\s+and\s+loss|"
        r"consolidated\s+statements?\s+of\s+operations"
    ),
    "balance": re.compile(
        r"(?:consolidated\s+)?balance\s+sheet|statement(?:s)?\s+of\s+financial\s+position|"
        r"financial\s+position"
    ),
    "cash": re.compile(
        r"(?:consolidated\s+)?statement(?:s)?\s+of\s+cash\s+flows?|cash\s+flows?\s+statement|"
        r"consolidated\s+statements?\s+of\s+cash\s+flows"
    ),
    "equity": re.compile(r"statement(?:s)?\s+of\s+(?:stockholders'?\s+)?equity|changes\s+in\s+equity"),
    "segment": re.compile(r"(?:business\s+)?segment\s+(?:data|information|results)|sales\s+and\s+operating\s+income"),
}

_SECTION_PATTERNS = {
    "mda": re.compile(
        r"management'?s\s+discussion|results\s+of\s+operations|md\s*&\s*a|"
        r"financial\s+condition\s+and\s+results"
    ),
    "business": re.compile(
        r"item\s+1[\.\s]+business|our\s+business|company\s+overview|overview|products?\s+and\s+services?"
    ),
    "risk": re.compile(r"risk\s+factors"),
    "legal": re.compile(r"legal\s+proceedings|litigation|contingenc"),
    "segment": re.compile(r"segment\s+(?:data|information|results)|reportable\s+segments?|business\s+segments?"),
    "notes": re.compile(r"notes?\s+to\s+(?:the\s+)?consolidated\s+financial\s+statements"),
}


@dataclass
class _Concept:
    terms: tuple[str, ...]
    weight: float
    kind: str = "core"


@dataclass
class _Plan:
    concepts: list[_Concept] = field(default_factory=list)
    phrases: list[tuple[str, ...]] = field(default_factory=list)
    statements: set[str] = field(default_factory=set)
    sections: set[str] = field(default_factory=set)


def _stem(token: str) -> str:  # noqa: C901 - explicit suffix cascade keeps stemming predictable
    t = token
    if len(t) <= 3 or t.isdigit():
        return t
    if t.endswith("ies") and len(t) > 4:
        t = t[:-3] + "y"
    elif t.endswith("sses") or (t.endswith("es") and len(t) > 4):
        t = t[:-2]
    elif t.endswith("s") and not t.endswith(("ss", "us", "is")) and len(t) > 3:
        t = t[:-1]
    if t.endswith("ations"):
        t = t[:-6]
    elif t.endswith("ation") or t.endswith("ions"):
        t = t[:-4]
    elif t.endswith("ion") or (t.endswith("ing") and len(t) > 5):
        t = t[:-3]
    elif (t.endswith("ed") and len(t) > 4) or (t.endswith("ly") and len(t) > 4):
        t = t[:-2]
    if t.endswith("ity") and len(t) > 5:
        t = t[:-3]
    elif t.endswith("ic") and len(t) > 5:
        t = t[:-2]
    if t.endswith("y") and len(t) > 4:
        t = t[:-1]
    if t.endswith("e") and len(t) > 4:
        t = t[:-1]
    return t


def _raw_tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _tokenize(text: str) -> list[str]:
    return [_stem(token) for token in _raw_tokens(text) if token not in _STOP]


def _add(plan: _Plan, terms: list[str] | tuple[str, ...], weight: float, kind: str) -> None:
    clean = tuple(dict.fromkeys(_stem(term) for term in terms if term and term not in _STOP))
    if clean:
        plan.concepts.append(_Concept(clean, weight, kind))


def _build_plan(question: str) -> _Plan:  # noqa: C901 - one branch per finance retrieval template
    lowered = question.lower()
    plan = _Plan()
    raw = _raw_tokens(question)

    for name, pattern in _STATEMENT_PATTERNS.items():
        if pattern.search(lowered):
            plan.statements.add(name)
    for name, pattern in _SECTION_PATTERNS.items():
        if pattern.search(lowered):
            plan.sections.add(name)

    if re.search(r"quick\s+ratio|liquidity|current\s+ratio|working\s+capital|positive\s+working\s+capital", lowered):
        _add(
            plan,
            ["current", "asset", "cash", "receivable", "inventory", "current", "liabilit", "payable"],
            1.0,
            "metric",
        )
        plan.statements.add("balance")
        plan.phrases.append(("current", "asset"))
        plan.phrases.append(("current", "liabilit"))
    if re.search(
        r"capital[-\s]intensive|capital\s+expenditure|capex|net\s+ppne|pp\s*&\s*e|property,?\s+plant", lowered
    ):
        _add(
            plan,
            [
                "capital",
                "expenditure",
                "spending",
                "purchase",
                "property",
                "plant",
                "equipment",
                "depreciation",
                "sales",
                "revenue",
                "asset",
            ],
            1.0,
            "metric",
        )
        plan.statements.update({"income", "balance", "cash"})
        plan.phrases.append(("property", "plant"))
        plan.phrases.append(("capital", "expenditure"))
    if re.search(r"ebitda", lowered):
        _add(plan, ["operating", "income", "profit", "depreciation", "amortization", "earnings"], 1.0, "metric")
        plan.statements.update({"income", "cash"})
        plan.phrases.append(("depreciation", "amortization"))
        plan.phrases.append(("operating", "income"))
    if re.search(r"free\s+cash\s*flow|operating\s+cash\s*flow|cash\s+flow\s+from\s+operations", lowered):
        _add(
            plan,
            ["cash", "operating", "activities", "capital", "expenditure", "purchase", "property", "plant", "equipment"],
            1.0,
            "metric",
        )
        plan.statements.update({"cash", "income"})
        plan.phrases.append(("cash", "flow"))
    if re.search(r"inventory\s+turnover|days\s+inventory|\bdio\b|sold\s+its\s+inventory", lowered):
        _add(plan, ["inventory", "inventories", "cost", "sales", "goods", "sold"], 1.0, "metric")
        plan.statements.update({"balance", "income"})
        plan.phrases.append(("cost", "sale"))
    if re.search(r"days\s+payable|\bdpo\b", lowered):
        _add(plan, ["account", "payable", "cost", "sales", "goods", "inventory"], 1.0, "metric")
        plan.statements.update({"balance", "income"})
        plan.phrases.append(("account", "payable"))
    if re.search(r"days\s+sales|\bdso\b|accounts?\s+receivable", lowered):
        _add(plan, ["account", "receivable", "revenue", "sales"], 1.0, "metric")
        plan.statements.update({"balance", "income"})
        plan.phrases.append(("account", "receivable"))
    if re.search(r"return\s+on\s+assets|\broa\b|asset\s+turnover|fixed\s+asset\s+turnover", lowered):
        _add(
            plan,
            ["net", "income", "total", "asset", "revenue", "sales", "property", "plant", "equipment"],
            1.0,
            "metric",
        )
        plan.statements.update({"income", "balance"})
        plan.phrases.append(("total", "asset"))
    if re.search(r"return\s+on\s+equity|\broe\b", lowered):
        _add(plan, ["net", "income", "shareholder", "equity"], 1.0, "metric")
        plan.statements.update({"income", "balance"})
    if re.search(
        r"gross\s+margin|operating\s+margin|net\s+profit\s+margin|profit\s+margin|margin\s+profile|margin\s+change",
        lowered,
    ):
        _add(
            plan, ["gross", "profit", "operating", "income", "net", "sales", "revenue", "cost", "sales"], 1.0, "metric"
        )
        plan.statements.add("income")
        plan.sections.add("mda")
        plan.phrases.append(("gross", "profit"))
        plan.phrases.append(("operating", "income"))
    if re.search(r"effective\s+tax\s+rate|tax\s+rate", lowered):
        _add(plan, ["effective", "tax", "rate", "provision", "income", "taxes", "before", "taxes"], 1.0, "metric")
        plan.statements.add("income")
        plan.phrases.append(("income", "tax"))
        plan.phrases.append(("tax", "rate"))
    if re.search(r"dividend\s+payout|retention\s+ratio|dividend", lowered):
        _add(plan, ["dividend", "dividends", "paid", "net", "income", "shareholder", "common"], 1.0, "metric")
        plan.statements.update({"cash", "income"})
        plan.phrases.append(("dividend", "paid"))
    if re.search(r"revenue\s+change|revenue\s+growth|sales\s+change|sales\s+growth|year-over-year\s+change", lowered):
        _add(plan, ["revenue", "revenues", "sales", "net", "change", "growth"], 1.0, "metric")
        plan.statements.add("income")
        plan.phrases.append(("net", "sales"))
    if re.search(r"operating\s+income|operating\s+profit", lowered):
        _add(plan, ["operating", "income", "profit"], 1.0, "metric")
        plan.statements.add("income")
        plan.phrases.append(("operating", "income"))

    if re.search(r"legal|litigation|lawsuit|legal\s+battle|proceeding|claim|dispute|investigation", lowered):
        _add(
            plan,
            ["legal", "litigation", "proceedings", "claims", "disputes", "investigations", "contingencies"],
            1.0,
            "topic",
        )
        plan.sections.add("legal")
    if re.search(r"geograph|region|country|countries|operate\s+in", lowered):
        _add(
            plan,
            ["geographic", "regions", "countries", "markets", "segments", "international", "worldwide"],
            1.0,
            "topic",
        )
        plan.sections.add("business")
    if re.search(r"customer|client|concentration", lowered):
        _add(plan, ["customer", "customers", "concentration", "airlines", "government", "contracts"], 1.0, "topic")
        _add(plan, ["revenue", "revenues", "significant", "substantial", "portion", "percent", "number"], 0.6, "topic")
        plan.phrases.extend([
            ("government", "contract"),
            ("commercial", "airlines"),
            ("significant", "portion"),
            ("substantial", "portion"),
            ("limited", "number"),
            ("revenues", "earned"),
        ])
    if re.search(r"product|service|offering|brand", lowered):
        _add(plan, ["products", "services", "offerings", "brands", "business"], 1.0, "topic")
        plan.sections.add("business")
        if re.search(
            r"categor|represent.*revenue|revenue.*represent|more\s+than\s+\d+\s*%|percent\s+of\s+revenue", lowered
        ):
            _add(plan, ["segments", "division", "reportable", "revenue", "category"], 1.0, "topic")
            plan.sections.add("segment")
            plan.phrases.extend([("business", "segment"), ("segment", "data"), ("revenue", "commercial")])
    if re.search(r"segment|division", lowered):
        _add(plan, ["segments", "division", "reportable"], 1.0, "topic")
        plan.sections.add("segment")
        plan.statements.add("segment")
    if re.search(r"acquisition|merger|m\s*&\s*a|divestiture", lowered) or re.search(r"\bm\s*&\s*a\b", lowered):
        _add(plan, ["acquisitions", "acquired", "merger", "divestiture", "purchase"], 1.0, "topic")
    if re.search(r"cyclical|cycle|downturn", lowered):
        _add(plan, ["cyclical", "cyclicality", "cycles", "downturn", "economic"], 1.0, "topic")
    if re.search(r"production\s+rate|production\s+change|production", lowered):
        _add(plan, ["production", "rates", "deliveries", "build", "manufacturing"], 1.0, "topic")
    if re.search(r"restructuring|severance|productivity\s+plan", lowered):
        _add(plan, ["restructuring", "charges", "severance", "productivity"], 1.0, "topic")
    if re.search(r"customer\s+concentration|concentration", lowered):
        _add(plan, ["customer", "concentration", "revenue", "percent"], 1.0, "topic")

    core: list[str] = []
    for raw_token in raw:
        if raw_token in _STOP:
            continue
        term = _stem(raw_token)
        if term in _STOP or len(term) < 2 or term in core:
            continue
        core.append(term)
        plan.concepts.append(_Concept(tuple(dict.fromkeys([term, *_THE.get(term, [])])), 1.0, "core"))

    sequence = [_stem(token) for token in raw if token not in _STOP]
    plan.phrases.extend((first, second) for first, second in pairwise(sequence) if first != second)

    normalized: list[tuple[str, ...]] = []
    for phrase in plan.phrases:
        cleaned = tuple(_stem(token) for token in phrase if token not in _STOP)
        if len(cleaned) >= 2 and cleaned not in normalized:
            normalized.append(cleaned)
    plan.phrases = normalized
    return plan


class _Corpus:
    """Offline lexical index and page ranking for one document."""

    def __init__(self, pages: list[str]) -> None:
        self.pages = pages
        self.tokens = [_tokenize(page) for page in pages]
        self.counts = [Counter(tokens) for tokens in self.tokens]
        self.sets = [set(tokens) for tokens in self.tokens]
        self.lengths = [len(tokens) for tokens in self.tokens]
        self.n = len(pages)
        self.avg = max(sum(self.lengths) / max(1, self.n), 1.0)
        df: Counter[str] = Counter()
        for token_set in self.sets:
            df.update(token_set)
        self.df = df
        heads = ["\n".join(_page_lines(page)[:14]) for page in pages]
        self.stmt = [
            {name for name, pattern in _STATEMENT_PATTERNS.items() if pattern.search(head.lower())} for head in heads
        ]
        self.section = [
            {name for name, pattern in _SECTION_PATTERNS.items() if pattern.search(page.lower()[:4000])}
            for page in pages
        ]

    def idf(self, term: str) -> float:
        document_frequency = self.df.get(term, 0)
        return math.log(1 + (self.n - document_frequency + 0.5) / (document_frequency + 0.5))

    def concept_idf(self, terms: tuple[str, ...]) -> float:
        return max((self.idf(term) for term in terms if self.df.get(term)), default=0.0)

    def rank(self, question: str, limit: int = 20) -> list[tuple[float, int]]:  # noqa: C901 - staged offline scoring
        if limit <= 0 or not self.pages:
            return []
        plan = _build_plan(question)
        term_weights: dict[str, float] = {}
        for concept in plan.concepts:
            for index, term in enumerate(concept.terms):
                weight = concept.weight * (1.0 if index == 0 else 0.55)
                term_weights[term] = max(term_weights.get(term, 0.0), weight)

        rows: list[tuple[float, float, float, float, float, float, int]] = []
        for i, count in enumerate(self.counts):
            lex = 0.0
            for term, weight in term_weights.items():
                frequency = count[term]
                if not frequency:
                    continue
                lex += (
                    weight
                    * self.idf(term)
                    * frequency
                    * 2.2
                    / (frequency + 1.2 * (0.25 + 0.75 * self.lengths[i] / self.avg))
                )
            coverage_numerator = coverage_denominator = 0.0
            for concept in plan.concepts:
                value = self.concept_idf(concept.terms) ** 2 * concept.weight
                coverage_denominator += value
                if any(count[term] for term in concept.terms):
                    coverage_numerator += value
            coverage = coverage_numerator / coverage_denominator if coverage_denominator else 0.0
            window = 0.0
            tokens = self.tokens[i]
            if tokens:
                for start in range(0, max(1, len(tokens) - 23), 6):
                    present = set(tokens[start : start + 24])
                    value = sum(
                        self.concept_idf(concept.terms) ** 2 * concept.weight
                        for concept in plan.concepts
                        if any(term in present for term in concept.terms)
                    )
                    window = max(window, value)
            phrase = 0.0
            for phrase_terms in plan.phrases:
                if all(count[term] for term in phrase_terms) and _contains_sequence(tokens, phrase_terms):
                    phrase += sum(self.idf(term) for term in phrase_terms)
            stmt = 1.0 if self.stmt[i] & plan.statements else 0.0
            section = 1.0 if self.section[i] & plan.sections else 0.0
            rows.append((lex, coverage, window, phrase, stmt, section, i))

        max_lex = _percentile([row[0] for row in rows], 0.99) or 1.0
        max_window = _percentile([row[2] for row in rows], 0.99) or 1.0
        max_phrase = _percentile([row[3] for row in rows], 0.99) or 1.0
        final: list[tuple[float, int]] = []
        for lex, coverage, window, phrase, stmt, section, index in rows:
            score = (
                1.0 * lex / max_lex
                + 0.35 * coverage
                + 0.45 * window / max_window
                + 0.25 * phrase / max_phrase
                + 0.90 * stmt
                + 0.30 * section
            )
            final.append((score, index))
        final.sort(key=lambda item: (-item[0], item[1]))
        return final[:limit]


def _page_lines(text: str) -> list[str]:
    return [line for line in text.split("\n") if line.strip()]


def _contains_sequence(tokens: list[str], sequence: tuple[str, ...]) -> bool:
    first = sequence[0]
    width = len(sequence)
    return any(
        token == first and tuple(tokens[index : index + width]) == sequence for index, token in enumerate(tokens)
    )


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


async def _search_fallback(  # noqa: C901 - bounded query expansion and citation resolution
    memory: Any, question: str, limit: int
) -> tuple[MemoryCitation, ...]:
    """Use the public search route when the scope does not expose entry listing."""

    try:
        from powercontext.artifacts import MemoryCitation as Citation
        from powercontext.builtin.runtime import GetMemoryEntryRequest, SearchMemoryRequest
    except Exception:
        return ()

    plan = _build_plan(question)
    queries = [question]
    for concept in plan.concepts[:10]:
        terms = [term for term in concept.terms if not term.isdigit()][:3]
        if terms:
            queries.append(" ".join(terms))

    citations: list[MemoryCitation] = []
    seen: set[str] = set()
    for query in queries:
        try:
            page = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=max(limit * 4, 40)))
        except Exception:  # noqa: S112 - keep trying the remaining query variants
            continue
        for hit in page.hits:
            citation = Citation(memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id)
            try:
                record = await memory.get(GetMemoryEntryRequest(citation=citation))
            except Exception:  # noqa: S112 - skip a citation that no longer resolves
                continue
            for reference in record.entry.sources:
                if reference.source_type == PDF_PAGE_SOURCE_NAME and reference.source_id not in seen:
                    seen.add(reference.source_id)
                    citations.append(citation)
                    break
            if len(citations) >= limit:
                return tuple(citations)
    return tuple(citations)

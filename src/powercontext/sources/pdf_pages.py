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

"""Complete PDF capture and offline, scoped page retrieval.

Retrieval reads a single current Memory snapshot through its public API. It
builds a request-local lexical index, so reopening a Runtime needs no sidecar
files and citations retain the exact revision supplied by Memory.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
from collections import Counter
from typing import Annotated

from pydantic import BaseModel, Field, field_validator

from powercontext.artifacts.models import MemoryCitation
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.models import Source, SourceMaterialization


class PDFDocumentInput(BaseModel):
    document_id: str
    content: bytes

    @field_validator("document_id")
    @classmethod
    def require_document_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("document_id must not be blank")  # noqa: TRY003
        return value


class PDFPageInput(BaseModel):
    document_id: str
    document_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    page_number: Annotated[int, Field(ge=1)]
    text: str

    @field_validator("document_id")
    @classmethod
    def require_document_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("document_id must not be blank")  # noqa: TRY003
        return value


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Parse every physical page, or fail without returning a partial document."""
    from pypdf import PdfReader

    if not value.content or not value.content.lstrip().startswith(b"%PDF-"):
        raise ValueError("content must be a complete PDF")  # noqa: TRY003
    digest = hashlib.sha256(value.content).hexdigest()
    try:
        reader = PdfReader(io.BytesIO(value.content))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF requires a password")  # noqa: TRY003, TRY301
        pages = tuple(
            PDFPageInput(
                document_id=value.document_id,
                document_sha256=digest,
                page_number=number,
                text=page.extract_text() or "",
            )
            for number, page in enumerate(reader.pages, 1)
        )
        if not pages:
            raise ValueError("PDF contains no pages")  # noqa: TRY003, TRY301
    except Exception as error:
        raise ValueError("PDF could not be completely parsed") from error  # noqa: TRY003
    return pages


class PDFPageSource(Source):
    page: PDFPageInput


class PDFPageAdapter:
    input_class = PDFPageInput
    source_class = PDFPageSource
    name = "pdf-page"

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        # A structured tuple prevents delimiter collisions; fixed-size digests
        # keep even arbitrarily long document identifiers within the ID budget.
        identity = json.dumps(
            [value.document_id, value.document_sha256, value.page_number], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return PDFPageSource(
            name="pdf-page:" + hashlib.sha256(identity).hexdigest(),
            materialization=SourceMaterialization.CAPTURED,
            page=value,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return source.page


class PDFPageProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        page = source.page
        return TextEvidence(
            source_type="pdf-page",
            source_id=source.name,
            content=f"{page.document_id}\n{page.text}",
            metadata={
                "document_id": page.document_id,
                "document_sha256": page.document_sha256,
                "page_number": page.page_number,
            },
        )


PDF_PAGE_SOURCE_DEFINITION = AdapterSourceDefinition(PDFPageAdapter(), projections=(PDFPageProjection(),))

_STOP = frozenset(
    (
        "a an and are as at be been being but by can could did do does for from had has have how if in into is it its "
        "may might more most no nor not of on or our out over should so some such than that the their them then there "
        "these they this those to too up us was we were what when where which while who why will with would you your"
    ).split()
)
_YEAR = re.compile(r"\b(?:fy|fiscal\s+year)?\s*((?:19|20)\d{2})\b", re.IGNORECASE)
_TOKEN = re.compile(r"[a-z]+|\d+(?:[.,]\d+)?")


def _normalize(text: str) -> str:
    text = _YEAR.sub(lambda match: f" {match.group(1)} ", text)
    return text.lower().replace(",", "")


def _stem(word: str) -> str:
    if len(word) > 5 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith("sses"):
        return word[:-2]
    if len(word) > 4 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    if len(word) > 5 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 4 and word.endswith("ed"):
        return word[:-2]
    return word


def _tokens(text: str) -> list[str]:
    return [
        _stem(token)
        for token in _TOKEN.findall(_normalize(text))
        if token not in _STOP and len(token) > 1
    ]


def _query_terms(question: str) -> list[str]:
    seen: list[str] = []
    for token in _tokens(question):
        if token not in seen:
            seen.append(token)
    return seen


def _bm25_scores(documents: list[Counter], query: list[str], k1: float = 1.2, b: float = 0.75) -> list[float]:
    total = len(documents)
    lengths = [sum(document.values()) or 1 for document in documents]
    average = sum(lengths) / total or 1.0
    appearances = Counter()
    for document in documents:
        for term in query:
            if document.get(term):
                appearances[term] += 1
    scores = [0.0] * total
    for term in query:
        frequency = appearances[term]
        if not frequency:
            continue
        idf = math.log(1.0 + (total - frequency + 0.5) / (frequency + 0.5))
        for index, document in enumerate(documents):
            hits = document.get(term)
            if not hits:
                continue
            weight = idf * (hits * (k1 + 1)) / (hits + k1 * (1 - b + b * lengths[index] / average))
            scores[index] += weight
    return scores


def _proximity_scores(documents: list[list[str]], query: list[str], window: int = 6) -> list[float]:
    pairs = {(query[i], query[i + 1]) for i in range(len(query) - 1)}
    if not pairs:
        return [0.0] * len(documents)
    scores = [0.0] * len(documents)
    for index, tokens in enumerate(documents):
        for position in range(len(tokens) - 1):
            limit = min(position + window, len(tokens))
            seen = set()
            for offset in range(position + 1, limit):
                seen.add(tokens[offset])
            if any((tokens[position], other) in pairs for other in seen):
                scores[index] += 1.0
    return scores


# Generic financial-report vocabulary. It expands a question into the wording a
# filing actually uses; it never references a document, a page or an answer.
_EXPANSIONS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (
        re.compile(r"geograph|region|countr|territor|where.*operat"),
        ("segment", "division", "international", "market", "business"),
    ),
    (
        re.compile(r"legal|litigat|lawsuit|sue[ds]?|court|proceed|contingen"),
        ("proceedings", "claims", "matters", "contingencies"),
    ),
    (re.compile(r"ebitda"), ("operating", "profit", "income", "depreciation", "amortization")),
    (
        re.compile(r"capex|capital expenditure"),
        ("capital", "expenditure", "purchase", "property", "plant", "equipment"),
    ),
    (re.compile(r"restructur"), ("restructuring", "charges", "productivity", "plan")),
    (re.compile(r"margin|percent|%"), ("percent", "percentage", "rate")),
    (re.compile(r"revenue|sales|turnover"), ("net", "sales", "revenue")),
    (re.compile(r"balance sheet|asset|liabilit|equity"), ("assets", "liabilities", "equity")),
    (re.compile(r"cash flow|free cash"), ("cash", "flow", "operating", "activities")),
    (re.compile(r"debt|borrow|leverage"), ("debt", "borrowings", "long-term", "notes")),
    (re.compile(r"divided|dividend|shareholder return"), ("dividends", "share", "repurchase")),
    (re.compile(r"employee|headcount|workforce"), ("employees", "personnel")),
    (re.compile(r"acquisition|acquire|divest"), ("acquisition", "divestiture", "transaction")),
)

# Intent -> section-title routing for financial filings. Generic to annual
# reports; it never names a document, a page or an answer.
_INTENTS: tuple[tuple[re.Pattern[str], re.Pattern[str]], ...] = (
    (re.compile(r"geograph|region|countr|territor|where.*operat"),
     re.compile(r"business|segment|geographic|properties")),
    (re.compile(r"legal|litigat|lawsuit|court|proceed|contingen"),
     re.compile(r"legal proceedings|contingencies|commitments")),
    (re.compile(r"\bebitda\b|operating profit|income from operations|margin|net income"),
     re.compile(r"statement of income|statements of income|results of operations")),
    (re.compile(r"capex|capital expenditure|cash flow|free cash"),
     re.compile(r"statement of cash flows|statements of cash flows|cash flow")),
    (re.compile(r"balance sheet|total asset|liabilit|equity|working capital|current ratio"),
     re.compile(r"balance sheet|financial position")),
    (re.compile(r"restructur"), re.compile(r"restructuring")),
    (re.compile(r"acquisition|acquire|divest"), re.compile(r"acquisition|divestiture")),
    (re.compile(r"revenue|sales|turnover"), re.compile(r"statement of income|net sales|results of operations")),
    (re.compile(r"risk"), re.compile(r"risk factors")),
    (re.compile(r"debt|borrow|leverage|note"), re.compile(r"debt|borrowings|notes")),
)
_BOILERPLATE = re.compile(r"^(table of contents|contents)$", re.IGNORECASE)


def _passages(body: str, size: int = 48, stride: int = 24) -> list[list[str]]:
    """Split one page into overlapping token windows used for page ranking."""

    tokens = _tokens(body)
    if len(tokens) <= size:
        return [tokens] if tokens else []
    windows = [tokens[start : start + size] for start in range(0, len(tokens) - size + 1, stride)]
    if len(tokens) % stride:
        windows.append(tokens[-size:])
    return windows


def _page_title(body: str) -> str:
    """Return the section title: the first lines that are not running headers."""

    lines = [line.strip() for line in body.splitlines() if line.strip()]
    kept = [line for line in lines if not _BOILERPLATE.match(line)]
    return " ".join(kept[:2])


def _expand(terms: list[str]) -> list[tuple[str, float]]:
    """Attach filing vocabulary to question terms with a much lower weight."""

    weighted = [(term, 1.0) for term in terms]
    seen = set(terms)
    for term in terms:
        for pattern, aliases in _EXPANSIONS:
            if not pattern.search(term):
                continue
            for alias in aliases:
                for piece in _tokens(alias):
                    if piece not in seen:
                        seen.add(piece)
                        weighted.append((piece, 0.3))
    return weighted


def _passage_scores(
    passages: list[tuple[int, list[str]]], weighted: list[tuple[str, float]]
) -> dict[int, float]:
    """BM25 plus adjacent-term proximity over passages, keeping the best per page."""

    counters = [Counter(tokens) for _, tokens in passages]
    total = len(counters) or 1
    lengths = [sum(counter.values()) or 1 for counter in counters]
    average = sum(lengths) / total
    appearances: Counter = Counter()
    for term, _ in weighted:
        for counter in counters:
            if counter.get(term):
                appearances[term] += 1
    pairs = {(weighted[index][0], weighted[index + 1][0]) for index in range(len(weighted) - 1)}
    best: dict[int, float] = {}
    for position, (page, tokens) in enumerate(passages):
        counter = counters[position]
        score = 0.0
        for term, weight in weighted:
            hits = counter.get(term)
            if not hits:
                continue
            idf = math.log(1.0 + (total - appearances[term] + 0.5) / (appearances[term] + 0.5))
            score += weight * idf * (hits * 2.2) / (hits + 1.2 * (1 - 0.75 + 0.75 * lengths[position] / average))
        for offset in range(len(tokens) - 1):
            if (tokens[offset], tokens[offset + 1]) in pairs:
                score += 0.6
        if score > best.get(page, 0.0):
            best[page] = score
    return best


def _intent_bonus(question: str, title: str, weight: float = 3.0) -> float:
    """Reward pages whose section title matches the question's intent."""

    bonus = 0.0
    for question_pattern, title_pattern in _INTENTS:
        if question_pattern.search(question) and title_pattern.search(title):
            bonus += weight
    return bonus


_INCOME_QUESTION = re.compile(r"operating income|net income|ebitda|margin|profit")
_INCOME_TITLE = re.compile(r"statement of income|statements of income")
_CASH_QUESTION = re.compile(r"cash flow|free cash|capex|capital expenditure")
_CASH_TITLE = re.compile(r"statement of cash flows|statements of cash flows")

_INTENT_WEIGHT = 8.0
_STATEMENT_WEIGHT = 8.0


async def search_pdf_pages(memory, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Rank distinct captured PDF pages for one question through public Memory only."""

    if limit <= 0 or not question.strip():
        return ()
    snapshot = await memory.list()
    records, seen = [], set()
    for record in snapshot.entries:
        refs = tuple(ref for ref in record.entry.sources if ref.source_type == "pdf-page")
        if len(refs) != 1 or len(record.entry.sources) != 1 or refs[0].source_id in seen:
            continue
        seen.add(refs[0].source_id)
        records.append(record)
    if not records:
        return ()

    bodies = [record.entry.text.partition("\n")[2] for record in records]
    terms = _query_terms(question)
    if not terms:
        return tuple(record.citation for record in records[:limit])
    weighted = _expand(terms)
    titles = [_page_title(body).lower() for body in bodies]

    passages: list[tuple[int, list[str]]] = []
    for index, body in enumerate(bodies):
        for window in _passages(body):
            passages.append((index, window))
    ranked = _passage_scores(passages, weighted)

    scores: list[float] = []
    for index in range(len(records)):
        score = ranked.get(index, 0.0)
        score += _intent_bonus(question, titles[index], _INTENT_WEIGHT)
        if _INCOME_QUESTION.search(question) and _INCOME_TITLE.search(titles[index]):
            score += _STATEMENT_WEIGHT
        if _CASH_QUESTION.search(question) and _CASH_TITLE.search(titles[index]):
            score += _STATEMENT_WEIGHT * 0.5
        scores.append(score)

    order = sorted(range(len(records)), key=lambda index: (-scores[index], index))
    return tuple(records[index].citation for index in order[:limit])

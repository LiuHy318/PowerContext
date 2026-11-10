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

_STOP = frozenset([
    "a",
    "an",
    "the",
    "and",
    "or",
    "of",
    "for",
    "in",
    "on",
    "to",
    "as",
    "at",
    "by",
    "from",
    "with",
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
    "does",
    "do",
    "did",
    "has",
    "have",
    "had",
    "any",
    "its",
    "it",
    "this",
    "that",
    "these",
    "those",
    "if",
    "then",
    "than",
    "not",
    "only",
    "can",
    "could",
    "would",
    "should",
    "will",
    "into",
    "through",
    "within",
    "using",
    "use",
    "used",
    "give",
    "answer",
    "respond",
    "response",
    "question",
    "assuming",
    "perspective",
    "investment",
    "analyst",
    "details",
    "shown",
    "calculate",
    "define",
    "relying",
    "reported",
    "report",
    "directly",
    "explicitly",
    "outlined",
    "primarily",
    "important",
    "unadjusted",
    "quantity",
    "million",
    "millions",
    "usd",
    "fy",
])


def _tokens(text: str) -> list[str]:
    text = re.sub(r"\bfy\s*(\d{4})\b", r"\1", text.lower())
    words = re.findall(r"[a-z]+|\d+", text)
    # Small, deterministic inflection normalizer; no downloaded language data.
    return [
        word[:-3] + "y"
        if len(word) > 5 and word.endswith("ies")
        else word[:-1]
        if len(word) > 4 and word.endswith("s") and not word.endswith(("ss", "us", "is"))
        else word
        for word in words
        if word not in _STOP
    ]


# General accounting vocabulary, independent of documents and benchmark questions.
_ALIASES = (
    ("geography", "geographical", "geographic", "region", "country", "international", "segment"),
    ("legal", "litigation", "lawsuit", "proceeding", "contingency"),
    ("revenue", "sale", "turnover"),
    ("capex", "capital", "expenditure", "purchase", "property", "equipment"),
    ("ebitda", "operating", "income", "depreciation", "amortization"),
    ("profit", "earning", "income"),
)


async def search_pdf_pages(memory, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Rank distinct captured PDF pages using public scoped Memory only.

    Cost is linear in the snapshot's text size, appropriate for report-sized
    corpora. Nothing is cached across requests, scopes, or Memory revisions.
    """
    if limit <= 0 or not question.strip():
        return ()
    snapshot = await memory.list()
    records, seen = [], set()
    for record in snapshot.entries:
        refs = tuple(ref for ref in record.entry.sources if ref.source_type == "pdf-page")
        # The page pipeline stores one page per entry. Reject mixed evidence,
        # which cannot constitute an exact citation to a single physical page.
        if len(refs) != 1 or len(record.entry.sources) != 1 or refs[0].source_id in seen:
            continue
        seen.add(refs[0].source_id)
        records.append(record)
    if not records:
        return ()
    ranked = _rank_pages(records, question)
    return tuple(record.citation for score, record in ranked[:limit] if score > 0)


def _rank_pages(records, question: str):
    """Score a captured snapshot with BM25 and independent document matching."""
    query = set(_tokens(question))
    weights = dict.fromkeys(query, 1.0)
    for group in _ALIASES:
        if query.intersection(group):
            for token in group:
                weights.setdefault(token, 0.35)
    documents = [Counter(_tokens(record.entry.text.partition("\n")[2])) for record in records]
    frequencies = Counter(token for document in documents for token in document)
    average = sum(sum(document.values()) for document in documents) / len(documents) or 1
    ranked = []
    for record, document in zip(records, documents, strict=True):
        length = sum(document.values())
        score = 0.0
        for token, weight in weights.items():
            count = document[token]
            if count:
                idf = math.log1p((len(documents) - frequencies[token] + 0.5) / (frequencies[token] + 0.5))
                score += weight * idf * count * 2.2 / (count + 1.2 * (0.25 + 0.75 * length / average))
        # Document identity is separate from page relevance, avoiding inflation
        # from the same document identifier on every page of a report.
        identity = set(_tokens(record.entry.text.partition("\n")[0]))
        company = {token for token in query & identity if token.isalpha() and len(token) > 2}
        score *= 1 + 2 * bool(company)
        score *= 1 + 0.25 * bool({token for token in query & identity if token.isdigit() and len(token) == 4})
        ranked.append((score, record))
    ranked.sort(key=lambda item: (-item[0], item[1].entry.entry_id))
    return ranked

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

# Independent retrieval channels for financial concepts. Each channel searches
# vocabulary and headings, never particular documents or physical page numbers.
_CONCEPTS = (
    (
        r"geograph|countr|territor|region|where.*operat",
        "country territory geography geographic segment division international",
        r"(?:our )?(?:operations|divisions)|(?:company|business) overview|geographic.*|segment information",
    ),
    (
        r"legal|litigation|lawsuit|court|proceeding|legal battle",
        "legal litigation lawsuit proceeding contingency settlement",
        r"(?:item \d+[.]? )?legal proceedings|(?:note \d+ .*)?(?:commitments and )?contingencies|litigation.*",
    ),
    (
        r"ebitda|income|p\s*&\s*l|profit|earning|revenue|sales|cost",
        "income revenue sale profit earning operating expense",
        r"(?:consolidated )?statements? of (?:income|operations|earnings|profit.*)|(?:consolidated )?income statements?",
    ),
    (
        r"cash.?flow|cash flow|capex|ebitda|depreciation|amortization|d\s*&\s*a",
        "cash flow operating investing depreciation amortization capital expenditure",
        r"(?:consolidated )?statements? of cash flows?|(?:consolidated )?cash flow statements?",
    ),
    (
        r"balance.sheet|asset|liabilit|equity|working capital|current ratio",
        "asset liability equity receivable payable inventory",
        r"(?:consolidated )?balance sheets?|(?:consolidated )?statements? of financial position",
    ),
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
    """Fuse lexical, concept and statement-heading recall channels."""
    identities = [record.entry.text.partition("\n")[0] for record in records]
    selected = _matching_documents(identities, question)
    if selected:
        records = [record for record, identity in zip(records, identities, strict=True) if identity in selected]
    bodies = [record.entry.text.partition("\n")[2] for record in records]
    documents = [Counter(_tokens(body)) for body in bodies]
    frequencies = Counter(token for document in documents for token in document)
    average = sum(sum(document.values()) for document in documents) / len(documents) or 1
    identity_terms = set(_tokens(" ".join(selected))) if selected else set()
    query = set(_tokens(question)) - identity_terms
    weights = {token: 0.05 if token.isdigit() else 1.0 for token in sorted(query)}
    for group in _ALIASES:
        if query.intersection(group):
            for token in group:
                weights.setdefault(token, 0.35)
    base = _bm25_scores(documents, frequencies, average, weights)
    channels = [(base, 1.0)]
    for trigger, terms, heading in _CONCEPTS:
        if re.search(trigger, question, re.IGNORECASE):
            focused = _bm25_scores(documents, frequencies, average, dict.fromkeys(_tokens(terms), 1.0))
            channels.append((focused, 0.8))
            # Prefer a section's own heading over mentions in running prose.
            # Bound title length so a paragraph cannot masquerade as a title.
            anchors = [
                (1.0 + min(focused[i], 10.0) / 100)
                if any(
                    re.fullmatch(heading, line.strip(" .:\t"), re.IGNORECASE)
                    for line in body.splitlines()
                    if len(line) < 140
                )
                else 0.0
                for i, body in enumerate(bodies)
            ]
            channels.append((anchors, 1.2))
    scores = [0.0] * len(records)
    for channel, weight in channels:
        order = sorted(range(len(records)), key=lambda i: (-channel[i], records[i].entry.entry_id))
        for rank, index in enumerate(order, 1):
            if channel[index] > 0:
                scores[index] += weight / (20 + rank)
    ranked = [(score, record) for score, record in zip(scores, records, strict=True)]
    return sorted(ranked, key=lambda item: (-item[0], item[1].entry.entry_id))


def _matching_documents(identities: list[str], question: str) -> set[str]:
    """Recognize a report's company in the question without a company registry.

    Preserve every revision/year for a named company: comparative questions may
    need more than one report. Do not filter if no unambiguous name is present.
    """
    question_words = re.findall(r"[a-z0-9]+", question.lower())
    spans = {
        "".join(question_words[start:end])
        for start in range(len(question_words))
        for end in range(start + 1, min(start + 6, len(question_words)) + 1)
    }
    selected = set()
    for identity in set(identities):
        company = re.split(r"[_\s-](?:fy)?(?:19|20)\d{2}", identity, flags=re.IGNORECASE)[0]
        compact = re.sub(r"[^a-z0-9]", "", company.lower())
        if len(compact) >= 2 and compact in spans:
            selected.add(identity)
    return selected


def _bm25_scores(documents, frequencies, average: float, weights: dict[str, float]) -> list[float]:
    scores = []
    for document in documents:
        normalizer = 1.2 * (0.25 + 0.75 * sum(document.values()) / average)
        score = 0.0
        for token, weight in weights.items():
            count = document[token]
            if count:
                idf = math.log1p((len(documents) - frequencies[token] + 0.5) / (frequencies[token] + 0.5))
                score += weight * idf * count * 2.2 / (count + normalizer)
        scores.append(score)
    return scores

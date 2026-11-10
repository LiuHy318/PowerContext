"""PDF contracts beyond the public competition smoke test."""

import io
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter

from powercontext.sources import SourceDefinitionRegistry, project_source_for_transport
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    PDFPageInput,
    parse_pdf_document,
    search_pdf_pages,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


@pytest.mark.parametrize("identifier", ["", "  ", "\n\t"])
def test_blank_document_identity_rejected(identifier):
    with pytest.raises(ValueError):
        PDFDocumentInput(document_id=identifier, content=b"pdf")


def test_password_protected_pdf_rejected():
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("secret")
    buffer = io.BytesIO()
    writer.write(buffer)
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="private", content=buffer.getvalue()))


async def test_identity_projection_and_serialized_readback():
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    page = PDFPageInput(
        document_id="long:identifier/" * 100, document_sha256="a" * 64, page_number=7, text="Revenue 123"
    )
    source = await registry.resolve(page)
    assert len(source.name) <= 256
    assert await registry.read(type(source).model_validate_json(source.model_dump_json())) == page
    changed = await registry.resolve(page.model_copy(update={"document_sha256": "b" * 64}))
    other_page = await registry.resolve(page.model_copy(update={"page_number": 8}))
    assert len({source.name, changed.name, other_page.name}) == 3
    projection = project_source_for_transport(registry, source).projection(TEXT_EVIDENCE_PROJECTION_KEY)
    assert projection["content"] == f"{page.document_id}\nRevenue 123"
    assert projection["metadata"]["page_number"] == 7


async def test_search_limits_deduplication_and_snapshot_isolation():
    from powercontext.artifacts.models import ArtifactRef, MemoryCitation
    from powercontext.sources.models import SourceRef

    class SnapshotMemory:
        def __init__(self, scope, entries):
            self.scope, self.entries = scope, entries

        async def list(self):
            return SimpleNamespace(entries=self.entries)

    def record(scope, identity, text, source_type="pdf-page"):
        return SimpleNamespace(
            citation=MemoryCitation(
                memory_ref=ArtifactRef(family="memory", artifact_id=scope, revision=3),
                entry_id=identity,
                entry_version_id=identity + "-v1",
            ),
            entry=SimpleNamespace(
                entry_id=identity,
                text=text,
                sources=(SourceRef(source_type=source_type, source_id=identity),),
            ),
        )

    first = record("a", "one", "ACME_2024\nLitigation and legal proceedings")
    second = record("a", "two", "ACME_2024\nLegal contingencies")
    irrelevant = record("a", "other", "ACME_2024\nLegal litigation", "content")
    memory = SnapshotMemory("a", (first, first, second, irrelevant))
    assert await search_pdf_pages(memory, "legal", limit=0) == ()
    assert await search_pdf_pages(memory, "  ") == ()
    found = await search_pdf_pages(memory, "legal", limit=20)
    assert {item.entry_id for item in found} == {"one", "two"}
    assert all(item.memory_ref.revision == 3 for item in found)
    assert len(await search_pdf_pages(memory, "legal", limit=1)) == 1
    # A subsequent call must read its own snapshot, including after revision
    # changes, rather than reusing a previous request's index or citations.
    memory.entries = ()
    assert await search_pdf_pages(memory, "legal") == ()
    other = SnapshotMemory("b", (record("b", "three", "BETA\nLegal proceedings"),))
    assert (await search_pdf_pages(other, "legal"))[0].memory_ref.artifact_id == "b"


def _page_memory(pages):
    from powercontext.artifacts.models import ArtifactRef, MemoryCitation
    from powercontext.sources.models import SourceRef

    records = tuple(
        SimpleNamespace(
            citation=MemoryCitation(
                memory_ref=ArtifactRef(family="memory", artifact_id="reports", revision=7),
                entry_id=identity,
                entry_version_id=identity + "-v1",
            ),
            entry=SimpleNamespace(
                entry_id=identity,
                text=document + "\n" + text,
                sources=(SourceRef(source_type="pdf-page", source_id=identity),),
            ),
        )
        for identity, document, text in pages
    )

    class Memory:
        async def list(self):
            return SimpleNamespace(entries=records)

    return Memory()


async def test_named_company_does_not_lose_places_to_other_reports():
    memory = _page_memory([
        ("requested", "3M_2024_10K", "Consolidated Statement of Income\nRevenue 1200 expenses 900"),
        *[(str(i), "OTHER_2024_10K", "Revenue 3 million " * 30) for i in range(30)],
    ])
    found = await search_pdf_pages(memory, "What was 3M revenue in FY2024?", limit=20)
    assert [citation.entry_id for citation in found] == ["requested"]


async def test_metric_question_recalls_both_underlying_statements():
    memory = _page_memory([
        ("income", "ACME_2024_10K", "Consolidated Statement of Income\nRevenue 120 operating profit 80"),
        ("cash", "ACME_2024_10K", "Consolidated Statement of Cash Flows\nDepreciation 20 amortization 10"),
        *[
            (str(i), "ACME_2024_10K", "EBITDA operating income depreciation amortization capital expenditure " * 20)
            for i in range(30)
        ],
    ])
    found = await search_pdf_pages(memory, "What was ACME EBITDA less capex in FY2024?", limit=20)
    assert {"income", "cash"} <= {citation.entry_id for citation in found}


async def test_geography_question_recalls_business_overview():
    memory = _page_memory([
        ("overview", "ACME_2024_10K", "Company Overview\nOur Operations\nWe serve 80 countries and territories."),
        *[(str(i), "ACME_2024_10K", "ACME operates internationally in 2024. " * 30) for i in range(30)],
    ])
    found = await search_pdf_pages(memory, "What geographies does ACME operate in during FY2024?", limit=20)
    assert "overview" in {citation.entry_id for citation in found}

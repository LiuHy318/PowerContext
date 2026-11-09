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

"""Row-level CSV Source: one captured Source per primary-keyed record.

Each physical row of an offline CSV document is resolved into an independent
:class:`CSVRowSource`. The row identity is derived solely from the document id
and the primary-key value, so reordering rows or editing non-key columns keeps
the same stable identity while the projected content reflects the edit. Field
values, column names and column order are preserved verbatim; no trimming,
numeric, date or boolean coercion is applied.
"""

from __future__ import annotations

import csv
import io
import json
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    AdapterSourceDefinition,
    Source,
    SourceMaterialization,
)
from powercontext.sources.projections import TextEvidence

CSV_ROW_SOURCE_NAME = "csv-row"
_SOURCE_ID_PREFIX = "csv"


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Build the stable, delimiter-safe Source identity for one CSV row.

    Both components are percent-encoded with :func:`urllib.parse.quote` so a
    literal ``:`` inside either component cannot collide with the separator.
    ``document_id`` must already be trimmed (no surrounding whitespace); the
    primary-key value is kept verbatim, including surrounding spaces.
    """

    if not isinstance(document_id, str) or not document_id or document_id != document_id.strip():
        raise ValueError("document_id must be a non-empty string without surrounding whitespace")  # noqa: TRY003
    if not isinstance(primary_key_value, str) or not primary_key_value or not primary_key_value.strip():
        raise ValueError("primary_key_value must not be empty or whitespace-only")  # noqa: TRY003
    source_id = f"{_SOURCE_ID_PREFIX}:{quote(document_id, safe='')}:{quote(primary_key_value, safe='')}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"row source_id exceeds {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return source_id


class CSVDocumentInput(BaseModel):
    """A complete offline CSV document to parse into per-row inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    """One resolved CSV row ready to be captured as a Source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


class CSVRowSource(Source):
    """A captured CSV row: verbatim columns/values plus its identity components."""

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @property
    def source_id(self) -> str:
        """The stable row identity, identical to the Source's ``name``."""

        return self.name


class CSVRowSourceAdapter:
    """Resolve a :class:`CSVRowInput` into a captured :class:`CSVRowSource`."""

    input_class = CSVRowInput
    name = CSV_ROW_SOURCE_NAME
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        return CSVRowSource(
            name=csv_row_source_id(value.document_id, value.primary_key_value),
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


class CSVRowTextEvidenceProjection:
    """Project a CSV row as compact, order-preserving text evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row = dict(zip(source.columns, source.values, strict=False))
        content = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        return TextEvidence(
            source_type=CSV_ROW_SOURCE_NAME,
            source_id=source.name,
            content=content,
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


CSV_ROW_SOURCE_ADAPTER = CSVRowSourceAdapter()
CSV_ROW_SOURCE_DEFINITION = AdapterSourceDefinition(
    CSV_ROW_SOURCE_ADAPTER,
    projections=(CSVRowTextEvidenceProjection(),),
)


def _decode_and_parse(content: bytes) -> list[list[str]]:
    try:
        text = content.decode("utf-8-sig")
    except (UnicodeDecodeError, UnicodeError) as error:
        raise ValueError(f"CSV document is not valid UTF-8: {error}") from error  # noqa: TRY003
    try:
        return list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error as error:
        raise ValueError(f"malformed CSV document: {error}") from error  # noqa: TRY003


def _validate_header(header: list[str], primary_key: str) -> tuple[int, tuple[str, ...]]:
    if not header or any(not column or not column.strip() for column in header):
        raise ValueError("CSV header must contain non-empty, non-blank column names")  # noqa: TRY003
    if len(set(header)) != len(header):
        raise ValueError("CSV header column names must be unique")  # noqa: TRY003
    if primary_key not in header:
        raise ValueError(f"primary_key {primary_key!r} is not a column in the CSV header")  # noqa: TRY003
    return header.index(primary_key), tuple(header)


def _build_rows(
    records: list[list[str]],
    *,
    document_id: str,
    primary_key: str,
    pk_index: int,
    width: int,
    columns: tuple[str, ...],
) -> list[CSVRowInput]:
    rows: list[CSVRowInput] = []
    seen_pks: set[str] = set()
    for record in records:
        # A completely blank physical record parses as an empty row; skip it.
        if len(record) == 0:
            continue
        if len(record) != width:
            raise ValueError(f"CSV row has {len(record)} fields but the header has {width}")  # noqa: TRY003
        primary_key_value = record[pk_index]
        if not primary_key_value or not primary_key_value.strip():
            raise ValueError("CSV primary-key value must not be empty or whitespace-only")  # noqa: TRY003
        if primary_key_value in seen_pks:
            raise ValueError(f"duplicate primary-key value {primary_key_value!r}")  # noqa: TRY003
        seen_pks.add(primary_key_value)
        rows.append(
            CSVRowInput(
                document_id=document_id,
                primary_key=primary_key,
                primary_key_value=primary_key_value,
                columns=columns,
                values=tuple(record),
            )
        )
    return rows


def parse_csv_document(document: CSVDocumentInput, /) -> tuple[CSVRowInput, ...]:
    """Parse a complete offline CSV document into per-row inputs.

    The whole document is decoded, parsed and validated atomically: any
    encoding, CSV-format or data error raises :class:`ValueError` and no rows
    are returned. The first physical record is the header; completely blank
    physical records after the header are ignored.
    """

    document_id = document.document_id
    primary_key = document.primary_key

    if not isinstance(document_id, str) or not document_id or document_id != document_id.strip():
        raise ValueError("document_id must be a non-empty string without surrounding whitespace")  # noqa: TRY003
    if not isinstance(primary_key, str) or not primary_key or not primary_key.strip():
        raise ValueError("primary_key must be a non-empty string")  # noqa: TRY003

    all_rows = _decode_and_parse(document.content)
    if not all_rows:
        raise ValueError("CSV document is empty: no header record")  # noqa: TRY003

    pk_index, columns = _validate_header(all_rows[0], primary_key)
    rows = _build_rows(
        all_rows[1:],
        document_id=document_id,
        primary_key=primary_key,
        pk_index=pk_index,
        width=len(columns),
        columns=columns,
    )
    return tuple(rows)


__all__ = [
    "CSV_ROW_SOURCE_ADAPTER",
    "CSV_ROW_SOURCE_DEFINITION",
    "CSV_ROW_SOURCE_NAME",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "CSVRowSourceAdapter",
    "CSVRowTextEvidenceProjection",
    "csv_row_source_id",
    "parse_csv_document",
]

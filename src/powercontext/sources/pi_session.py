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

"""Pi session branch ingestion: parse a Pi v3 JSONL session into the visible
events on the selected leaf branch, expose each as a captured ``pi-event``
Source, and incrementally import only unseen branch events with durable
checkpoint / restart / branch-switch support.

The parser walks the parent graph for the chosen leaf and keeps the visible
events on the root-to-leaf chain. Control nodes (model changes, thinking-level
changes, thinking/image blocks) stay in the graph but produce no text evidence.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    AdapterSourceDefinition,
    Source,
    SourceMaterialization,
)
from powercontext.sources.connectors import (
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
)
from powercontext.sources.projections import TextEvidence

PI_EVENT_SOURCE_NAME = "pi-event"
PI_CONNECTOR_NAME = "pi-session-history"


def pi_event_source_id(session_id: str, event_id: str) -> str:
    """Stable identity derived only from the session and event ids."""

    if not isinstance(session_id, str) or not session_id or session_id != session_id.strip():
        raise ValueError("session_id must be a non-empty trimmed string")  # noqa: TRY003
    if not isinstance(event_id, str) or not event_id or event_id != event_id.strip():
        raise ValueError("event_id must be a non-empty trimmed string")  # noqa: TRY003
    source_id = f"{PI_EVENT_SOURCE_NAME}:{quote(session_id, safe='')}:{quote(event_id, safe='')}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"pi-event source_id exceeds {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return source_id


class PiSessionInput(BaseModel):
    """A raw Pi v3 JSONL session and the optional leaf branch to select."""

    model_config = ConfigDict(frozen=True)

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible event on the selected branch, with its full input record."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, Any]


class PiEventSource(Source):
    """A captured Pi event: identity plus the full event and raw record."""

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, Any]

    @property
    def source_id(self) -> str:
        return self.name


class PiEventSourceAdapter:
    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        return PiEventSource(
            name=pi_event_source_id(value.session_id, value.event_id),
            materialization=SourceMaterialization.CAPTURED,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


class PiEventTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.name,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


PI_EVENT_SOURCE_ADAPTER = PiEventSourceAdapter()
PI_EVENT_SOURCE_DEFINITION = AdapterSourceDefinition(
    PI_EVENT_SOURCE_ADAPTER,
    projections=(PiEventTextEvidenceProjection(),),
)


# ---------------------------------------------------------------------------
# Session parsing
# ---------------------------------------------------------------------------


def _visible_text(content: Any) -> str:
    """Join visible string/text-block content with newlines."""

    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
    return "\n".join(parts)


def _compact_arguments(arguments: Any) -> str:
    return json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _message_body(message: dict[str, Any]) -> str:
    role = message.get("role")
    content = message.get("content")
    if role == "toolResult":
        text = _visible_text(content)
        return f"[tool_result {message.get('toolName')} {message.get('toolCallId')}] {text}"
    if role == "bashExecution":
        return f"$ {message.get('command')}\n{message.get('output')}"
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                parts.append(block.get("text", ""))
            elif kind == "toolCall":
                args = _compact_arguments(block.get("arguments", {}))
                parts.append(f"[tool_call {block.get('name')} {block.get('id')}] {args}")
            # thinking / image blocks contribute no evidence
    return "\n".join(parts)


def _visible_event(record: dict[str, Any], session_id: str) -> PiEventInput | None:
    rtype = record.get("type")
    body: str | None = None
    role: str | None = None
    if rtype == "compaction":
        role = "compaction"
        body = record.get("summary", "")
    elif rtype == "branch_summary":
        role = "branch_summary"
        body = record.get("summary", "")
    elif rtype == "custom_message":
        role = "custom"
        body = _visible_text(record.get("content"))
    elif rtype == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        role = message.get("role", "")
        body = _message_body(message)
    else:
        # control node (model_change, thinking_level_change, ...): graph only
        return None
    if not isinstance(body, str) or not body.strip():
        return None
    return PiEventInput(
        session_id=session_id,
        event_id=record["id"],
        parent_id=record.get("parentId"),
        event_type=rtype,
        role=role,
        content=f"{role}: {body}",
        record=record,
    )


def _decode_records(content: bytes) -> list[dict[str, Any]]:
    try:
        text = content.decode("utf-8-sig")
    except (UnicodeDecodeError, UnicodeError) as error:
        raise ValueError(f"pi session is not valid UTF-8: {error}") from error  # noqa: TRY003

    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"pi session contains invalid JSON: {error}") from error  # noqa: TRY003
        records.append(record)
    return records


def _build_graph(
    records: list[dict[str, Any]],
) -> tuple[str, dict[str, dict[str, Any]], list[dict[str, Any]]]:
    if not records or not isinstance(records[0], dict):
        raise ValueError("pi session must start with a session header record")  # noqa: TRY003
    header = records[0]
    if (
        header.get("type") != "session"
        or header.get("version") != 3
        or not isinstance(header.get("id"), str)
        or not header["id"]
    ):
        raise ValueError("pi session header must be type=session, version=3 with a non-empty id")  # noqa: TRY003
    session_id = header["id"]

    nodes: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for record in records[1:]:
        if not isinstance(record, dict):
            raise ValueError("pi session records must be JSON objects")  # noqa: TRY003, TRY004
        rid = record.get("id")
        if not isinstance(rid, str) or not rid:
            raise ValueError("pi session event records must have a non-empty string id")  # noqa: TRY003
        if rid in nodes:
            raise ValueError(f"duplicate pi session event id {rid!r}")  # noqa: TRY003
        pid = record.get("parentId")
        if pid is not None and pid not in nodes:
            raise ValueError(f"pi session event {rid!r} references missing parent {pid!r}")  # noqa: TRY003
        nodes[rid] = record
        ordered.append(record)
    return session_id, nodes, ordered


def _walk_chain(leaf: str, nodes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    cur: str | None = leaf
    while cur is not None:
        if cur in seen:
            raise ValueError("pi session parent graph contains a cycle")  # noqa: TRY003
        seen.add(cur)
        node = nodes[cur]
        chain.append(node)
        cur = node.get("parentId")
    chain.reverse()
    return chain


def parse_pi_session(inp: PiSessionInput, /) -> tuple[PiEventInput, ...]:
    """Parse a Pi v3 JSONL archive into the visible events on the selected branch.

    The whole archive is decoded and validated atomically; any encoding, JSON,
    graph or leaf error raises :class:`ValueError` with no partial result.
    """

    records = _decode_records(inp.content)
    session_id, nodes, ordered = _build_graph(records)

    if inp.leaf_id is not None:
        if not isinstance(inp.leaf_id, str) or inp.leaf_id not in nodes:
            raise ValueError(f"pi session leaf {inp.leaf_id!r} not found")  # noqa: TRY003
        leaf = inp.leaf_id
    elif ordered:
        leaf = ordered[-1]["id"]
    else:
        return ()

    chain = _walk_chain(leaf, nodes)
    events: list[PiEventInput] = []
    for record in chain:
        event = _visible_event(record, session_id)
        if event is not None:
            events.append(event)
    return tuple(events)


# ---------------------------------------------------------------------------
# Incremental connector
# ---------------------------------------------------------------------------


def _fingerprint(record: dict[str, Any]) -> str:
    canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PiSessionConnector:
    """Incrementally import visible events on the selected branch.

    The file is fully parsed and the branch selected on every run. Already
    imported events (across branch switches and restarts) are verified against
    their stored fingerprint and skipped; unseen events are submitted. The
    checkpoint is an opaque JSON value persisted by ``ConnectorLifecycle``.
    """

    name = PI_CONNECTOR_NAME
    version = "1"
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        content = self._path.read_bytes()
        events = parse_pi_session(PiSessionInput(content=content, leaf_id=self._leaf_id))

        previous = session.checkpoint
        imported: dict[str, str] = {}
        if isinstance(previous, dict):
            raw = previous.get("events")
            if isinstance(raw, dict):
                imported = {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}

        for event in events:
            fingerprint = _fingerprint(event.record)
            known = imported.get(event.event_id)
            if known is not None:
                if known != fingerprint:
                    raise ValueError(  # noqa: TRY003
                        f"pi session event {event.event_id!r} was modified after import"
                    )
                continue
            await session.submit(event.event_id, PI_EVENT_SOURCE_NAME, event)
            imported[event.event_id] = fingerprint

        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint={"events": imported},
        )


__all__ = [
    "PI_CONNECTOR_NAME",
    "PI_EVENT_SOURCE_ADAPTER",
    "PI_EVENT_SOURCE_DEFINITION",
    "PI_EVENT_SOURCE_NAME",
    "PiEventInput",
    "PiEventSource",
    "PiEventSourceAdapter",
    "PiEventTextEvidenceProjection",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_id",
]

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

"""Source lifecycle: propagate source availability through evidence dependencies.

A scope registers Sources, Memories and Topic Memories together with their parents
and an ``all``/``any`` support policy. Applying a withdrawal or restore event on a
Source propagates availability down the DAG, deactivates the native Memory entries
that lost their support, keeps Topic Memories out of search and context, and
restores both when the source comes back. Registrations, events and derived
availability are persisted so a restarted runtime sees the same graph.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import text

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.sources.models import SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import BuiltinRuntime

__all__ = [
    "SourceLifecycleApplication",
    "SourceLifecycleError",
    "SourceLifecycleEvent",
    "ensure_source_lifecycle_schema",
]

_KIND_SOURCE = "source"
_KIND_MEMORY = "memory"
_KIND_TOPIC = "topic"
_POLICY_ALL = "all"
_POLICY_ANY = "any"
_PURPOSE_GENERATION = "generation"
_PURPOSE_AUDIT = "audit"
_TABLE = "pc_source_lifecycle"


class SourceLifecycleError(ValueError):
    """Raised when a lifecycle registration, event or lookup violates its contract."""


class SourceLifecycleEvent(BaseModel):
    """One ordered availability event for a registered Source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    reason: str
    revision: int
    sequence: int
    available: bool

    @field_validator("node_id", "reason")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("revision", "sequence")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("value must be positive")
        return value


async def ensure_source_lifecycle_schema(connection: Any) -> None:
    """Create the lifecycle table when the runtime bootstraps its database."""

    await connection.exec_driver_sql(
        f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
        "scope_id VARCHAR(191) NOT NULL, "
        "node_id VARCHAR(191) NOT NULL, "
        "kind VARCHAR(16) NOT NULL, "
        "policy VARCHAR(8) NOT NULL, "
        "parents TEXT NOT NULL, "
        "revision INTEGER NULL, "
        "sequence INTEGER NULL, "
        "available INTEGER NOT NULL, "
        "body TEXT NOT NULL, "
        f"PRIMARY KEY (scope_id, node_id))"  # noqa: S608 - constant identifiers
    )


@dataclass(frozen=True, slots=True)
class _Node:
    node_id: str
    kind: str
    policy: str
    parents: tuple[str, ...]
    revision: int | None
    sequence: int | None
    available: bool
    body: dict[str, Any]


def _reference(node: _Node) -> SourceRef | MemoryCitation | ArtifactRef:
    if node.kind == _KIND_SOURCE:
        return SourceRef.model_validate(node.body)
    if node.kind == _KIND_MEMORY:
        return MemoryCitation.model_validate(node.body)
    return ArtifactRef.model_validate(node.body)


class ScopedSourceLifecycle:
    """Register an evidence DAG and propagate source availability inside one scope."""

    def __init__(self, runtime: BuiltinRuntime, database: AsyncDatabase, scope_id: str) -> None:
        self._runtime = runtime
        self._database = database
        self.scope_id = scope_id

    # ------------------------------------------------------------------ registration

    async def register_source(self, node_id: str, source_ref: SourceRef, *, revision: int) -> None:
        if revision <= 0:
            raise SourceLifecycleError("revision must be positive")
        await self._validate_source_reference(source_ref)
        node = _Node(node_id, _KIND_SOURCE, _POLICY_ALL, (), revision, None, True,
                     source_ref.model_dump(mode="json"))
        await self._register(node)

    async def _validate_source_reference(self, source_ref: SourceRef) -> None:
        async with self._runtime._context(self.scope_id) as context:
            entries = await context.sources.journal.entries()
        if not any(entry.source_ref == source_ref for entry in entries):
            raise SourceLifecycleError("source-reference-unreadable")

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        *,
        parents: tuple[str, ...],
        policy: str = _POLICY_ALL,
    ) -> None:
        node = _Node(node_id, _KIND_MEMORY, _policy(policy), _parents(parents), None, None, True,
                     citation.model_dump(mode="json"))
        await self._register(node, validate=self._validate_memory_reference)

    async def register_topic(
        self,
        node_id: str,
        reference: ArtifactRef,
        *,
        parents: tuple[str, ...],
        policy: str = _POLICY_ALL,
    ) -> None:
        node = _Node(node_id, _KIND_TOPIC, _policy(policy), _parents(parents), None, None, True,
                     reference.model_dump(mode="json"))
        await self._register(node, validate=self._validate_topic_reference)

    async def _register(self, node: _Node, *, validate: Any = None) -> None:
        nodes = await self._load()
        existing = nodes.get(node.node_id)
        if existing is not None:
            if _same_binding(existing, node):
                return
            raise SourceLifecycleError("conflicting-registration")
        if node.kind != _KIND_SOURCE:
            self._check_parents(node, nodes)
        availability = _evaluate(node, nodes)
        if validate is not None:
            await validate(node)
        stored = _Node(node.node_id, node.kind, node.policy, node.parents, node.revision, node.sequence,
                       availability, node.body)
        await self._upsert((stored,))
        if node.kind == _KIND_MEMORY and not availability:
            await self._set_memory_state(stored, available=False)

    async def _validate_memory_reference(self, node: _Node) -> None:
        citation = MemoryCitation.model_validate(node.body)
        async with self._runtime._context(self.scope_id) as context:
            try:
                await context.artifacts.memory.validate_citation(citation)
            except Exception as error:  # noqa: BLE001
                raise SourceLifecycleError("memory-reference-unreadable") from error

    async def _validate_topic_reference(self, node: _Node) -> None:
        reference = ArtifactRef.model_validate(node.body)
        try:
            await self._runtime.records.for_scope(self.scope_id).get_artifact_revision(
                reference.family, reference.artifact_id, reference.revision
            )
        except Exception as error:  # noqa: BLE001
            raise SourceLifecycleError("topic-reference-unreadable") from error

    def _check_parents(self, node: _Node, nodes: dict[str, _Node]) -> None:
        if not node.parents:
            raise SourceLifecycleError("missing-parents")
        if len(set(node.parents)) != len(node.parents):
            raise SourceLifecycleError("duplicate-parent")
        if node.node_id in node.parents:
            raise SourceLifecycleError("self-reference")
        seen: set[str] = set()
        stack = list(node.parents)
        while stack:
            parent = stack.pop()
            if parent == node.node_id:
                raise SourceLifecycleError("cycle")
            known = nodes.get(parent)
            if known is None:
                raise SourceLifecycleError("unknown-parent")
            if parent in seen:
                continue
            seen.add(parent)
            stack.extend(known.parents)

    # ------------------------------------------------------------------ events

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        nodes = await self._load()
        source = nodes.get(event.node_id)
        if source is None or source.kind != _KIND_SOURCE:
            raise SourceLifecycleError("unknown-source")
        if source.revision != event.revision:
            raise SourceLifecycleError("revision-mismatch")
        if source.sequence is not None:
            if event.sequence < source.sequence:
                raise SourceLifecycleError("stale-sequence")
            if event.sequence == source.sequence:
                if source.available == event.available:
                    return _active_ids(nodes)
                raise SourceLifecycleError("conflicting-sequence")

        updated = dict(nodes)
        updated[source.node_id] = _replace(source, available=event.available, sequence=event.sequence)
        _propagate(updated)

        changed_memory = [
            node for node in updated.values()
            if node.kind == _KIND_MEMORY and node.available != nodes[node.node_id].available
        ]
        for node in changed_memory:
            await self._set_memory_state(node, available=node.available)
        await self._upsert(tuple(updated.values()))
        return _active_ids(updated)

    # ------------------------------------------------------------------ lookups

    async def resolve(self, node_id: str, *, purpose: str) -> SourceRef | MemoryCitation | ArtifactRef:
        if purpose not in {_PURPOSE_GENERATION, _PURPOSE_AUDIT}:
            raise SourceLifecycleError("unknown-purpose")
        nodes = await self._load()
        node = nodes.get(node_id)
        if node is None:
            raise SourceLifecycleError("unknown-node")
        if purpose == _PURPOSE_GENERATION and not node.available:
            raise SourceLifecycleError("unavailable-evidence")
        return _reference(node)

    async def topic_available(self, artifact_id: str) -> bool:
        nodes = await self._load()
        for node in nodes.values():
            if node.kind == _KIND_TOPIC and node.body.get("artifact_id") == artifact_id:
                return node.available
        return True

    # ------------------------------------------------------------------ native state

    async def _set_memory_state(self, node: _Node, *, available: bool) -> None:
        citation = MemoryCitation.model_validate(node.body)
        desired = "active" if available else "inactive"
        async with self._runtime._context(self.scope_id) as context:
            async with self._runtime._locked(self.scope_id):
                service = context.artifacts.memory
                current = await service.head(context.artifacts.memory_artifact_id)
                versions = {version.entry_id: version for version in await service.entries(current)}
                target = versions.get(citation.entry_id)
                if target is None:
                    raise SourceLifecycleError("memory-entry-missing")
                states = {item.entry_id: item.state for item in current.content.manifest.entries}
                if states.get(citation.entry_id) == desired:
                    return
                if available:
                    await service.reactivate(current, entries=(target,), reason="source-restored")
                else:
                    await service.forget(current, entries=(target,), reason="source-withdrawn")

    # ------------------------------------------------------------------ persistence

    async def _load(self) -> dict[str, _Node]:
        statement = text(
            "SELECT node_id, kind, policy, parents, revision, sequence, available, body "
            f"FROM {_TABLE} WHERE scope_id = :scope_id"  # noqa: S608 - constant identifiers
        )
        async with self._database.transaction() as connection:
            result = await connection.execute(statement, {"scope_id": self.scope_id})
            rows = result.fetchall()
        nodes: dict[str, _Node] = {}
        for row in rows:
            node = _Node(
                node_id=row[0],
                kind=row[1],
                policy=row[2],
                parents=tuple(json.loads(row[3])),
                revision=row[4],
                sequence=row[5],
                available=bool(row[6]),
                body=json.loads(row[7]),
            )
            nodes[node.node_id] = node
        return nodes

    async def _upsert(self, nodes: tuple[_Node, ...]) -> None:
        update = text(
            f"UPDATE {_TABLE} SET kind = :kind, policy = :policy, parents = :parents, "  # noqa: S608
            "revision = :revision, sequence = :sequence, available = :available, body = :body "
            "WHERE scope_id = :scope_id AND node_id = :node_id"
        )
        insert = text(
            f"INSERT INTO {_TABLE} "  # noqa: S608
            "(scope_id, node_id, kind, policy, parents, revision, sequence, available, body) "
            "VALUES (:scope_id, :node_id, :kind, :policy, :parents, :revision, :sequence, :available, :body)"
        )
        async with self._database.transaction() as connection:
            for node in nodes:
                params = {
                    "scope_id": self.scope_id,
                    "node_id": node.node_id,
                    "kind": node.kind,
                    "policy": node.policy,
                    "parents": json.dumps(list(node.parents)),
                    "revision": node.revision,
                    "sequence": node.sequence,
                    "available": int(node.available),
                    "body": json.dumps(node.body, ensure_ascii=False),
                }
                result = await connection.execute(update, params)
                if result.rowcount:
                    continue
                await connection.execute(insert, params)

    async def _delete(self, node_id: str) -> None:
        statement = text(f"DELETE FROM {_TABLE} WHERE scope_id = :scope_id AND node_id = :node_id")  # noqa: S608
        async with self._database.transaction() as connection:
            await connection.execute(statement, {"scope_id": self.scope_id, "node_id": node_id})


class SourceLifecycleApplication:
    """Select the scoped Source lifecycle service."""

    def __init__(self, runtime: BuiltinRuntime, database: AsyncDatabase) -> None:
        self._runtime = runtime
        self._database = database

    def for_scope(self, scope_id: str) -> ScopedSourceLifecycle:
        return ScopedSourceLifecycle(self._runtime, self._database, scope_id)

    async def topic_available(self, scope_id: str, artifact_id: str) -> bool:
        return await self.for_scope(scope_id).topic_available(artifact_id)


def _policy(value: str) -> str:
    if value not in {_POLICY_ALL, _POLICY_ANY}:
        raise SourceLifecycleError("unknown-policy")
    return value


def _parents(values: tuple[str, ...]) -> tuple[str, ...]:
    if not values:
        raise SourceLifecycleError("missing-parents")
    return tuple(values)


def _same_binding(left: _Node, right: _Node) -> bool:
    return (
        left.kind == right.kind
        and left.policy == right.policy
        and left.parents == right.parents
        and left.revision == right.revision
        and left.body == right.body
    )


def _replace(node: _Node, *, available: bool, sequence: int | None = None) -> _Node:
    return _Node(node.node_id, node.kind, node.policy, node.parents, node.revision,
                 node.sequence if sequence is None else sequence, available, node.body)


def _evaluate(node: _Node, nodes: dict[str, _Node]) -> bool:
    if node.kind == _KIND_SOURCE:
        return node.available
    support = [nodes[parent].available for parent in node.parents]
    return all(support) if node.policy == _POLICY_ALL else any(support)


def _propagate(nodes: dict[str, _Node]) -> None:
    changed = True
    while changed:
        changed = False
        for node_id, node in list(nodes.items()):
            if node.kind == _KIND_SOURCE:
                continue
            availability = _evaluate(node, nodes)
            if availability != node.available:
                nodes[node_id] = _replace(node, available=availability)
                changed = True


def _active_ids(nodes: dict[str, _Node]) -> list[str]:
    return sorted(node_id for node_id, node in nodes.items() if node.available)


LifecyclePurpose = Literal["generation", "audit"]

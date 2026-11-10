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

"""Portable, self-validating context packs and prefix-reuse deltas."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass

_PACK_SCHEMA = "powercontext.context-pack.v1"
_DELTA_SCHEMA = "powercontext.context-pack-delta.v1"


class ContextPackError(ValueError):
    """Invalid pack, delta, UTF-8 chunk or receiver reconstruction."""


def _json(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _validate_items(items: tuple[bytes, ...]) -> None:
    if type(items) is not tuple or any(type(item) is not bytes for item in items):
        raise ContextPackError("chunks must be a tuple of bytes")  # noqa: TRY003
    try:
        for item in items:
            item.decode("utf-8")
    except UnicodeError as error:
        raise ContextPackError("each chunk must be valid UTF-8") from error  # noqa: TRY003


def _encoded(items: tuple[bytes, ...]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _digest(items: tuple[bytes, ...]) -> str:
    return "sha256:" + hashlib.sha256(_json({"schema": _PACK_SCHEMA, "items": _encoded(items)})).hexdigest()


def _validate_digest(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise ContextPackError("invalid digest")  # noqa: TRY003


def _pack_wire(items: tuple[bytes, ...], digest: str) -> bytes:
    payload = {"schema": _PACK_SCHEMA, "items": _encoded(items), "digest": digest, "byte_count": 0}
    while True:
        wire = _json(payload)
        if payload["byte_count"] == len(wire):
            return wire
        payload["byte_count"] = len(wire)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContextPackError("duplicate JSON field")  # noqa: TRY003
        result[key] = value
    return result


def _reject_constant(value):
    raise ContextPackError("non-finite JSON number")  # noqa: TRY003


def _wire_object(raw: bytes, fields: set[str], schema: str) -> dict:
    if type(raw) is not bytes:
        raise ContextPackError("wire data must be bytes")  # noqa: TRY003
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as error:
        raise ContextPackError("invalid wire JSON") from error  # noqa: TRY003
    if not isinstance(value, dict) or set(value) != fields or value["schema"] != schema:
        raise ContextPackError("invalid wire fields or schema")  # noqa: TRY003
    return value


def _decoded(value: object) -> tuple[bytes, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ContextPackError("chunks must be a Base64 string array")  # noqa: TRY003
    try:
        items = tuple(base64.b64decode(item.encode("ascii"), validate=True) for item in value)
    except (UnicodeError, binascii.Error, ValueError) as error:
        raise ContextPackError("invalid Base64 chunk") from error  # noqa: TRY003
    if _encoded(items) != value:
        raise ContextPackError("noncanonical Base64 chunk")  # noqa: TRY003
    return items


@dataclass(frozen=True, slots=True)
class ContextPack:
    """Immutable full pack, with digest and byte count independently checked."""

    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    def __post_init__(self) -> None:
        _validate_items(self.items)
        if self.digest != _digest(self.items):
            raise ContextPackError("pack digest mismatch")  # noqa: TRY003
        if type(self.byte_count) is not int or self.byte_count != len(self.to_bytes()):
            raise ContextPackError("pack byte_count mismatch")  # noqa: TRY003

    @classmethod
    def build(cls, items: tuple[bytes, ...]) -> ContextPack:
        _validate_items(items)
        digest = _digest(items)
        return cls(items, digest, len(_pack_wire(items, digest)))

    @classmethod
    def from_content(cls, content: str | None, *, chunk_bytes: int = 512) -> ContextPack:
        """Keep line boundaries, splitting oversized lines at UTF-8 boundaries."""
        if type(chunk_bytes) is not int or chunk_bytes <= 0:
            raise ContextPackError("chunk_bytes must be a positive integer")  # noqa: TRY003
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise ContextPackError("content must be text or None")  # noqa: TRY003
        items = []
        try:
            lines = [line.encode("utf-8") for line in content.splitlines(keepends=True)]
        except UnicodeError as error:
            raise ContextPackError("content must be valid UTF-8") from error  # noqa: TRY003
        for line in lines:
            start = 0
            while start < len(line):
                end = min(start + chunk_bytes, len(line))
                while end < len(line) and line[end] & 0xC0 == 0x80:
                    end -= 1
                if end == start:
                    raise ContextPackError("chunk_bytes cannot hold a UTF-8 code point")  # noqa: TRY003
                items.append(line[start:end])
                start = end
        return cls.build(tuple(items))

    def to_bytes(self) -> bytes:
        return _pack_wire(self.items, self.digest)

    @classmethod
    def from_bytes(cls, raw: bytes) -> ContextPack:
        value = _wire_object(raw, {"schema", "items", "digest", "byte_count"}, _PACK_SCHEMA)
        return cls(_decoded(value["items"]), value["digest"], value["byte_count"])


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    """The exact target suffix following a reusable base prefix."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    def __post_init__(self) -> None:
        _validate_digest(self.base_digest)
        _validate_digest(self.target_digest)
        if type(self.prefix_items) is not int or self.prefix_items < 0:
            raise ContextPackError("prefix_items must be a nonnegative integer")  # noqa: TRY003
        _validate_items(self.appended)

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        if not isinstance(base, ContextPack) or not isinstance(target, ContextPack):
            raise ContextPackError("base and target must be ContextPack values")  # noqa: TRY003
        prefix = 0
        for left, right in zip(base.items, target.items, strict=False):
            if left != right:
                break
            prefix += 1
        return cls(base.digest, target.digest, prefix, target.items[prefix:])

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        return _json({
            "schema": _DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": _encoded(self.appended),
        })

    @classmethod
    def from_bytes(cls, raw: bytes) -> ContextPackDelta:
        value = _wire_object(raw, {"schema", "base_digest", "target_digest", "prefix_items", "appended"}, _DELTA_SCHEMA)
        return cls(value["base_digest"], value["target_digest"], value["prefix_items"], _decoded(value["appended"]))


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Reconstruct using only the receiver's base and the decoded wire delta."""
    if not isinstance(base, ContextPack) or not isinstance(delta, ContextPackDelta):
        raise ContextPackError("expected a ContextPack and ContextPackDelta")  # noqa: TRY003
    if base.digest != delta.base_digest or delta.prefix_items > len(base.items):
        raise ContextPackError("delta does not match the supplied base")  # noqa: TRY003
    target = ContextPack.build(base.items[: delta.prefix_items] + delta.appended)
    if target.digest != delta.target_digest:
        raise ContextPackError("delta target digest mismatch")  # noqa: TRY003
    return target

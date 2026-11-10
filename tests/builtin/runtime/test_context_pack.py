"""Receiver-only replay, UTF-8 boundaries and strict wire validation."""

import json
import random

import pytest

from powercontext.builtin.runtime.context_pack import (
    ContextPack,
    ContextPackDelta,
    ContextPackError,
    apply_context_pack_delta,
)


@pytest.mark.parametrize("size", [1, 2, 3, 4, 7, 16, 512])
def test_utf8_chunk_boundaries_and_independent_replay(size):
    rng = random.Random(814)  # noqa: S311 - Reproducible test text, not cryptography.
    alphabet = "abc\r\n\t" + ("海洋" if size >= 3 else "") + ("🌊" if size >= 4 else "")
    text = "".join(rng.choice(alphabet) for _ in range(1500))
    target = ContextPack.from_content(text, chunk_bytes=size)
    assert b"".join(target.items) == text.encode("utf8")
    assert all(len(item) <= size and item.decode("utf8") for item in target.items)
    assert ContextPack.from_bytes(target.to_bytes()) == target
    assert ContextPack.from_content(text, chunk_bytes=size) == target
    base = ContextPack.from_content(text[:900], chunk_bytes=size)
    delta = ContextPackDelta.from_bytes(ContextPackDelta.between(base, target).to_bytes())
    receiver = ContextPack.from_bytes(base.to_bytes())
    assert apply_context_pack_delta(receiver, delta) == target


@pytest.mark.parametrize("size", [0, -1, True, 1.5, "512"])
def test_invalid_chunk_limits(size):
    with pytest.raises(ContextPackError):
        ContextPack.from_content("", chunk_bytes=size)


def test_invalid_utf8_and_surrogates_are_protocol_errors():
    with pytest.raises(ContextPackError):
        ContextPack.build((b"\xff",))
    with pytest.raises(ContextPackError):
        ContextPack.from_content("\ud800")
    with pytest.raises(ContextPackError):
        ContextPack.from_content("🌊", chunk_bytes=3)


def test_receiver_accepts_every_valid_common_prefix():
    base = ContextPack.build((b"one", b"two", b"old"))
    target = ContextPack.build((b"one", b"two", b"new"))
    for prefix in range(3):
        delta = ContextPackDelta(base.digest, target.digest, prefix, target.items[prefix:])
        assert (
            apply_context_pack_delta(
                ContextPack.from_bytes(base.to_bytes()), ContextPackDelta.from_bytes(delta.to_bytes())
            )
            == target
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", "wrong"),
        ("items", ["@@=="]),
        ("items", ["/w=="]),
        ("items", ["Zh=="]),
        ("items", "YQ=="),
        ("items", [True]),
        ("digest", "sha256:" + "0" * 64),
        ("byte_count", True),
        ("byte_count", 1.5),
        ("byte_count", -1),
        ("extra", 1),
    ],
)
def test_pack_rejects_tampering(field, value):
    payload = json.loads(ContextPack.build((b"a",)).to_bytes())
    payload[field] = value
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(json.dumps(payload).encode())


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", "wrong"),
        ("base_digest", "bad"),
        ("target_digest", None),
        ("prefix_items", True),
        ("prefix_items", 1.0),
        ("prefix_items", -1),
        ("appended", ["@@=="]),
        ("appended", ["/w=="]),
        ("extra", 1),
    ],
)
def test_delta_rejects_tampering(field, value):
    base = ContextPack.build((b"a",))
    target = ContextPack.build((b"a", b"b"))
    payload = json.loads(ContextPackDelta.between(base, target).to_bytes())
    payload[field] = value
    with pytest.raises(ContextPackError):
        ContextPackDelta.from_bytes(json.dumps(payload).encode())


def test_duplicate_fields_and_changed_content_are_rejected():
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(b'{"schema":"a","schema":"b"}')
    base = ContextPack.build((b"alpha",))
    target = ContextPack.build((b"alpha", b"beta"))
    delta = ContextPackDelta(base.digest, target.digest, 1, (b"different",))
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(base, delta)


def test_empty_target_then_repopulation():
    receiver = ContextPack.from_content("start")
    for text in ["", "new 海\nline", "", None]:
        target = ContextPack.from_content(text)
        wire = ContextPackDelta.between(receiver, target).to_bytes()
        receiver = apply_context_pack_delta(
            ContextPack.from_bytes(receiver.to_bytes()), ContextPackDelta.from_bytes(wire)
        )
        assert b"".join(receiver.items) == (text or "").encode()


def test_self_inclusive_byte_count_across_decimal_boundaries():
    for length in [0, 1, 9, 99, 999, 9999]:
        pack = ContextPack.build((b"a" * length,))
        payload = json.loads(pack.to_bytes())
        assert payload["byte_count"] == len(pack.to_bytes())
        assert ContextPack.from_bytes(pack.to_bytes()) == pack


async def test_runtime_empty_context_is_an_empty_wire_pack():
    from powercontext.builtin.runtime import BuiltinConfig, PrepareContextRequest, open_builtin_runtime
    from powercontext.builtin.scope import ScopeDraft

    async with open_builtin_runtime(BuiltinConfig()) as runtime:
        scope = await runtime.scopes.create(
            ScopeDraft(title="Empty", summary="Empty pack", idempotency_key="empty-pack")
        )
        context = runtime.context.for_scope(scope.scope_id)
        request = PrepareContextRequest(query="unknown")
        ordinary = await context.prepare(request)
        pack = ContextPack.from_bytes((await context.prepare_incremental(request)).to_bytes())
        assert ordinary.content is None and pack.items == ()
        old = ContextPack.from_content("old evidence")
        delta = await context.prepare_incremental(request, base_pack=old)
        restored = apply_context_pack_delta(
            ContextPack.from_bytes(old.to_bytes()), ContextPackDelta.from_bytes(delta.to_bytes())
        )
        assert restored == pack

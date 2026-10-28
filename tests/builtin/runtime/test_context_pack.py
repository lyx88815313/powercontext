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

from __future__ import annotations

import asyncio
import base64
import hashlib
import json

import pytest

from powercontext.builtin.artifacts.memory import MemoryCandidateRequest, MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import BuiltinConfig, CaptureSource, PrepareContextRequest, open_builtin_runtime
from powercontext.builtin.runtime.context_pack import (
    CONTEXT_PACK_DELTA_SCHEMA,
    CONTEXT_PACK_SCHEMA,
    ContextPack,
    ContextPackDelta,
    ContextPackError,
    apply_context_pack_delta,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.builtin.sources import ContentSource

_FIRST_TURN = "Project Manager: We compared three remote control prototypes in the second laboratory ."
_SECOND_TURN = "Engineer: The remote control signal latency stayed below two milliseconds in every sample ."


class _ContentCandidatePipeline:
    async def extract(self, request: MemoryCandidateRequest, /) -> tuple[MemoryEntryInput, ...]:
        return tuple(
            MemoryEntryInput(kind="conversation-turn", text=source.content, sources=(source,))
            for source in request.sources
            if isinstance(source, ContentSource)
        )


def _wire_payload(value: bytes) -> dict[str, object]:
    payload = json.loads(value)
    assert isinstance(payload, dict)
    return payload


def test_pack_payload_matches_an_independent_oracle() -> None:
    items = (b"alpha\n", "海洋".encode())
    unsigned = {
        "schema": CONTEXT_PACK_SCHEMA,
        "items": [base64.b64encode(item).decode("ascii") for item in items],
    }
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    pack = ContextPack.build(items)

    payload = _wire_payload(pack.to_bytes())
    assert payload == {
        **unsigned,
        "digest": "sha256:" + hashlib.sha256(canonical).hexdigest(),
        "byte_count": len(pack.to_bytes()),
    }
    assert pack.byte_count == len(pack.to_bytes())
    assert set(payload) == {"schema", "items", "digest", "byte_count"}


def test_pack_round_trip_preserves_items_and_digest() -> None:
    pack = ContextPack.build((b"alpha", b"beta"))

    restored = ContextPack.from_bytes(pack.to_bytes())

    assert restored == pack
    assert restored.items == (b"alpha", b"beta")
    assert restored.digest == pack.digest


@pytest.mark.parametrize("chunk_bytes", [7, 64, 512, 4096])
@pytest.mark.parametrize(
    "content",
    [
        "alpha 海洋 beta",
        "first line\nsecond line\n",
        "x" * 5000,
        "",
    ],
)
def test_from_content_chunks_stay_bounded_and_reconstruct_the_text(content: str, chunk_bytes: int) -> None:
    pack = ContextPack.from_content(content, chunk_bytes=chunk_bytes)

    assert b"".join(pack.items).decode("utf-8") == content
    assert all(0 < len(item) <= chunk_bytes for item in pack.items)
    assert all(item.decode("utf-8") for item in pack.items)
    assert pack == ContextPack.from_content(content, chunk_bytes=chunk_bytes)


def test_from_content_rejects_a_character_that_cannot_fit_the_limit() -> None:
    with pytest.raises(ContextPackError):
        ContextPack.from_content("海", chunk_bytes=2)

    with pytest.raises(ContextPackError):
        ContextPack.from_content("alpha", chunk_bytes=0)


def test_empty_context_is_an_empty_pack() -> None:
    empty = ContextPack.from_content(None)

    assert empty.items == ()
    assert empty.byte_count == len(empty.to_bytes())
    assert ContextPack.from_bytes(empty.to_bytes()) == empty


def test_delta_carries_the_suffix_after_the_common_prefix() -> None:
    base = ContextPack.build((b"alpha", b"beta"))
    target = ContextPack.build((b"alpha", b"beta", b"gamma"))

    delta = ContextPackDelta.between(base, target)

    payload = _wire_payload(delta.to_bytes())
    assert set(payload) == {"schema", "base_digest", "target_digest", "prefix_items", "appended"}
    assert payload["schema"] == CONTEXT_PACK_DELTA_SCHEMA
    assert payload["base_digest"] == base.digest
    assert payload["target_digest"] == target.digest
    assert payload["prefix_items"] == 2
    assert payload["appended"] == [base64.b64encode(b"gamma").decode("ascii")]
    assert delta.byte_count == len(delta.to_bytes())


@pytest.mark.parametrize(
    "target_items",
    [(b"a", b"b", b"new", b"tail"), (b"new", b"b", b"old"), (b"a", b"new", b"old"), ()],
)
def test_delta_rebuilds_a_change_at_any_position(target_items: tuple[bytes, ...]) -> None:
    base = ContextPack.build((b"a", b"b", b"old"))
    target = ContextPack.build(target_items)

    delta = ContextPackDelta.between(base, target)
    rebuilt = apply_context_pack_delta(
        ContextPack.from_bytes(base.to_bytes()), ContextPackDelta.from_bytes(delta.to_bytes())
    )

    assert rebuilt.to_bytes() == target.to_bytes()


def test_apply_rejects_a_base_that_does_not_match_the_delta() -> None:
    delta = ContextPackDelta.between(ContextPack.build((b"one",)), ContextPack.build((b"one", b"two")))

    with pytest.raises(ContextPackError):
        apply_context_pack_delta(ContextPack.build((b"other",)), delta)


def test_apply_rejects_a_prefix_beyond_the_base() -> None:
    base = ContextPack.build((b"one",))
    delta = ContextPackDelta(base.digest, base.digest, 2, (b"two",))

    with pytest.raises(ContextPackError):
        apply_context_pack_delta(base, delta)


def test_apply_rejects_a_target_digest_that_does_not_match() -> None:
    base = ContextPack.build((b"one",))
    delta = ContextPackDelta(base.digest, "sha256:" + "0" * 64, 0, (b"two",))

    with pytest.raises(ContextPackError):
        apply_context_pack_delta(base, delta)


@pytest.mark.parametrize("prefix", [True, 1.5, -1, "1"])
def test_delta_decoder_rejects_a_prefix_that_is_not_a_non_negative_integer(prefix: object) -> None:
    delta = ContextPackDelta.between(ContextPack.build((b"alpha",)), ContextPack.build((b"alpha", b"beta")))
    payload = _wire_payload(delta.to_bytes())
    payload["prefix_items"] = prefix

    with pytest.raises(ContextPackError):
        ContextPackDelta.from_bytes(json.dumps(payload).encode())


def test_pack_decoder_rejects_missing_extra_and_tampered_fields() -> None:
    pack = ContextPack.build((b"alpha",))
    payload = _wire_payload(pack.to_bytes())

    for field in payload:
        incomplete = {key: value for key, value in payload.items() if key != field}
        with pytest.raises(ContextPackError):
            ContextPack.from_bytes(json.dumps(incomplete).encode())

    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(json.dumps({**payload, "extra": 1}).encode())

    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(pack.to_bytes().replace(pack.digest.encode(), b"sha256:" + b"0" * 64))

    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(json.dumps({**payload, "byte_count": payload["byte_count"] + 1}).encode())


def test_delta_decoder_rejects_extra_fields_and_invalid_base64() -> None:
    delta = ContextPackDelta.between(ContextPack.build((b"alpha",)), ContextPack.build((b"alpha", b"beta")))
    payload = _wire_payload(delta.to_bytes())

    with pytest.raises(ContextPackError):
        ContextPackDelta.from_bytes(json.dumps({**payload, "extra": 1}).encode())

    with pytest.raises(ContextPackError):
        ContextPackDelta.from_bytes(json.dumps({**payload, "appended": ["not base64!"]}).encode())


def test_prepare_incremental_returns_a_pack_then_a_delta_that_rebuilds_the_context() -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(
            BuiltinConfig(database=SQLiteConfig()),
            candidate_pipeline=_ContentCandidatePipeline(),
        ) as runtime:
            scope = await runtime.scopes.create(
                ScopeDraft(title="Incremental", summary="Incremental pack", idempotency_key="context-pack-test")
            )
            sources = runtime.sources.for_scope(scope.scope_id)
            memory = runtime.memory.for_scope(scope.scope_id)
            context = runtime.context.for_scope(scope.scope_id)
            await sources.capture(CaptureSource(source_id="turn-1", content=_FIRST_TURN, metadata={}))
            while (await memory.flush()).processed:
                pass

            first = await context.prepare_incremental(PrepareContextRequest(query="remote control"))
            assert isinstance(first, ContextPack)

            await sources.capture(CaptureSource(source_id="turn-2", content=_SECOND_TURN, metadata={}))
            while (await memory.flush()).processed:
                pass
            second = await context.prepare_incremental(PrepareContextRequest(query="remote control"), base_pack=first)
            assert isinstance(second, ContextPackDelta)
            expected = await context.prepare(PrepareContextRequest(query="remote control"))

        restored_base = ContextPack.from_bytes(first.to_bytes())
        restored_delta = ContextPackDelta.from_bytes(second.to_bytes())
        rebuilt = apply_context_pack_delta(restored_base, restored_delta)

        assert b"".join(rebuilt.items).decode("utf-8") == expected.content
        assert expected.content
        assert expected.content != b"".join(first.items).decode("utf-8")

    asyncio.run(scenario())


def test_prepare_incremental_reports_an_empty_result_as_an_empty_pack() -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(
            BuiltinConfig(database=SQLiteConfig()),
            candidate_pipeline=_ContentCandidatePipeline(),
        ) as runtime:
            scope = await runtime.scopes.create(
                ScopeDraft(title="Empty", summary="Empty pack", idempotency_key="context-pack-empty")
            )
            context = runtime.context.for_scope(scope.scope_id)

            first = await context.prepare_incremental(PrepareContextRequest(query="remote control"))
            assert isinstance(first, ContextPack)
            assert first.items == ()

            second = await context.prepare_incremental(PrepareContextRequest(query="remote control"), base_pack=first)
            assert isinstance(second, ContextPackDelta)
            rebuilt = apply_context_pack_delta(
                ContextPack.from_bytes(first.to_bytes()), ContextPackDelta.from_bytes(second.to_bytes())
            )
            assert rebuilt.items == ()
            assert b"".join(rebuilt.items) == b""

    asyncio.run(scenario())

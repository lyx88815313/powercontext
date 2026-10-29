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

"""Evidence selection: literal spans, exact citations, and a budget charged on the output."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from powercontext.artifacts import ArtifactAddress, ArtifactRef
from powercontext.builtin.artifacts.memory.models import MemoryCitation, MemoryHit
from powercontext.builtin.runtime import PrepareContextRequest
from powercontext.builtin.runtime.prepared_context import (
    MemoryEntryAddress,
    PreparedContextBuilder,
    PreparedMemoryCandidates,
)

SCOPE_A = "0190f0e1-0000-7000-8000-00000000000a"
SCOPE_B = "0190f0e1-0000-7000-8000-00000000000b"
MEMORY = ArtifactRef(family="memory", artifact_id="0190f0e1-0000-7000-8000-0000000000fe", revision=3)
PADDING = "General administrative notes about the archive. " * 40


def hit(entry_id: str, text: str, *, score: float = 1.0) -> MemoryHit:
    return MemoryHit(
        memory_ref=MEMORY,
        entry_id=entry_id,
        entry_version_id=f"version-{entry_id}",
        text=text,
        score=score,
        matched_by=(),
    )


def build(
    *,
    query: str,
    hits: tuple[MemoryHit, ...],
    max_bytes: int = 1400,
    selection: bool = True,
    scope_id: str = SCOPE_A,
    current_scope_id: str | None = SCOPE_A,
):
    request = PrepareContextRequest(
        query=query, max_bytes=max_bytes, **({"evidence_selection": True} if selection else {})
    )
    return PreparedContextBuilder().build_scopes_result(
        request=request,
        current_scope_id=current_scope_id,
        memory_candidates=(PreparedMemoryCandidates(scope_id=scope_id, memory_ref=MEMORY, hits=hits),),
    )


def test_query_selects_late_literal_evidence() -> None:
    cobalt = hit("cobalt", PADDING + "The cobalt sensor is calibrated at 37 degrees.")
    amber = hit("amber", PADDING + "The amber valve opens at 19 degrees.")
    result = build(query="cobalt sensor calibrated", hits=(cobalt, amber))

    assert result.fragments, "selection must deliver evidence"
    text = " ".join(fragment.text for fragment in result.fragments)
    assert "37" in text
    assert "19" not in text
    for fragment in result.fragments:
        source = cobalt.text if fragment.address.entry_id == "cobalt" else amber.text
        assert fragment.text == source[fragment.start : fragment.end]
        assert 0 <= fragment.start < fragment.end <= len(source)


def test_fragments_origins_and_items_agree() -> None:
    hits = (hit("alpha", PADDING + "The cobalt sensor is calibrated at 37 degrees."),)
    result = build(query="cobalt sensor calibrated", hits=hits)

    items = json.loads(result.context.content.splitlines()[-2])["items"]
    assert len(items) == len(result.fragments) == len(result.origins)
    for item, fragment, origin in zip(items, result.fragments, result.origins, strict=True):
        assert origin == fragment.address
        assert isinstance(origin, MemoryEntryAddress)
        assert item["content"] == fragment.text
        assert (item["start"], item["end"]) == (fragment.start, fragment.end)
        assert item["citation"] == {
            "scope_id": SCOPE_A,
            "memory_ref": MEMORY.model_dump(mode="json"),
            "entry_id": fragment.address.entry_id,
            "entry_version_id": fragment.address.entry_version_id,
        }


def test_budget_counts_the_whole_rendered_context() -> None:
    hits = tuple(
        hit(f"entry-{index}", PADDING + f"Observation {index} mentions the cobalt sensor at {index} degrees.")
        for index in range(3)
    )
    for max_bytes in (512, 700, 1200, 1400, 1800):
        result = build(query="cobalt sensor degrees", hits=hits, max_bytes=max_bytes)
        content = result.context.content or ""
        assert result.context.content_bytes == len(content.encode("utf-8"))
        assert result.context.content_bytes <= max_bytes
        # The envelope itself is charged: an item's text is not budgeted on its own.
        assert "BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1" in content or not content


def test_unicode_is_charged_in_utf8_bytes() -> None:
    hits = (hit("unicode", "\u6d77\u6d0b\u89c2\u6d4b station records temperatures. " * 40),)
    result = build(query="\u6d77\u6d0b\u89c2\u6d4b station", hits=hits, max_bytes=1200)
    assert result.fragments
    assert result.context.content_bytes <= 1200


def test_selection_off_keeps_ordinary_prepare() -> None:
    hits = (hit("alpha", "A station records temperature."),)
    result = build(query="temperature", hits=hits, selection=False)
    assert result.fragments == ()
    items = json.loads(result.context.content.splitlines()[-2])["items"]
    assert items[0]["content"] == "A station records temperature."


def test_scope_identity_is_not_mixed_for_identical_text() -> None:
    text = PADDING + "The cobalt sensor is calibrated at 37 degrees."
    same = MemoryHit(
        memory_ref=MEMORY,
        entry_id="alpha",
        entry_version_id="version-alpha",
        text=text,
        score=1.0,
        matched_by=(),
    )
    request = PrepareContextRequest(query="cobalt sensor calibrated", max_bytes=1400, evidence_selection=True)
    result = PreparedContextBuilder().build_scopes_result(
        request=request,
        current_scope_id=SCOPE_A,
        memory_candidates=(
            PreparedMemoryCandidates(scope_id=SCOPE_A, memory_ref=MEMORY, hits=(same,)),
            PreparedMemoryCandidates(scope_id=SCOPE_B, memory_ref=MEMORY, hits=(same,)),
        ),
    )

    assert {fragment.address.memory.scope_id for fragment in result.fragments} == {SCOPE_A, SCOPE_B}
    for fragment in result.fragments:
        # Identical prose in another Scope keeps its own identity rather than borrowing one.
        assert fragment.address == MemoryEntryAddress(
            memory=ArtifactAddress(scope_id=fragment.address.memory.scope_id, artifact=MEMORY),
            entry_id="alpha",
            entry_version_id="version-alpha",
        )
        items = json.loads(result.context.content.splitlines()[-2])["items"]
        assert {item["citation"]["scope_id"] for item in items} == {SCOPE_A, SCOPE_B}


def test_no_relevant_text_yields_empty_context() -> None:
    result = build(query="cobalt sensor calibrated", hits=(hit("alpha", "Unrelated prose about weather."),))
    assert result.context.status == "empty"
    assert result.context.content is None
    assert result.fragments == ()
    assert result.origins == ()


def test_no_candidates_yields_empty_context() -> None:
    result = build(query="cobalt", hits=())
    assert result.context.status == "empty"
    assert result.fragments == ()


def test_evidence_selection_must_be_explicit_bool() -> None:
    with pytest.raises(ValidationError):
        PrepareContextRequest(query="cobalt", evidence_selection="yes")  # type: ignore[arg-type]


def test_citation_can_be_replayed_against_the_address() -> None:
    hits = (hit("alpha", PADDING + "The cobalt sensor is calibrated at 37 degrees."),)
    result = build(query="cobalt sensor calibrated", hits=hits)
    fragment = result.fragments[0]
    citation = MemoryCitation(
        memory_ref=fragment.address.memory.artifact,
        entry_id=fragment.address.entry_id,
        entry_version_id=fragment.address.entry_version_id,
    )
    assert citation.memory_ref == MEMORY
    assert ArtifactAddress(scope_id=SCOPE_A, artifact=MEMORY) == fragment.address.memory

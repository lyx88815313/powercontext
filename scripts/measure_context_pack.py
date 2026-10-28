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

"""Measure the Context Pack protocol over a repeated prepare sequence.

The tool drives the real Runtime, so the numbers are the ones a peer would
observe: every round prepares the context once, sends the first full pack and
then one delta per round, and the receiver rebuilds each round from the
serialized bytes alone.

Example::

    python scripts/measure_context_pack.py --meeting data/ALL/test/ES2004a.json
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
from collections.abc import Sequence
from pathlib import Path

from powercontext.builtin.artifacts.memory import MemoryCandidateRequest, MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import BuiltinConfig, CaptureSource, PrepareContextRequest, open_builtin_runtime
from powercontext.builtin.runtime.context_pack import (
    ContextPack,
    ContextPackDelta,
    apply_context_pack_delta,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.builtin.sources import ContentSource

_MIN_TURN_BYTES = 120
_MAX_TURN_BYTES = 900


class _ContentCandidatePipeline:
    async def extract(self, request: MemoryCandidateRequest, /) -> tuple[MemoryEntryInput, ...]:
        return tuple(
            MemoryEntryInput(kind="conversation-turn", text=source.content, sources=(source,))
            for source in request.sources
            if isinstance(source, ContentSource)
        )


def load_turns(path: Path, term: str | None) -> tuple[list[str], str]:
    """Return the meeting turns and the retrieval term used for the measurement."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "meeting_transcripts" in payload:
        transcript = payload["meeting_transcripts"]
        turns = [f"{turn['speaker']}: {turn['content']}" for turn in transcript]
        bodies = [turn["content"] for turn in transcript]
        query = term or "remote"
    else:
        turns = [turn["text"] for turn in payload["turns"]]
        bodies = [turn["text"].split(": ", 1)[-1] for turn in payload["turns"]]
        query = term or payload.get("query", "remote")
    selected = [
        turn
        for turn, body in zip(turns, bodies, strict=True)
        if _MIN_TURN_BYTES <= len(body.encode("utf-8")) <= _MAX_TURN_BYTES and query.lower() in body.lower()
    ]
    return selected or turns, query


class _Measurement:
    """Accumulate the wire cost of one measurement sequence."""

    def __init__(self) -> None:
        self.raw_bytes = 0
        self.pack_bytes = 0
        self.gzip_bytes = 0
        self.incremental_bytes = 0
        self.rounds = 0
        self.distinct: set[bytes] = set()
        self._base: ContextPack | None = None

    def record(self, content: str, result: ContextPack | ContextPackDelta) -> None:
        encoded = content.encode("utf-8")
        self.raw_bytes += len(encoded)
        self.gzip_bytes += len(gzip.compress(encoded, compresslevel=9, mtime=0))
        self.distinct.add(encoded)
        self.rounds += 1
        if isinstance(result, ContextPack):
            payload = result.to_bytes()
            self.pack_bytes += len(payload)
            self.incremental_bytes += len(payload)
            self._base = result
            return
        self.pack_bytes += len(ContextPack.from_content(content).to_bytes())
        self.incremental_bytes += len(result.to_bytes())
        rebuilt = apply_context_pack_delta(
            ContextPack.from_bytes(self._base.to_bytes()) if self._base else ContextPack.build(()),
            ContextPackDelta.from_bytes(result.to_bytes()),
        )
        if b"".join(rebuilt.items) != encoded:
            raise RuntimeError("context-pack-rebuild-mismatch")
        self._base = rebuilt

    def report(self) -> str:
        lines = [
            f"rounds                         {self.rounds}",
            f"distinct contexts              {len(self.distinct)}",
            f"raw_full_bytes                 {self.raw_bytes}",
            f"incremental_bytes              {self.incremental_bytes}",
            f"full v1 pack bytes             {self.pack_bytes}",
            f"gzip bytes (per round)         {self.gzip_bytes}",
            f"1 - incremental / raw          {self._saving(self.raw_bytes):.2%}",
            f"1 - incremental / full pack    {self._saving(self.pack_bytes):.2%}",
            f"1 - incremental / gzip         {self._saving(self.gzip_bytes):.2%}",
        ]
        return "\n".join(lines)

    def _saving(self, baseline: int) -> float:
        if baseline <= 0:
            return 0.0
        return 1 - self.incremental_bytes / baseline


async def run_measurement(
    turns: Sequence[str],
    queries: Sequence[str],
    *,
    rounds: int,
    starting_turns: int,
    append_every: int,
    max_bytes: int,
) -> _Measurement:
    measurement = _Measurement()
    async with open_builtin_runtime(
        BuiltinConfig(database=SQLiteConfig()),
        candidate_pipeline=_ContentCandidatePipeline(),
    ) as runtime:
        scope = await runtime.scopes.create(
            ScopeDraft(
                title="Context pack measurement",
                summary="Context pack measurement",
                idempotency_key="context-pack-measurement",
            )
        )
        sources = runtime.sources.for_scope(scope.scope_id)
        memory = runtime.memory.for_scope(scope.scope_id)
        context = runtime.context.for_scope(scope.scope_id)
        captured = 0

        async def capture(count: int) -> None:
            nonlocal captured
            for _ in range(count):
                await sources.capture(
                    CaptureSource(
                        source_id=f"turn-{captured}",
                        content=turns[captured % len(turns)],
                        metadata={},
                    )
                )
                captured += 1
            while (await memory.flush()).processed:
                pass

        await capture(min(starting_turns, len(turns)))
        base: ContextPack | None = None
        for index in range(rounds):
            if append_every and index and index % append_every == 0:
                await capture(1)
            request = PrepareContextRequest(query=queries[index % len(queries)], max_bytes=max_bytes)
            prepared = await context.prepare(request)
            result = await context.prepare_incremental(request, base_pack=base)
            measurement.record(prepared.content or "", result)
            if isinstance(result, ContextPack):
                base = result
            else:
                base = apply_context_pack_delta(
                    ContextPack.from_bytes(base.to_bytes()) if base else ContextPack.build(()),
                    ContextPackDelta.from_bytes(result.to_bytes()),
                )
    return measurement


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--meeting", type=Path, required=True, help="QMSum meeting JSON or the public sample")
    parser.add_argument("--term", default=None, help="retrieval term; defaults to the sample query")
    parser.add_argument(
        "--query",
        action="append",
        default=None,
        help="retrieval query; repeat to rotate the query across rounds",
    )
    parser.add_argument("--rounds", type=int, default=40)
    parser.add_argument("--starting-turns", type=int, default=8)
    parser.add_argument("--append-every", type=int, default=0, help="capture one more turn every N rounds")
    parser.add_argument("--max-bytes", type=int, default=8000)
    arguments = parser.parse_args()

    turns, query = load_turns(arguments.meeting, arguments.term)
    queries = arguments.query or [query]
    measurement = asyncio.run(
        run_measurement(
            turns,
            queries,
            rounds=arguments.rounds,
            starting_turns=arguments.starting_turns,
            append_every=arguments.append_every,
            max_bytes=arguments.max_bytes,
        )
    )
    print(f"meeting {arguments.meeting} term {query!r} turns {len(turns)} queries {len(queries)}")
    print(measurement.report())


if __name__ == "__main__":
    main()

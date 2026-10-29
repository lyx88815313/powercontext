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

"""Cross-call embedding request coalescing with success-only vector reuse.

Callers share one wrapper per event loop. Concurrent calls deduplicate their
texts into bounded provider batches, callers arriving while a batch is still
collecting or executing join it, and successful vectors feed a bounded LRU so
later calls (including queries on symmetric models) avoid a second request.
Cancellation is scoped to the caller: batch work runs in its own task, so a
cancelled waiter never cancels or loses the vectors other waiters need.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel, QueryEmbeddingModel

BatchKey = tuple[bool, str]


@dataclass(slots=True)
class _Batch:
    """One bounded provider request shared by every caller that needs its texts."""

    query: bool
    done: asyncio.Future[None]
    texts: list[str] = field(default_factory=list)
    frozen: tuple[str, ...] = ()
    vectors: dict[str, EmbeddingVector] = field(default_factory=dict)
    usage: InferenceUsage = field(default_factory=lambda: InferenceUsage(requests=0))


class CoalescingEmbeddingModel:
    """Merge concurrent ``embed`` calls into bounded, shared provider requests.

    ``max_batch_size`` caps the distinct texts per provider call, ``coalesce_ms``
    is the collection window (zero still yields once so same-turn callers merge),
    and ``cache_size`` bounds the success-only LRU (zero disables reuse across
    calls while still sharing in-flight work).
    """

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if type(max_batch_size) is not int or max_batch_size < 1:
            raise ValueError("max_batch_size must be a positive integer")  # noqa: TRY003
        if isinstance(coalesce_ms, bool) or not isinstance(coalesce_ms, (int, float)) or coalesce_ms < 0:
            raise ValueError("coalesce_ms must be a non-negative number")  # noqa: TRY003
        if type(cache_size) is not int or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer")  # noqa: TRY003
        self._model = model
        self.profile = model.profile
        self._max_batch_size = max_batch_size
        self._coalesce_seconds = float(coalesce_ms) / 1000.0
        self._cache_size = cache_size
        # Text keys waiting for, or executing in, a provider batch.
        self._pending: dict[BatchKey, _Batch] = {}
        self._inflight: dict[BatchKey, _Batch] = {}
        # Collecting batches per namespace; each namespace flushes independently.
        self._ready: dict[bool, list[_Batch]] = {}
        self._flush_tasks: dict[bool, asyncio.Task[None]] = {}
        self._batch_tasks: set[asyncio.Task[None]] = set()
        # A success-only LRU keyed by (query space, text). Query and document
        # spaces share a key only for symmetric delegates, where reusing the
        # document vector for the same text is exactly the intended behavior.
        self._cache: OrderedDict[BatchKey, EmbeddingVector] = OrderedDict()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        return await self._embed(texts, query=False)

    async def embed_query(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        if isinstance(self._model, QueryEmbeddingModel):
            return await self._embed(texts, query=True)
        # A symmetric model has one vector space, so queries reuse the same
        # cache entries and in-flight work as documents.
        return await self._embed(texts, query=False)

    async def _embed(self, texts: Iterable[str], *, query: bool) -> EmbeddingResult:
        values = tuple(texts)
        if not values:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))
        self._bind_loop()
        cached: dict[str, EmbeddingVector] = {}
        waits: dict[str, _Batch] = {}
        for text in dict.fromkeys(values):
            key: BatchKey = (query, text)
            vector = self._cached(key)
            if vector is not None:
                cached[text] = vector
                continue
            batch = self._pending.get(key) or self._inflight.get(key)
            if batch is None:
                batch = self._enqueue(query, text)
            waits[text] = batch
        if self._ready.get(query):
            self._schedule_flush(query)
        batches = _unique(waits.values())
        for batch in batches:
            # Shield the shared batch: cancelling this waiter would otherwise
            # cancel the Future that every other waiter is blocked on.
            await asyncio.shield(batch.done)
        return EmbeddingResult(
            vectors=tuple(cached[text] if text in cached else waits[text].vectors[text] for text in values),
            usage=_merge_usage(batch.usage for batch in batches),
        )

    def _bind_loop(self) -> None:
        """Abandon state owned by a closed loop so one model can be reused."""

        loop = asyncio.get_running_loop()
        if self._loop is loop:
            return
        self._loop = loop
        self._pending.clear()
        self._inflight.clear()
        self._ready.clear()
        self._flush_tasks.clear()
        self._batch_tasks.clear()

    def _cached(self, key: BatchKey) -> EmbeddingVector | None:
        if self._cache_size <= 0:
            return None
        vector = self._cache.get(key)
        if vector is None:
            return None
        self._cache.move_to_end(key)
        return vector

    def _enqueue(self, query: bool, text: str) -> _Batch:
        batches = self._ready.setdefault(query, [])
        if not batches or len(batches[-1].texts) >= self._max_batch_size:
            batches.append(_Batch(query=query, done=asyncio.get_running_loop().create_future()))
            batches[-1].done.add_done_callback(_retrieve_exception)
        batch = batches[-1]
        batch.texts.append(text)
        self._pending[(query, text)] = batch
        return batch

    def _schedule_flush(self, query: bool) -> None:
        task = self._flush_tasks.get(query)
        if task is not None and not task.done():
            return
        self._flush_tasks[query] = asyncio.create_task(self._flush(query))

    async def _flush(self, query: bool) -> None:
        # A zero window still yields once so callers already runnable in this
        # turn register their texts before the batches freeze.
        await asyncio.sleep(self._coalesce_seconds)
        batches = self._ready.pop(query, None)
        if not batches:
            return
        for batch in batches:
            batch.frozen = tuple(batch.texts)
            for text in batch.frozen:
                key: BatchKey = (query, text)
                if self._pending.get(key) is batch:
                    del self._pending[key]
                self._inflight[key] = batch
            task = asyncio.create_task(self._run_batch(batch))
            self._batch_tasks.add(task)
            task.add_done_callback(self._batch_tasks.discard)

    async def _run_batch(self, batch: _Batch) -> None:
        try:
            delegate = self._model.embed_query if batch.query else self._model.embed
            result = await delegate(batch.frozen)
            if len(result.vectors) != len(batch.frozen):
                raise ValueError(  # noqa: TRY003, TRY301 - provider contract violation
                    "embedding vector count does not match the batch size"
                )
            for text, vector in zip(batch.frozen, result.vectors, strict=True):
                batch.vectors[text] = vector
                self._remember((batch.query, text), vector)
            batch.usage = result.usage
        except BaseException as error:
            if not batch.done.done():
                batch.done.set_exception(error)
        else:
            if not batch.done.done():
                batch.done.set_result(None)
        finally:
            for text in batch.frozen:
                key: BatchKey = (batch.query, text)
                if self._inflight.get(key) is batch:
                    del self._inflight[key]

    def _remember(self, key: BatchKey, vector: EmbeddingVector) -> None:
        if self._cache_size <= 0:
            return
        self._cache[key] = vector
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)


def _unique(batches: Iterable[_Batch]) -> tuple[_Batch, ...]:
    seen: set[int] = set()
    ordered: list[_Batch] = []
    for batch in batches:
        identity = id(batch)
        if identity not in seen:
            seen.add(identity)
            ordered.append(batch)
    return tuple(ordered)


def _merge_usage(usages: Iterable[InferenceUsage]) -> InferenceUsage:
    """Sum distinct provider batches once, keeping unknown token counts unknown."""

    collected = tuple(usages)
    if not collected:
        return InferenceUsage(requests=0)
    return InferenceUsage(
        requests=sum(usage.requests for usage in collected),
        input_tokens=_sum_optional(usage.input_tokens for usage in collected),
        output_tokens=_sum_optional(usage.output_tokens for usage in collected),
    )


def _sum_optional(values: Iterable[int | None]) -> int | None:
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _retrieve_exception(future: asyncio.Future[None]) -> None:
    """Mark a shared failure as observed so an abandoned batch does not warn."""

    if not future.cancelled():
        future.exception()


__all__ = ["CoalescingEmbeddingModel"]

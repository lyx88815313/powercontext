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

"""HTTP cross-encoder reranking for coarse Memory search candidates."""

from __future__ import annotations

import math
from typing import Any

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import (
    MemoryRerankDecision,
    _validate_bounds,
)
from powercontext.builtin.inference import InferenceUsage

HTTP_RERANK_POLICY_ID = "http-cross-encoder-v1"


class HTTPMemoryReranker:
    """Score one bounded candidate pool through an HTTP reranking service.

    The service accepts at most ``batch_size`` candidates per request, so one coarse
    search pool is split into contiguous batches. Every request carries each
    candidate's one-based rank in the *complete* pool rather than a batch-local
    position, and the parsed scores are joined back onto those global ranks before a
    single global selection. A strong candidate from any batch can therefore enter
    the final decision, while the returned ranks keep pointing at the original
    Memory entries and versions.

    The supplied ``httpx.AsyncClient`` owns transport, timeouts, and connection
    pooling; this adapter issues requests and validates their payloads. Transport
    and status failures propagate unchanged, because the caller configured the
    client and needs to see its own error types. Malformed JSON or a response that
    does not exactly cover the requested ranks with finite scores raises
    :class:`ValueError`.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = HTTP_RERANK_POLICY_ID,
    ) -> None:
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError("rerank batch_size must be a positive integer")  # noqa: TRY003
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Score every candidate exactly once and select the globally best ranks."""

        _validate_bounds(len(candidates), limit)
        scores: dict[int, float] = {}
        requests = 0
        for offset in range(0, len(candidates), self._batch_size):
            batch = candidates[offset : offset + self._batch_size]
            ranks = tuple(range(offset + 1, offset + len(batch) + 1))
            response = await self._client.post(
                self._endpoint,
                json={
                    "query": query,
                    "candidates": [
                        {"rank": rank, "text": candidate.text} for rank, candidate in zip(ranks, batch, strict=True)
                    ],
                },
            )
            response.raise_for_status()
            requests += 1
            scores.update(_parse_scores(response, expected_ranks=ranks))
        # Selection is global: descending score, then ascending original rank so an
        # exact tie keeps the coarse pool's order instead of the batch's order.
        selected = sorted(scores, key=lambda rank: (-scores[rank], rank))[:limit]
        return MemoryRerankDecision(
            selected_ranks=tuple(selected),
            usage=InferenceUsage(requests=requests),
        )


def _parse_scores(response: httpx.Response, *, expected_ranks: tuple[int, ...]) -> dict[int, float]:
    """Validate one batch response and return its rank-to-score mapping."""

    payload: Any = _decode_json(response)
    if not isinstance(payload, dict) or set(payload) != {"scores"}:
        raise ValueError("rerank response must contain exactly the 'scores' field")  # noqa: TRY003
    entries = payload["scores"]
    if not isinstance(entries, list):
        raise ValueError("rerank response 'scores' must be a list")  # noqa: TRY003, TRY004
    expected = set(expected_ranks)
    scores: dict[int, float] = {}
    for entry in entries:
        rank, score = _parse_score_entry(entry)
        if rank not in expected:
            raise ValueError("rerank score rank does not belong to this batch")  # noqa: TRY003
        if rank in scores:
            raise ValueError("rerank response repeats a rank")  # noqa: TRY003
        scores[rank] = score
    if scores.keys() != expected:
        raise ValueError("rerank response must cover every requested rank exactly once")  # noqa: TRY003
    return scores


def _decode_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as error:
        raise ValueError("rerank service returned invalid JSON") from error  # noqa: TRY003


def _parse_score_entry(entry: Any) -> tuple[int, float]:
    if not isinstance(entry, dict) or set(entry) != {"rank", "score"}:
        raise ValueError("rerank score entries must contain exactly 'rank' and 'score'")  # noqa: TRY003
    rank = entry["rank"]
    if type(rank) is not int:
        raise ValueError("rerank score rank must be a one-based integer")  # noqa: TRY003
    value = entry["score"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("rerank score must be a number")  # noqa: TRY003, TRY004
    score = float(value)
    if not math.isfinite(score):
        raise ValueError("rerank score must be finite")  # noqa: TRY003
    return rank, score


__all__ = ["HTTP_RERANK_POLICY_ID", "HTTPMemoryReranker"]

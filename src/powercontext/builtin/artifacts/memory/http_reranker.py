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

"""Identity-preserving Memory reranking through a caller-owned HTTP client."""

from __future__ import annotations

import json
import math

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage


class HTTPMemoryReranker:
    """Score every original candidate once, then select a global stable top-k.

    The caller controls client lifetime, authentication and timeouts. Requests
    are sequential, bounded by ``batch_size`` and never retried implicitly.
    HTTP and transport failures retain their httpx exception types.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = "http-cross-encoder-v1",
    ) -> None:
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")  # noqa: TRY003
        self.policy_id = policy_id
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size

    async def rerank(self, query: str, candidates: tuple[MemoryHit, ...], limit: int, /) -> MemoryRerankDecision:
        """Return original global ranks without mutating candidates or entries."""
        if not candidates or type(limit) is not int or not 1 <= limit <= len(candidates):
            raise ValueError("rerank requires candidates and a limit within their count")  # noqa: TRY003
        scores: dict[int, int | float] = {}
        requests = 0
        for start in range(0, len(candidates), self._batch_size):
            batch = candidates[start : start + self._batch_size]
            body = {
                "query": query,
                "candidates": [
                    {"rank": start + offset, "text": candidate.text} for offset, candidate in enumerate(batch, 1)
                ],
            }
            requests += 1
            response = await self._client.post(self._endpoint, json=body, follow_redirects=False)
            response.raise_for_status()
            payload = json.loads(response.content, object_pairs_hook=_unique_object)
            scores.update(_validated_scores(payload, set(range(start + 1, start + len(batch) + 1))))
        selected = sorted(scores, key=lambda rank: (-scores[rank], rank))[:limit]
        return MemoryRerankDecision(selected_ranks=tuple(selected), usage=InferenceUsage(requests=requests))


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate JSON members instead of silently overwriting them."""
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate field in reranker response")  # noqa: TRY003
        value[key] = item
    return value


def _validated_scores(payload: object, expected: set[int]) -> dict[int, int | float]:
    if not isinstance(payload, dict) or set(payload) != {"scores"} or not isinstance(payload["scores"], list):
        raise ValueError("reranker response must contain exactly a scores array")  # noqa: TRY003
    scores: dict[int, int | float] = {}
    for item in payload["scores"]:
        if not isinstance(item, dict) or set(item) != {"rank", "score"}:
            raise ValueError("score records must contain exactly rank and score")  # noqa: TRY003
        rank, score = item["rank"], item["score"]
        if type(rank) is not int or rank not in expected or rank in scores:
            raise ValueError("response ranks must cover this batch exactly once")  # noqa: TRY003
        if type(score) not in (int, float) or (isinstance(score, float) and not math.isfinite(score)):
            raise ValueError("score must be a finite JSON number")  # noqa: TRY003
        scores[rank] = score
    if set(scores) != expected:
        raise ValueError("response is missing a batch rank")  # noqa: TRY003
    return scores

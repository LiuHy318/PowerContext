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

"""Cross-call request coalescing and bounded vector caching for embeddings.

A single wrapper coordinates every call made in one event loop: identical texts
in the same call, identical texts in concurrent calls, and texts already in
flight all share one underlying request. Successful vectors are kept in a bounded
LRU cache so later calls can answer without touching the provider.

The wrapper keeps three invariants that callers depend on:

* every call receives vectors in its own input order, duplicate positions
  included;
* a call only reports the usage of the distinct underlying batches it waited
  for, so a shared batch is not double counted inside one call;
* cancelling one call never cancels work that other calls are waiting on.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel

__all__ = ["CoalescingEmbeddingModel"]


@dataclass(frozen=True, slots=True)
class _Settled:
    """One settled vector together with the batch that produced it."""

    vector: EmbeddingVector
    batch: int
    usage: InferenceUsage


def _sum_tokens(values: Iterable[int | None]) -> int | None:
    """Sum optional token counts, keeping ``None`` when nothing was reported."""

    reported = [value for value in values if value is not None]
    if not reported:
        return None
    return sum(reported)


class CoalescingEmbeddingModel:
    """Embedding model wrapper that coalesces requests and caches vectors."""

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        self._model = model
        self.profile = model.profile
        self._max_batch_size = max(1, int(max_batch_size))
        self._coalesce_seconds = max(0.0, float(coalesce_ms) / 1000.0)
        self._cache_size = max(0, int(cache_size))

        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._pending: dict[str, asyncio.Future[_Settled]] = {}
        self._queue: list[str] = []
        self._flusher: asyncio.Task[None] | None = None
        self._batches = 0

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Return one ordered vector per input text, sharing provider work."""

        requested = tuple(texts)
        if not requested:
            return EmbeddingResult(vectors=())

        settled: dict[str, _Settled] = {}
        waiting: dict[str, asyncio.Future[_Settled]] = {}
        missing: list[str] = []

        for text in dict.fromkeys(requested):
            cached = self._cache_lookup(text)
            if cached is not None:
                # Cached vectors belong to no batch of this call, so they add no
                # usage and no provider request.
                settled[text] = _Settled(vector=cached, batch=-1, usage=InferenceUsage(requests=0))
                continue
            future = self._pending.get(text)
            if future is None:
                future = asyncio.get_running_loop().create_future()
                self._pending[text] = future
                missing.append(text)
            waiting[text] = future

        if missing:
            self._queue.extend(missing)
            self._arm_flusher()

        if waiting:
            results = await asyncio.gather(*(asyncio.shield(future) for future in waiting.values()))
            for text, value in zip(waiting, results, strict=True):
                settled[text] = value

        vectors = tuple(settled[text].vector for text in requested)
        return EmbeddingResult(vectors=vectors, usage=self._collect_usage(settled.values()))

    def _collect_usage(self, values: Iterable[_Settled]) -> InferenceUsage:
        batches: dict[int, InferenceUsage] = {}
        for value in values:
            if value.batch >= 0:
                batches[value.batch] = value.usage
        if not batches:
            return InferenceUsage(requests=0)
        return InferenceUsage(
            requests=sum(usage.requests for usage in batches.values()),
            input_tokens=_sum_tokens(usage.input_tokens for usage in batches.values()),
            output_tokens=_sum_tokens(usage.output_tokens for usage in batches.values()),
        )

    def _cache_lookup(self, text: str) -> EmbeddingVector | None:
        if self._cache_size == 0:
            return None
        vector = self._cache.get(text)
        if vector is None:
            return None
        self._cache.move_to_end(text)
        return vector

    def _cache_store(self, text: str, vector: EmbeddingVector) -> None:
        if self._cache_size == 0:
            return
        self._cache[text] = vector
        self._cache.move_to_end(text)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    def _arm_flusher(self) -> None:
        if self._flusher is not None and not self._flusher.done():
            return
        self._flusher = asyncio.get_running_loop().create_task(self._flush())

    async def _flush(self) -> None:
        try:
            # A zero window still yields once so calls started in the same tick
            # can join the batch that is about to be issued.
            await asyncio.sleep(self._coalesce_seconds)
            while self._queue:
                batch = self._take_batch()
                if not batch:
                    return
                self._batches += 1
                asyncio.get_running_loop().create_task(self._run_batch(self._batches, batch))
        finally:
            self._flusher = None
            if self._queue:
                self._arm_flusher()

    def _take_batch(self) -> list[str]:
        batch: list[str] = []
        seen: set[str] = set()
        while self._queue and len(batch) < self._max_batch_size:
            text = self._queue.pop(0)
            if text in seen or text not in self._pending:
                continue
            seen.add(text)
            batch.append(text)
        return batch

    async def _run_batch(self, batch_id: int, texts: list[str]) -> None:
        futures = {text: self._pending.get(text) for text in texts}
        try:
            result = await self._model.embed(tuple(texts))
            vectors = tuple(result.vectors)
            if len(vectors) != len(texts):
                raise ValueError(
                    f"embedding model returned {len(vectors)} vectors for {len(texts)} texts"
                )
            for text, vector in zip(texts, vectors, strict=True):
                self._cache_store(text, vector)
                future = futures.get(text)
                if future is not None and not future.done():
                    future.set_result(_Settled(vector=vector, batch=batch_id, usage=result.usage))
        except asyncio.CancelledError:
            for future in futures.values():
                if future is not None and not future.done():
                    future.cancel()
            raise
        except BaseException as exc:  # noqa: BLE001 - forwarded to every waiter
            for future in futures.values():
                if future is not None and not future.done():
                    future.set_exception(exc)
                    # Mark the failure as observed so a cancelled waiter cannot
                    # leave an unretrieved exception behind.
                    future.add_done_callback(lambda done: done.exception())
        finally:
            for text, future in futures.items():
                if self._pending.get(text) is future:
                    self._pending.pop(text, None)

"""HTTP reranker contract regressions and an actual loopback HTTP service."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory.http_reranker import HTTPMemoryReranker
from powercontext.builtin.artifacts.memory.models import MemoryHit


def _hits(count):
    return tuple(
        MemoryHit(
            memory_ref=ArtifactRef(family="memory", artifact_id="report", revision=5),
            entry_id=f"entry-{rank}",
            entry_version_id=f"version-{rank}",
            text=f"Evidence {rank}: calibration — 原文",
            score=1 / rank,
            matched_by=("fts",),
        )
        for rank in range(1, count + 1)
    )


@pytest.mark.parametrize("batch_size", [1, 2, 7, 32, 173, 174])
def test_all_batches_keep_global_ranks_and_client_lifetime(batch_size):
    seen = []
    candidates = _hits(173)
    original = tuple(hit.model_dump() for hit in candidates)

    def handler(request):
        body = json.loads(request.content)
        assert body["query"] == "question"
        assert 1 <= len(body["candidates"]) <= batch_size
        seen.extend(item["rank"] for item in body["candidates"])
        assert all(item["text"] == candidates[item["rank"] - 1].text for item in body["candidates"])
        return httpx.Response(
            200,
            json={
                "scores": [
                    {"rank": item["rank"], "score": (item["rank"] * 37) % 23 - 11}
                    for item in reversed(body["candidates"])
                ],
            },
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reranker = HTTPMemoryReranker(
                "https://score.invalid", client=client, batch_size=batch_size, policy_id="test"
            )
            decision = await reranker.rerank("question", candidates, 10)
            assert not client.is_closed
            assert reranker.policy_id == "test"
        expected = sorted(range(1, 174), key=lambda rank: (-((rank * 37) % 23 - 11), rank))[:10]
        assert decision.selected_ranks == tuple(expected)
        assert decision.usage.requests == (173 + batch_size - 1) // batch_size
        assert seen == list(range(1, 174))
        assert tuple(hit.model_dump() for hit in candidates) == original

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"scores": {}},
        {"scores": []},
        {"scores": [{"rank": True, "score": 1}]},
        {"scores": [{"rank": 1.0, "score": 1}]},
        {"scores": [{"rank": "1", "score": 1}]},
        {"scores": [{"rank": 1, "score": True}]},
        {"scores": [{"rank": 1, "score": "0.5"}]},
        {"scores": [{"rank": 1, "score": None}]},
        {"scores": [{"rank": 1, "score": float("nan")}]},
        {"scores": [{"rank": 1, "score": -float("inf")}]},
        {"scores": [{"rank": 1, "score": 1, "text": "rewrite"}]},
        {"scores": [{"rank": 1, "score": 1}], "usage": {}},
    ],
)
def test_rejects_schema_coercions_and_nonfinite_numbers(payload):
    async def scenario():
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=json.dumps(payload).encode()))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ValueError):
                await HTTPMemoryReranker("https://score.invalid", client=client).rerank("q", _hits(1), 1)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "payload", [b"not json", b'{"scores":[],"scores":[]}', b'{"scores":[{"rank":1,"rank":1,"score":0.5}]}']
)
def test_rejects_malformed_or_ambiguous_json(payload):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=payload))
        ) as client:
            with pytest.raises(ValueError):
                await HTTPMemoryReranker("https://score.invalid", client=client).rerank("q", _hits(1), 1)

    asyncio.run(scenario())


def test_later_batch_cannot_reuse_an_earlier_rank():
    async def scenario():
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"scores": [{"rank": 1, "score": 1}]}))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ValueError):
                await HTTPMemoryReranker("https://score.invalid", client=client, batch_size=1).rerank("q", _hits(2), 1)

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [302, 429, 500])
def test_http_status_errors_are_preserved(status):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status))) as client:
            with pytest.raises(httpx.HTTPStatusError) as caught:
                await HTTPMemoryReranker("https://score.invalid", client=client).rerank("q", _hits(1), 1)
            assert caught.value.response.status_code == status

    asyncio.run(scenario())


@pytest.mark.parametrize("limit", [0, -1, 2, True, 1.0, "1"])
def test_invalid_bounds_do_not_send_requests(limit):
    def handler(request):
        pytest.fail("invalid bounds must fail before sending HTTP")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError):
                await HTTPMemoryReranker("https://score.invalid", client=client).rerank("q", _hits(1), limit)

    asyncio.run(scenario())


def test_uses_an_actual_http_service():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            response = json.dumps({
                "scores": [{"rank": item["rank"], "score": item["rank"] % 3} for item in reversed(body["candidates"])]
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def scenario():
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            reranker = HTTPMemoryReranker(f"http://127.0.0.1:{server.server_port}/score", client=client, batch_size=2)
            decision = await reranker.rerank("calibration 校准", _hits(5), 3)
            assert decision.selected_ranks == (2, 5, 1)
            assert decision.usage.requests == 3

    try:
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert [item["rank"] for body in requests for item in body["candidates"]] == list(range(1, 6))


def test_redirect_is_not_an_unreported_extra_request():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(307, headers={"Location": "/other"})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await HTTPMemoryReranker("https://score.invalid", client=client).rerank("q", _hits(1), 1)
        assert len(seen) == 1

    asyncio.run(scenario())

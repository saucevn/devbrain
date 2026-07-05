"""P1-4: transient API errors must back off and retry; permanently-failing
events (404/410/422) must become PoisonEvent so the cursor can advance."""
import asyncio

import httpx
import pytest

import pipeline


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "http://x")
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(code, request=req))


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    async def instant(_secs: float) -> None:
        return None
    monkeypatch.setattr(asyncio, "sleep", instant)


def test_retries_transient_503_then_succeeds():
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _status_error(503)
        return "ok"

    assert asyncio.run(pipeline.with_retries(fn)) == "ok"
    assert calls["n"] == 3


def test_404_raises_poison_event_immediately():
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        raise _status_error(404)

    with pytest.raises(pipeline.PoisonEvent):
        asyncio.run(pipeline.with_retries(fn))
    assert calls["n"] == 1


def test_non_retryable_400_raises_original_error():
    async def fn():
        raise _status_error(400)

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(pipeline.with_retries(fn))


def test_exhausted_retries_raise_last_error():
    async def fn():
        raise _status_error(429)

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(pipeline.with_retries(fn, attempts=2))

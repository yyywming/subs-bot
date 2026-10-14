"""Subscription fetch safety regressions. No network."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiohttp import StreamReader

from convert import MAX_SUBSCRIPTION_BYTES, fetch_subscription  # noqa: E402


class FakeContent:
    def __init__(self, body: bytes):
        self.body = body
        self.offset = 0

    async def read(self, limit: int) -> bytes:
        chunk = self.body[self.offset:self.offset + limit]
        self.offset += len(chunk)
        return chunk


class FakeResponse:
    def __init__(self, status: int, *, headers=None, body: bytes = b""):
        self.status = status
        self.headers = headers or {}
        self.charset = "utf-8"
        self.content = FakeContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, _type, _value, _tb):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls: list[str] = []

    def get(self, url: str, **_kwargs):
        self.urls.append(url)
        return self.responses.pop(0)


async def test_redirect_target_is_revalidated():
    session = FakeSession([
        FakeResponse(302, headers={"Location": "http://127.0.0.1/internal"}),
    ])
    checked: list[str] = []

    async def check(url: str) -> None:
        checked.append(url)
        if "127.0.0.1" in url:
            raise ValueError("private redirect blocked")

    with patch("convert.get_session", new=AsyncMock(return_value=session)):
        with patch("convert._check_public_url", side_effect=check):
            nodes, _meta, error = await fetch_subscription("https://public.example/sub")
    assert nodes == []
    assert "private redirect blocked" in (error or "")
    assert checked == ["https://public.example/sub", "http://127.0.0.1/internal"]
    assert session.urls == ["https://public.example/sub"]


async def test_response_size_is_capped():
    session = FakeSession([
        FakeResponse(200, body=b"x" * (MAX_SUBSCRIPTION_BYTES + 1)),
    ])
    with patch("convert.get_session", new=AsyncMock(return_value=session)):
        with patch("convert._check_public_url", new=AsyncMock()):
            nodes, _meta, error = await fetch_subscription("https://public.example/sub")
    assert nodes == []
    assert error == "订阅响应过大"


async def test_fragmented_response_is_read_to_eof():
    body = (
        b'proxies:\n  - name: streamed\n    type: ss\n'
        b'    server: example.com\n    port: 443\n'
        b'    cipher: aes-256-gcm\n    password: secret\n'
    )
    protocol = Mock(_reading_paused=False)
    reader = StreamReader(protocol, limit=2**16)
    reader.feed_data(body[:18])
    response = FakeResponse(200)
    response.content = reader
    session = FakeSession([response])

    async def finish_body():
        await asyncio.sleep(0)
        reader.feed_data(body[18:])
        reader.feed_eof()

    producer = asyncio.create_task(finish_body())
    try:
        with patch("convert.get_session", new=AsyncMock(return_value=session)), patch(
            "convert._check_public_url", new=AsyncMock()
        ):
            nodes, _meta, error = await fetch_subscription("https://public.example/sub")
    finally:
        await producer
    assert error is None
    assert nodes == [{
        "name": "streamed", "type": "ss", "server": "example.com",
        "port": 443, "cipher": "aes-256-gcm", "password": "secret", "share": "",
    }]


async def test_response_exactly_at_limit_is_accepted():
    body = b"#" + b" " * (MAX_SUBSCRIPTION_BYTES - 1)
    session = FakeSession([FakeResponse(200, body=body)])
    with patch("convert.get_session", new=AsyncMock(return_value=session)), patch(
        "convert._check_public_url", new=AsyncMock()
    ):
        nodes, _meta, error = await fetch_subscription("https://public.example/sub")
    assert error is None
    assert nodes == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            asyncio.run(fn())
            print("ok", name)
    print("all passed")

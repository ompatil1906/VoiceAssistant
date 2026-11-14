#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Tests for the streaming Gemini client.

The voice loop depends on two properties here: text arrives incrementally so
synthesis can start, and the terminal chunk carries the tool calls and usage
that the turn needs. Reasoning ("thought") parts must never reach the mouth.
"""

from __future__ import annotations

import json

import httpx
import pytest

from emma.services.gemini_client import GeminiClient


def sse(*payloads: dict) -> bytes:
    """Encode Gemini SSE frames."""
    body = ""
    for payload in payloads:
        body += f"data: {json.dumps(payload)}\r\n\r\n"
    return body.encode()


def text_frame(text: str, *, thought: bool = False) -> dict:
    """A candidate chunk containing text."""
    part: dict = {"text": text}
    if thought:
        part["thought"] = True
    return {"candidates": [{"content": {"parts": [part]}}]}


def call_frame(name: str, args: dict | None = None) -> dict:
    """A candidate chunk containing a function call."""
    return {
        "candidates": [
            {
                "content": {
                    "parts": [{"functionCall": {"name": name, "args": args or {}}}]
                }
            }
        ]
    }


async def collect(client: GeminiClient, **kwargs) -> list:
    """Drain the stream into a list."""
    return [
        chunk
        async for chunk in client.stream(
            system_instruction="You are EMMA.",
            contents=[{"role": "user", "parts": [{"text": "hello"}]}],
            **kwargs,
        )
    ]


@pytest.mark.asyncio
async def test_stream_yields_text_incrementally_and_finishes_with_usage():
    """Text deltas arrive separately; the last chunk carries finish and tokens."""
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        assert "streamGenerateContent" in str(request.url)
        assert request.url.params.get("alt") == "sse"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse(
                text_frame("The clinic is open "),
                text_frame("from eight until six."),
                {
                    "candidates": [
                        {
                            "content": {"parts": [{"text": ""}]},
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {"totalTokenCount": 42},
                },
            ),
        )

    client = GeminiClient(api_key="test")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: SLF001
    chunks = await collect(client)

    assert "".join(chunk.text for chunk in chunks) == "The clinic is open from eight until six."
    terminal = chunks[-1]
    assert terminal.final is True
    assert terminal.finish_reason == "STOP"
    assert terminal.tokens == 42
    assert terminal.first_text_ms > 0
    assert calls["count"] == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_reasoning_parts_are_never_spoken():
    """A thinking model's deliberation must not be read out loud."""
    payload = sse(
        text_frame("The caller probably wants an appointment. ", thought=True),
        text_frame("I can help with that."),
        {"candidates": [{"content": {"parts": []}, "finishReason": "STOP"}]},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=payload)

    client = GeminiClient(api_key="test")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: SLF001
    chunks = await collect(client)

    spoken = "".join(chunk.text for chunk in chunks)
    assert spoken == "I can help with that."
    assert "probably wants" not in spoken
    await client.aclose()


@pytest.mark.asyncio
async def test_function_calls_are_reported_on_the_terminal_chunk():
    """Tool rounds are recognised from the stream, not only from a full reply."""
    payload = sse(
        call_frame("get_clinic_info", {"topic": "hours"}),
        {"candidates": [{"content": {"parts": []}, "finishReason": "STOP"}]},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=payload)

    client = GeminiClient(api_key="test")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: SLF001
    chunks = await collect(client)

    assert not any(chunk.text for chunk in chunks)
    terminal = chunks[-1]
    assert [call.name for call in terminal.function_calls] == ["get_clinic_info"]
    assert terminal.function_calls[0].arguments == {"topic": "hours"}
    await client.aclose()


@pytest.mark.asyncio
async def test_server_error_is_retried_once_before_any_text():
    """A transient 5xx is retried; the caller must not see a failed turn."""
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(503, content=b'{"error": "unavailable"}')
        return httpx.Response(200, content=sse(text_frame("Hello.")))

    client = GeminiClient(api_key="test")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: SLF001
    chunks = await collect(client)

    assert "".join(chunk.text for chunk in chunks) == "Hello."
    assert attempts["count"] == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_a_client_error_is_reported_as_a_failed_stream():
    """A 4xx surfaces as an error chunk so the turn can fall back."""
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(400, content=b'{"error": "bad request"}')

    client = GeminiClient(api_key="test")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: SLF001
    chunks = await collect(client)

    assert len(chunks) == 1
    assert chunks[0].final is True
    assert chunks[0].error
    assert chunks[0].text == ""
    await client.aclose()


@pytest.mark.asyncio
async def test_malformed_json_frames_are_skipped():
    """One corrupt frame must not abort the rest of the reply."""
    body = b"data: not-json\r\n\r\ndata: " + json.dumps(text_frame("Still here.")).encode()
    body += b"\r\n\r\n"

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=body)

    client = GeminiClient(api_key="test")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: SLF001
    chunks = await collect(client)

    assert "".join(chunk.text for chunk in chunks) == "Still here."
    await client.aclose()

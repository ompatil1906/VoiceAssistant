#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Minimal Gemini client with function calling for the voice assistant.

Only the REST surface the receptionist needs is implemented: one
``generateContent`` call with system instruction, conversation turns and tool
declarations. The HTTP client is kept alive for the whole session because the
TLS handshake dominates the first token on a high-latency link; warming it while
the greeting plays removes seconds from the first caller turn.

Nothing here logs message bodies: transcripts and tool arguments can contain
patient identifiers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

try:
    from loguru import logger
except ImportError:  # pragma: no cover - loguru is a runtime dependency
    import logging

    logger = logging.getLogger("emma.gemini_client")

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"

# httpx logs every request URL, which would put the API key and prompt size in
# the log. The client logs its own redacted line instead.
logging.getLogger("httpx").setLevel(logging.WARNING)
DEFAULT_MODEL = "gemini-3.8-flash"


@dataclass
class FunctionCall:
    """One tool invocation requested by the model."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelReply:
    """The assistant's reply for one turn.

    Parameters:
        text: Spoken text, possibly empty when the model asked for a tool.
        function_calls: Tools to execute before the next model request.
        finish_reason: Gemini's finish reason, for diagnostics.
        latency_ms: Time to complete the request.
        tokens: Total token count reported by the API.
        error: Set when the request could not be completed.
    """

    text: str = ""
    function_calls: list[FunctionCall] = field(default_factory=list)
    finish_reason: str = ""
    latency_ms: float = 0.0
    tokens: int = 0
    error: str | None = None

    @property
    def needs_tools(self) -> bool:
        """Whether the model asked for at least one tool."""
        return bool(self.function_calls)


class GeminiClient:
    """A persistent Gemini conversation client.

    Parameters:
        api_key: Gemini API key.
        model: Model identifier; defaults to the ``EMMA_GEMINI_MODEL`` value.
        timeout: Per-request timeout in seconds.
        budget: Optional session budget that meters requests and tokens.
    """

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        *,
        timeout: float = 30.0,
        budget: Any | None = None,
    ) -> None:
        """Store the credentials and model; connections open on first use."""
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._budget = budget
        self._client: httpx.AsyncClient | None = None
        self._quota_exhausted = False

    @property
    def model(self) -> str:
        """The model identifier in use."""
        return self._model

    async def __aenter__(self) -> GeminiClient:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout, connect=15.0),
            limits=httpx.Limits(max_keepalive_connections=2, keepalive_expiry=300.0),
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _url(self) -> str:
        return f"{GEMINI_ENDPOINT}/{self._model}:generateContent"

    async def warmup(self) -> None:
        """Open the TLS connection and prime the model with a tiny request."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=15.0),
                limits=httpx.Limits(max_keepalive_connections=2, keepalive_expiry=300.0),
            )
        payload = {
            "contents": [
                {"role": "user", "parts": [{"text": "Reply with the single word: ready"}]}
            ],
            "generationConfig": {
                "temperature": 0.0,
                "maxOutputTokens": 8,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        started = time.perf_counter()
        try:
            response = await self._client.post(
                self._url(),
                headers={"x-goog-api-key": self._api_key},
                json=payload,
            )
            response.raise_for_status()
            logger.info(f"[Gemini] connection warm in {time.perf_counter() - started:.2f}s")
        except Exception as exc:  # warm-up is best effort
            logger.warning(f"[Gemini] warm-up skipped: {type(exc).__name__}: {exc}")

    async def generate(
        self,
        *,
        system_instruction: str,
        contents: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        temperature: float = 0.25,
        max_output_tokens: int = 400,
    ) -> ModelReply:
        """Ask the model for the next assistant turn.

        Args:
            system_instruction: Grounding text for the receptionist.
            contents: Gemini conversation turns (``role``/``parts``).
            tools: Optional tool declarations (``[{"function_declarations": [...]}]``).
            temperature: Sampling temperature; low keeps confirmations faithful.
            max_output_tokens: Output ceiling; replies are spoken, so short.

        Returns:
            A :class:`ModelReply`; ``error`` is set instead of raising so the
            caller always has something to say.
        """
        if self._client is None:
            await self.__aenter__()
        assert self._client is not None

        if self._quota_exhausted:
            return ModelReply(
                text=(
                    "I'm sorry, this call has reached its usage limit. "
                    "Please call the practice directly."
                ),
                error="quota-exhausted",
            )
        if self._budget is not None:
            try:
                self._budget.reserve_request()
            except Exception as exc:
                self._quota_exhausted = True
                logger.warning(f"[Gemini] budget refused the request: {exc}")
                return ModelReply(
                    text=(
                        "I'm sorry, this call has reached its usage limit. "
                        "Please call the practice directly."
                    ),
                    error=str(exc),
                )

        payload: dict[str, Any] = {
            "system_instruction": {"parts": [{"text": system_instruction}]},
            "contents": list(contents),
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_output_tokens,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        if tools:
            payload["tools"] = list(tools)

        started = time.perf_counter()
        response: httpx.Response | None = None
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = await self._client.post(
                    self._url(),
                    headers={"x-goog-api-key": self._api_key},
                    json=payload,
                )
                if response.status_code >= 500 and attempt == 0:
                    last_error = RuntimeError(f"HTTP {response.status_code}")
                    await asyncio.sleep(0.4)
                    continue
                response.raise_for_status()
                last_error = None
                break
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(0.3)
                    continue
            except httpx.HTTPStatusError as exc:
                last_error = exc
                break

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if response is None or last_error is not None:
            logger.error(f"[Gemini] request failed after {elapsed_ms:.0f}ms: {last_error}")
            return ModelReply(
                text="I'm having trouble reaching the practice system. Could you say that again?",
                latency_ms=elapsed_ms,
                error=str(last_error),
            )

        try:
            data: dict[str, Any] = response.json()
        except (ValueError, json.JSONDecodeError):
            return ModelReply(
                text="I'm having trouble reaching the practice system. Could you say that again?",
                latency_ms=elapsed_ms,
                error="invalid-json",
            )

        usage = data.get("usageMetadata") or {}
        tokens = int(usage.get("totalTokenCount") or 0)
        if self._budget is not None and tokens:
            try:
                self._budget.record_tokens(tokens)
            except Exception as exc:
                self._quota_exhausted = True
                logger.warning(f"[Gemini] token allowance exceeded: {exc}")

        candidates = data.get("candidates") or []
        if not candidates:
            return ModelReply(
                text="I'm sorry, I could not form a reply. Could you say that again?",
                latency_ms=elapsed_ms,
                tokens=tokens,
                error="no-candidates",
            )

        candidate = candidates[0]
        finish_reason = str(candidate.get("finishReason", ""))
        parts = (candidate.get("content") or {}).get("parts") or []
        text_chunks: list[str] = []
        calls: list[FunctionCall] = []
        for part in parts:
            if "text" in part and isinstance(part["text"], str):
                text_chunks.append(part["text"])
            call = part.get("functionCall") or part.get("function_call")
            if isinstance(call, dict) and call.get("name"):
                args = call.get("args")
                calls.append(
                    FunctionCall(
                        name=str(call["name"]),
                        arguments=dict(args) if isinstance(args, dict) else {},
                    )
                )

        latency_ms = (time.perf_counter() - started) * 1000.0
        logger.info(
            f"[Gemini] reply in {latency_ms:.0f}ms tokens={tokens} "
            f"tool_calls={[call.name for call in calls]} finish={finish_reason}"
        )
        return ModelReply(
            text=sanitize_spoken_text(" ".join(text_chunks)),
            function_calls=calls,
            finish_reason=finish_reason,
            latency_ms=latency_ms,
            tokens=tokens,
        )


_MARKDOWN_CHARS = "*_#`>|"


def sanitize_spoken_text(text: str) -> str:
    """Strip markup a text-to-speech engine would read out loud.

    Args:
        text: Raw model output.

    Returns:
        Plain spoken text with markdown, citations and extra whitespace removed.
    """
    if not text:
        return ""
    cleaned = text.replace("\u2019", "'").replace("\u2014", ",").replace("\u2013", ",")
    cleaned = cleaned.replace("**", "").replace("__", "")
    for char in _MARKDOWN_CHARS:
        cleaned = cleaned.replace(char, "")
    cleaned = cleaned.replace("\n", " ")
    cleaned = " ".join(cleaned.split())
    return cleaned.strip()

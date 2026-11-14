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
from collections.abc import AsyncIterator, Sequence
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


@dataclass
class StreamChunk:
    """One increment of a streamed model response.

    Text chunks are yielded as soon as the model produces them so synthesis can
    start before the reply is complete. The terminal chunk carries the tool
    calls (if any), the finish reason and the token usage.

    Parameters:
        text: New spoken text, empty for non-text chunks.
        function_calls: Tools requested by the model; only on the final chunk.
        finish_reason: Gemini's finish reason.
        tokens: Total tokens used, from ``usageMetadata``.
        error: Set when the stream failed before producing any text.
        final: True for the last chunk of this stream.
        latency_ms: Time from request start to this chunk.
        first_text_ms: Time from request start to the first text chunk.
    """

    text: str = ""
    function_calls: list[FunctionCall] = field(default_factory=list)
    finish_reason: str = ""
    tokens: int = 0
    error: str | None = None
    final: bool = False
    latency_ms: float = 0.0
    first_text_ms: float = 0.0


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

    def _stream_url(self) -> str:
        return f"{GEMINI_ENDPOINT}/{self._model}:streamGenerateContent?alt=sse"

    def _quota_reply(self, reason: str) -> ModelReply:
        """The spoken refusal used when the budget or quota blocks a request."""
        self._quota_exhausted = True
        return ModelReply(
            text=(
                "I'm sorry, this call has reached its usage limit. "
                "Please call the practice directly."
            ),
            error=reason,
        )

    def _reserve(self) -> ModelReply | None:
        """Reserve one metered request, returning the refusal reply on failure."""
        if self._budget is None:
            return None
        try:
            self._budget.reserve_request()
        except Exception as exc:
            logger.warning(f"[Gemini] budget refused the request: {exc}")
            return self._quota_reply(str(exc))
        return None

    def _record_tokens(self, tokens: int) -> None:
        """Record token usage, flipping the session to exhausted on refusal."""
        if self._budget is None or not tokens:
            return
        try:
            self._budget.record_tokens(tokens)
        except Exception as exc:
            self._quota_exhausted = True
            logger.warning(f"[Gemini] token allowance exceeded: {exc}")

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
        refused = self._reserve()
        if refused is not None:
            return refused

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
        self._record_tokens(tokens)

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

    async def stream(
        self,
        *,
        system_instruction: str,
        contents: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        temperature: float = 0.25,
        max_output_tokens: int = 400,
    ) -> AsyncIterator[StreamChunk]:
        """Stream the next assistant turn token by token.

        Text is yielded as soon as the model produces it, so the caller can
        synthesise and play the first sentence while the rest of the reply is
        still being generated. Thought parts are never yielded: a reasoning
        model would otherwise read its own deliberation out loud.

        Args:
            system_instruction: Grounding text for the receptionist.
            contents: Gemini conversation turns (``role``/``parts``).
            tools: Optional tool declarations.
            temperature: Sampling temperature.
            max_output_tokens: Output ceiling; replies are spoken, so short.

        Yields:
            :class:`StreamChunk` increments; the last one has ``final=True``.
        """
        if self._client is None:
            await self.__aenter__()
        assert self._client is not None

        started = time.perf_counter()
        if self._quota_exhausted:
            yield StreamChunk(error="quota-exhausted", final=True)
            return
        refused = self._reserve()
        if refused is not None:
            yield StreamChunk(text=refused.text, error=refused.error, final=True)
            return

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

        emitted_chars = 0
        first_text_ms = 0.0
        tokens = 0
        finish_reason = ""
        calls: list[FunctionCall] = []
        last_error = ""

        for attempt in range(2):
            try:
                async with self._client.stream(
                    "POST",
                    self._stream_url(),
                    headers={"x-goog-api-key": self._api_key},
                    json=payload,
                ) as response:
                    if response.status_code >= 500 and attempt == 0:
                        await response.aread()
                        last_error = f"HTTP {response.status_code}"
                        await asyncio.sleep(0.25)
                        continue
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", "replace")
                        last_error = f"HTTP {response.status_code}: {body[:200]}"
                        break

                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        data = line[len("data:") :].strip() if line.startswith("data:") else ""
                        if not data or data == "[DONE]":
                            continue
                        try:
                            message = json.loads(data)
                        except ValueError:
                            continue

                        usage = message.get("usageMetadata") or {}
                        tokens = int(usage.get("totalTokenCount") or tokens)
                        for candidate in message.get("candidates") or []:
                            finish_reason = str(
                                candidate.get("finishReason") or finish_reason
                            )
                            parts = (candidate.get("content") or {}).get("parts") or []
                            for part in parts:
                                if part.get("thought"):
                                    # Deliberation, not speech.
                                    continue
                                text = part.get("text")
                                if isinstance(text, str) and text:
                                    # Sanitise per delta but keep its edge
                                    # whitespace: deltas split mid-phrase and a
                                    # collapsed trailing space would glue the
                                    # next delta's first word onto this one.
                                    cleaned = sanitize_stream_delta(text)
                                    if not cleaned:
                                        continue
                                    elapsed_ms = (
                                        time.perf_counter() - started
                                    ) * 1000.0
                                    if not first_text_ms:
                                        first_text_ms = elapsed_ms
                                        logger.info(
                                            f"[Gemini] first text in {first_text_ms:.0f}ms"
                                        )
                                    emitted_chars += len(cleaned)
                                    yield StreamChunk(
                                        text=cleaned,
                                        latency_ms=elapsed_ms,
                                        first_text_ms=first_text_ms,
                                    )
                                call = part.get("functionCall") or part.get("function_call")
                                if isinstance(call, dict) and call.get("name"):
                                    args = call.get("args")
                                    calls.append(
                                        FunctionCall(
                                            name=str(call["name"]),
                                            arguments=dict(args)
                                            if isinstance(args, dict)
                                            else {},
                                        )
                                    )
                    last_error = ""
                    break
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                # A stream that already produced text cannot be replayed
                # without duplicating it; fail the turn instead.
                if attempt == 0 and emitted_chars == 0:
                    await asyncio.sleep(0.3)
                    continue
                break
            except Exception as exc:  # pragma: no cover - defensive
                last_error = f"{type(exc).__name__}: {exc}"
                break

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self._record_tokens(tokens)
        if last_error:
            logger.error(f"[Gemini] stream failed after {elapsed_ms:.0f}ms: {last_error}")
            yield StreamChunk(error=last_error, final=True, latency_ms=elapsed_ms)
            return
        logger.info(
            f"[Gemini] stream done in {elapsed_ms:.0f}ms first_text={first_text_ms:.0f}ms "
            f"chars={emitted_chars} tokens={tokens} "
            f"tool_calls={[call.name for call in calls]} finish={finish_reason}"
        )
        yield StreamChunk(
            function_calls=calls,
            finish_reason=finish_reason,
            tokens=tokens,
            final=True,
            latency_ms=elapsed_ms,
            first_text_ms=first_text_ms,
        )


_MARKDOWN_CHARS = "*_#`>|"


def sanitize_stream_delta(text: str) -> str:
    """Strip markup from one streamed delta, preserving word boundaries.

    Args:
        text: Raw model delta.

    Returns:
        The delta with markdown removed and internal whitespace collapsed, but
        its leading and trailing space kept when the model produced one.
    """
    if not text:
        return ""
    cleaned = text.replace("\u2019", "'").replace("\u2014", ",").replace("\u2013", ",")
    cleaned = cleaned.replace("**", "").replace("__", "")
    for char in _MARKDOWN_CHARS:
        cleaned = cleaned.replace(char, "")
    cleaned = cleaned.replace("\n", " ")
    leading = " " if cleaned[:1].isspace() else ""
    trailing = " " if cleaned[-1:].isspace() else ""
    core = " ".join(cleaned.split())
    if not core:
        return " " if (leading or trailing) else ""
    return f"{leading}{core}{trailing}"


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

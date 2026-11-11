#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Streaming text-to-speech for the LiveKit agent.

The assistant must start speaking before the whole answer has been synthesised.
:class:`ElevenLabsStreamingTTS` reads the provider's chunked PCM response and
yields audio as it arrives; :class:`SystemTTS` renders locally with macOS
``say`` when the remote provider is unavailable, so a call never goes silent
because of a network failure.

Both produce mono PCM16 at the requested sample rate (24 kHz for LiveKit here).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import httpx

try:
    from loguru import logger
except ImportError:  # pragma: no cover - loguru is a runtime dependency
    import logging

    logger = logging.getLogger("emma.streaming_tts")

ELEVENLABS_BASE = "https://api.elevenlabs.io/v1/text-to-speech"
DEFAULT_SAMPLE_RATE = 24_000
DEFAULT_MODEL = "eleven_flash_v2_5"
DEFAULT_VOICE = "CwhRBWXzGAHq8TQ4Fs17"


@dataclass
class SynthesisResult:
    """Timing for one synthesis request, for latency telemetry."""

    provider: str
    characters: int
    first_byte_ms: float = 0.0
    total_ms: float = 0.0
    failed: bool = False


class ElevenLabsStreamingTTS:
    """Chunked ElevenLabs synthesis.

    Parameters:
        api_key: ElevenLabs API key.
        voice_id: Voice identifier.
        model: ElevenLabs model, e.g. ``eleven_flash_v2_5``.
        sample_rate: Output sample rate; 24 kHz matches LiveKit here.
        budget: Optional session budget, metered in characters.
    """

    def __init__(
        self,
        api_key: str,
        voice_id: str = DEFAULT_VOICE,
        *,
        model: str = DEFAULT_MODEL,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        budget: object | None = None,
    ) -> None:
        """Store the remote voice configuration; the client opens on first use."""
        self._api_key = api_key
        self._voice_id = voice_id
        self._model = model
        self._sample_rate = sample_rate
        self._budget = budget
        self._client: httpx.AsyncClient | None = None
        self.last_result: SynthesisResult | None = None
        # Parsed provider error, used to explain a fallback to the caller.
        self.last_error_code = ""
        self.last_error_message = ""
        self.last_error_fatal = False

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(45.0, connect=15.0),
                limits=httpx.Limits(max_keepalive_connections=2, keepalive_expiry=300.0),
            )
        return self._client

    async def aclose(self) -> None:
        """Close the HTTP connection pool."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _url(self) -> str:
        return f"{ELEVENLABS_BASE}/{self._voice_id}/stream?output_format=pcm_{self._sample_rate}"

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        """Yield PCM16 chunks for ``text`` as the provider produces them.

        Args:
            text: Sentence or short paragraph to speak.

        Yields:
            Mono PCM16 little-endian audio at :attr:`sample_rate`.
        """
        clean = text.strip()
        if not clean or len(clean) < 2:
            return
        self.last_error_code = ""
        self.last_error_message = ""
        self.last_error_fatal = False
        if self._budget is not None:
            try:
                self._budget.reserve_request()  # type: ignore[attr-defined]
                self._budget.record_characters(len(clean))  # type: ignore[attr-defined]
            except Exception as exc:
                logger.warning(f"[TTS] budget refused the request: {exc}")
                return

        payload = {
            "text": clean,
            "model_id": self._model,
            "voice_settings": {"stability": 0.45, "similarity_boost": 0.75},
        }
        client = await self._http()
        result = SynthesisResult(provider="elevenlabs", characters=len(clean))
        started = time.perf_counter()
        try:
            async with client.stream(
                "POST",
                self._url(),
                headers={
                    "xi-api-key": self._api_key,
                    "content-type": "application/json",
                    "accept": "audio/pcm",
                },
                json=payload,
            ) as response:
                if response.status_code >= 400:
                    # Read the body before leaving the context manager: after
                    # that the response is closed and the reason is lost.
                    body = (await response.aread()).decode("utf-8", "replace")
                    self._record_error(response.status_code, body)
                    result.failed = True
                    logger.warning(
                        f"[TTS] ElevenLabs rejected the request after "
                        f"{(time.perf_counter() - started) * 1000:.0f}ms: HTTP "
                        f"{response.status_code} {self.last_error_code}: "
                        f"{self.last_error_message or body[:200]}"
                    )
                    return
                async for chunk in response.aiter_bytes(960):
                    if not chunk:
                        continue
                    if not result.first_byte_ms:
                        result.first_byte_ms = (time.perf_counter() - started) * 1000.0
                        logger.info(
                            f"[TTS] first audio in {result.first_byte_ms:.0f}ms "
                            f"({len(clean)} chars)"
                        )
                    yield chunk
        except httpx.HTTPStatusError as exc:
            result.failed = True
            detail = ""
            try:
                await exc.response.aread()
                detail = exc.response.text[:300]
            except Exception:
                pass
            self._record_error(exc.response.status_code, detail)
            logger.warning(
                f"[TTS] ElevenLabs rejected the request after "
                f"{(time.perf_counter() - started) * 1000:.0f}ms: HTTP "
                f"{exc.response.status_code} {self.last_error_code}: "
                f"{self.last_error_message or detail}"
            )
        except Exception as exc:
            result.failed = True
            logger.warning(
                f"[TTS] ElevenLabs failed after {(time.perf_counter() - started) * 1000:.0f}ms: "
                f"{type(exc).__name__}: {exc}"
            )
        finally:
            result.total_ms = (time.perf_counter() - started) * 1000.0
            self.last_result = result

    def _record_error(self, status: int, body: str) -> None:
        """Parse an ElevenLabs error body so the fallback can be explained."""
        try:
            payload = json.loads(body)
            detail = payload.get("detail") if isinstance(payload, dict) else None
            if isinstance(detail, dict):
                self.last_error_code = str(detail.get("code") or "")
                self.last_error_message = str(detail.get("message") or "")
        except (ValueError, TypeError):
            pass
        if not self.last_error_code:
            self.last_error_code = "http_401" if status == 401 else f"http_{status}"
        # A key or quota problem will not fix itself mid-call; a timeout might.
        self.last_error_fatal = status in (401, 402, 403) or (
            status == 429 and "quota" in self.last_error_code
        )

    async def synthesize(self, text: str) -> bytes:
        """Return the whole clip, used by callers that do not stream."""
        chunks = [chunk async for chunk in self.stream(text)]
        return b"".join(chunks)


class SystemTTS:
    """Local macOS speech synthesis with ``say`` and ``afconvert``.

    This is the zero-network fallback: it keeps the call audible when the
    provider is down or the API key is rejected. It is not available on Linux,
    where :meth:`available` returns False.
    """

    def __init__(self, sample_rate: int = DEFAULT_SAMPLE_RATE, voice: str = "Samantha") -> None:
        """Configure the local voice and output rate."""
        self._sample_rate = sample_rate
        self._voice = voice
        self.last_result: SynthesisResult | None = None

    @staticmethod
    def available() -> bool:
        """Whether the local synthesiser exists on this host."""
        return bool(shutil.which("say") and shutil.which("afconvert"))

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        """Yield the rendered PCM16 clip."""
        audio = await asyncio.to_thread(self._render, text)
        if audio:
            yield audio

    async def synthesize(self, text: str) -> bytes:
        """Return the rendered clip as bytes."""
        return await asyncio.to_thread(self._render, text)

    def _render(self, text: str) -> bytes:
        clean = text.strip()
        if not clean or not self.available():
            return b""
        result = SynthesisResult(provider="system", characters=len(clean))
        started = time.perf_counter()
        aiff_path = wav_path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as handle:
                aiff_path = handle.name
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                wav_path = handle.name

            import subprocess

            subprocess.run(
                ["say", "-v", self._voice, clean, "-o", aiff_path],
                check=True,
                capture_output=True,
                timeout=30,
            )
            subprocess.run(
                [
                    "afconvert",
                    "-f",
                    "WAVE",
                    "-d",
                    f"LEI16@{self._sample_rate}",
                    "-c",
                    "1",
                    aiff_path,
                    wav_path,
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            import wave

            with wave.open(wav_path, "rb") as handle:
                audio = handle.readframes(handle.getnframes())
            result.first_byte_ms = (time.perf_counter() - started) * 1000.0
            logger.info(f"[TTS] system voice rendered in {result.first_byte_ms:.0f}ms")
            return audio
        except Exception as exc:
            result.failed = True
            logger.warning(f"[TTS] system voice failed: {type(exc).__name__}: {exc}")
            return b""
        finally:
            result.total_ms = (time.perf_counter() - started) * 1000.0
            self.last_result = result
            for path in (aiff_path, wav_path):
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass


class TTSRouter:
    """Choose a synthesis provider and fail over to the local voice.

    Parameters:
        provider: ``elevenlabs``, ``system`` or ``auto`` (remote, then local).
        elevenlabs: Configured remote provider, if any.
        system: Local provider, if available.
        on_fallback: Called with a human-readable reason when the primary
            provider fails and the local one takes over.
        sample_rate: Output sample rate for both providers.
    """

    def __init__(
        self,
        *,
        provider: str,
        elevenlabs: ElevenLabsStreamingTTS | None,
        system: SystemTTS | None,
        on_fallback: Callable[[str], None] | None = None,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
    ) -> None:
        """Store the provider chain and the fallback callback."""
        self._provider = provider
        self._elevenlabs = elevenlabs
        self._system = system
        self._on_fallback = on_fallback
        self._sample_rate = sample_rate
        self._remote_disabled = elevenlabs is None
        self._remote_failures = 0
        self._fallback_notified = False

    @property
    def sample_rate(self) -> int:
        """Sample rate of the audio this router produces."""
        return self._sample_rate

    async def aclose(self) -> None:
        """Release provider resources."""
        if self._elevenlabs is not None:
            await self._elevenlabs.aclose()

    def _fallback_reason(self) -> str:
        """A caller-safe explanation of why the local voice took over."""
        code = (self._elevenlabs.last_error_code if self._elevenlabs else "").lower()
        message = self._elevenlabs.last_error_message if self._elevenlabs else ""
        permission = re.search(r"missing the permission ([a-zA-Z_]+)", message)

        if permission:
            return (
                f"The ElevenLabs API key is missing the {permission.group(1)} "
                "permission; using the local voice for this call."
            )
        if code == "quota_exceeded":
            return "The ElevenLabs voice quota is exhausted; using the local voice for this call."
        if code == "paid_plan_required":
            return (
                "The selected ElevenLabs voice needs a paid plan; "
                "using the local voice for this call."
            )
        if code == "voice_not_found":
            return (
                "The selected ElevenLabs voice was not found; using the local voice for this call."
            )
        if code in ("invalid_api_key", "missing_permissions", "unauthorized"):
            return "The ElevenLabs API key was rejected; using the local voice for this call."
        if message:
            return (
                f"The online voice is unavailable ({code or 'provider error'}); "
                "using the local voice for this call."
            )
        return "The online voice is unavailable, switching to the local voice."

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        """Yield PCM16 audio for ``text``, using the fallback on failure."""
        use_remote = self._provider in ("elevenlabs", "auto") and not self._remote_disabled
        if use_remote and self._elevenlabs is not None:
            produced = False
            async for chunk in self._elevenlabs.stream(text):
                produced = True
                yield chunk
            if produced:
                self._remote_failures = 0
                return
            self._remote_failures += 1
            if self._elevenlabs.last_error_fatal or self._remote_failures >= 3:
                # A rejected key or exhausted quota will not recover mid-call;
                # stop paying the round trip on every reply.
                self._remote_disabled = True
            if self._system is None or not SystemTTS.available():
                return
            reason = self._fallback_reason()
            logger.warning(f"[TTS] {reason} (remote failures: {self._remote_failures})")
            if self._on_fallback is not None and not self._fallback_notified:
                self._fallback_notified = True
                self._on_fallback(reason)

        if self._system is not None:
            async for chunk in self._system.stream(text):
                yield chunk


def build_tts_router(
    *,
    provider: str,
    elevenlabs_api_key: str,
    voice_id: str,
    model: str,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    budget: object | None = None,
    on_fallback: Callable[[str], None] | None = None,
) -> TTSRouter:
    """Build the provider chain described by the environment.

    Args:
        provider: ``elevenlabs``, ``system`` or ``auto``.
        elevenlabs_api_key: Key for the remote provider; empty disables it.
        voice_id: Remote voice identifier.
        model: Remote model identifier.
        sample_rate: Output sample rate.
        budget: Optional session budget, metered in characters.
        on_fallback: Callback invoked when the local voice takes over.

    Returns:
        A configured :class:`TTSRouter`.
    """
    elevenlabs = (
        ElevenLabsStreamingTTS(
            api_key=elevenlabs_api_key,
            voice_id=voice_id,
            model=model,
            sample_rate=sample_rate,
            budget=budget,
        )
        if elevenlabs_api_key
        else None
    )
    system = SystemTTS(sample_rate=sample_rate) if SystemTTS.available() else None
    return TTSRouter(
        provider=provider or "auto",
        elevenlabs=elevenlabs,
        system=system,
        on_fallback=on_fallback,
        sample_rate=sample_rate,
    )

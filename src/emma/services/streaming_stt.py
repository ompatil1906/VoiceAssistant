#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Streaming speech-to-text for the LiveKit agent.

The call path needs transcripts while the caller is still speaking, not a
batch upload after the turn ends. :class:`DeepgramStreamingTranscriber` holds a
WebSocket open for the whole call and forwards 16 kHz PCM16 as it arrives;
:class:`BatchTranscriber` is the fallback used when the stream cannot be
opened or drops mid-call (a slow link, a proxy, an expired key).

Both paths return the same normalized event shape::

    {"type": "partial" | "final" | "utterance_end" | "speech_started",
     "text": str, "start": float, "duration": float, "source": "deepgram"}
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import time
import wave
from dataclasses import dataclass
from typing import Any

import httpx

try:
    from loguru import logger
except ImportError:  # pragma: no cover - loguru is a runtime dependency
    import logging

    logger = logging.getLogger("emma.streaming_stt")

import websockets

DEEPGRAM_WS_URL = "wss://api.deepgram.com/v1/listen"
DEEPGRAM_HTTP_URL = "https://api.deepgram.com/v1/listen"
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"

logging.getLogger("httpx").setLevel(logging.WARNING)

DEFAULT_SAMPLE_RATE = 16_000
# Provider endpointing only has to cut the audio into segments; the turn
# assembler decides when a caller turn is complete. Keeping this short lowers
# the latency of the committed transcript without answering mid-sentence.
DEFAULT_ENDPOINTING_MS = 300
# Silence after which Deepgram declares UtteranceEnd. This is the provider's
# authoritative end-of-turn signal and the assembler's preferred trigger.
DEFAULT_UTTERANCE_END_MS = 1000


@dataclass
class TranscriptEvent:
    """One event produced by a transcriber.

    Parameters:
        type: ``partial``, ``final``, ``utterance_end`` or ``speech_started``.
        text: Transcribed text (empty for non-text events).
        start: Offset of the segment from the start of the stream, in seconds.
        duration: Segment duration in seconds.
        source: Which provider produced it.
        speech_final: Whether the provider marked this segment as the end of a
            spoken utterance. Deepgram emits several ``final`` segments per
            sentence; only ``speech_final`` means the provider's endpointing
            saw silence. Turn assembly treats it as a signal, not a turn.
        is_final: Whether the provider committed this segment (``is_final``).
            ``type`` is ``final`` for these events.
        from_finalize: Whether the segment was produced by an explicit
            ``Finalize`` request rather than the provider's own endpointing.
            Such segments are revisions and must not open a second turn.
        sequence: Monotonic arrival counter within the stream, used to keep
            events ordered when several arrive together.
    """

    type: str
    text: str = ""
    start: float = 0.0
    duration: float = 0.0
    source: str = "deepgram"
    speech_final: bool = False
    is_final: bool = False
    from_finalize: bool = False
    sequence: int = 0


class DeepgramStreamingTranscriber:
    """A long-lived Deepgram WebSocket for one caller track.

    Parameters:
        api_key: Deepgram API key.
        model: Deepgram model, e.g. ``nova-3``.
        sample_rate: Input sample rate in Hz; LiveKit delivers 16 kHz.
        language: BCP-47 language tag.
        budget: Optional session budget; recorded as audio seconds when known.
        endpointing_ms: Provider endpointing silence in milliseconds. Only
            splits audio into segments; turn completeness is decided locally.
        utterance_end_ms: Silence after which the provider emits
            ``UtteranceEnd``, the authoritative end-of-turn signal.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "nova-3",
        *,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        language: str = "en-GB",
        budget: Any | None = None,
        endpointing_ms: int = DEFAULT_ENDPOINTING_MS,
        utterance_end_ms: int = DEFAULT_UTTERANCE_END_MS,
    ) -> None:
        """Store the stream configuration; call :meth:`start` to connect."""
        self._api_key = api_key
        self._model = model
        self._sample_rate = sample_rate
        self._language = language
        self._budget = budget
        self._endpointing_ms = max(10, int(endpointing_ms))
        # Deepgram requires utterance_end_ms to exceed endpointing.
        self._utterance_end_ms = max(
            self._endpointing_ms + 100, int(utterance_end_ms)
        )
        self._ws: Any | None = None
        self._reader: asyncio.Task[None] | None = None
        self._events: asyncio.Queue[TranscriptEvent] = asyncio.Queue(maxsize=64)
        self._closed = False
        self.failed = False
        self.connected = False
        self._connected_at = 0.0
        self._sequence = 0
        self.events_received = 0

    @property
    def healthy(self) -> bool:
        """Whether the stream is open and usable."""
        return self._ws is not None and not self.failed and not self._closed

    def _url(self) -> str:
        params = {
            "model": self._model,
            "language": self._language,
            "encoding": "linear16",
            "sample_rate": str(self._sample_rate),
            "channels": "1",
            "smart_format": "true",
            "interim_results": "true",
            "endpointing": str(self._endpointing_ms),
            "utterance_end_ms": str(self._utterance_end_ms),
            "vad_events": "true",
            # Opt out of model-improvement programmes where the account allows it.
            "mip_opt_out": "true",
        }
        query = "&".join(f"{key}={value}" for key, value in params.items())
        return f"{DEEPGRAM_WS_URL}?{query}"

    async def start(self, timeout: float = 12.0) -> bool:
        """Open the stream.

        Args:
            timeout: Connection budget in seconds.

        Returns:
            True when the socket is ready. A false result means the caller
            should use the batch fallback.
        """
        started = time.perf_counter()
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(
                    self._url(),
                    additional_headers={"Authorization": f"Token {self._api_key}"},
                    open_timeout=timeout,
                    max_size=4 * 1024 * 1024,
                    ping_interval=20,
                    ping_timeout=20,
                ),
                timeout=timeout + 0.5,
            )
        except Exception as exc:
            self.failed = True
            logger.warning(f"[Deepgram] stream could not open: {type(exc).__name__}: {exc}")
            return False

        self.connected = True
        self._connected_at = time.monotonic()
        self._reader = asyncio.create_task(self._read_loop(), name="deepgram-stt-reader")
        logger.info(
            f"[Deepgram] stream open in {time.perf_counter() - started:.2f}s model={self._model}"
        )
        return True

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                event = self._to_event(message)
                if event is None:
                    continue
                try:
                    self._events.put_nowait(event)
                except asyncio.QueueFull:
                    # Drop the oldest event rather than stalling the reader.
                    try:
                        self._events.get_nowait()
                        self._events.put_nowait(event)
                    except (asyncio.QueueEmpty, asyncio.QueueFull):
                        pass
        except asyncio.CancelledError:
            raise
        except websockets.ConnectionClosed as exc:
            if not self._closed:
                self.failed = True
                logger.warning(f"[Deepgram] stream closed unexpectedly: {exc}")
        except Exception as exc:  # pragma: no cover - defensive
            self.failed = True
            logger.warning(f"[Deepgram] stream read failed: {type(exc).__name__}: {exc}")

    def _to_event(self, message: dict[str, Any]) -> TranscriptEvent | None:
        kind = message.get("type")
        if kind == "Results":
            alternatives = (message.get("channel") or {}).get("alternatives") or []
            text = (alternatives[0].get("transcript") if alternatives else "") or ""
            if not text.strip():
                return None
            self._sequence += 1
            self.events_received += 1
            is_final = bool(message.get("is_final"))
            speech_final = bool(message.get("speech_final"))
            return TranscriptEvent(
                type="final" if (is_final or speech_final) else "partial",
                text=text.strip(),
                start=float(message.get("start") or 0.0),
                duration=float(message.get("duration") or 0.0),
                speech_final=speech_final,
                is_final=is_final or speech_final,
                from_finalize=bool(message.get("from_finalize")),
                sequence=self._sequence,
            )
        if kind == "UtteranceEnd":
            self._sequence += 1
            return TranscriptEvent(type="utterance_end", sequence=self._sequence)
        if kind == "SpeechStarted":
            self._sequence += 1
            return TranscriptEvent(type="speech_started", sequence=self._sequence)
        if kind in ("Metadata", "KeepAlive"):
            return None
        return None

    async def send(self, pcm: bytes) -> None:
        """Forward one PCM16 chunk."""
        ws = self._ws
        if ws is None or self.failed or self._closed or not pcm:
            return
        try:
            await ws.send(pcm)
        except Exception as exc:
            self.failed = True
            logger.warning(f"[Deepgram] send failed: {type(exc).__name__}: {exc}")

    async def next_event(self, timeout: float = 0.25) -> TranscriptEvent | None:
        """Wait briefly for the next transcription event."""
        try:
            return await asyncio.wait_for(self._events.get(), timeout=timeout)
        except TimeoutError:
            return None

    async def finalize(self) -> None:
        """Ask Deepgram to flush the current utterance."""
        if self._ws is None or self.failed or self._closed:
            return
        try:
            await self._ws.send(json.dumps({"type": "Finalize"}))
        except Exception:
            self.failed = True

    async def close(self) -> None:
        """Close the stream cleanly."""
        self._closed = True
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
            except Exception:
                pass
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
            self._reader = None


class BatchTranscriber:
    """Fallback transcription when the streaming socket is unavailable.

    Parameters:
        deepgram_api_key: Deepgram key; empty disables the Deepgram path.
        deepgram_model: Deepgram model name.
        gemini_api_key: Gemini key; empty disables the multimodal fallback.
        gemini_model: Gemini model name.
        budget: Optional session budget.
    """

    def __init__(
        self,
        *,
        deepgram_api_key: str = "",
        deepgram_model: str = "nova-3",
        gemini_api_key: str = "",
        gemini_model: str = "gemini-3.8-flash",
        budget: Any | None = None,
    ) -> None:
        """Store provider credentials for the fallback transcription paths."""
        self._deepgram_key = deepgram_api_key
        self._deepgram_model = deepgram_model
        self._gemini_key = gemini_api_key
        self._gemini_model = gemini_model
        self._budget = budget
        self._client: httpx.AsyncClient | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=15.0))
        return self._client

    async def aclose(self) -> None:
        """Close the HTTP connection pool."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def transcribe(self, pcm: bytes, sample_rate: int = DEFAULT_SAMPLE_RATE) -> str:
        """Transcribe a complete utterance.

        Args:
            pcm: Mono PCM16 audio.
            sample_rate: Sample rate of ``pcm``.

        Returns:
            The transcript, or an empty string when no provider produced one.
        """
        if len(pcm) < sample_rate * 2 // 4:  # under 250 ms cannot be speech
            return ""
        if self._budget is not None:
            try:
                self._budget.reserve_request()
                self._budget.record_audio_seconds(len(pcm) / 2 / sample_rate)
            except Exception as exc:
                logger.warning(f"[STT] budget refused the batch request: {exc}")
                return ""

        wav = _to_wav(pcm, sample_rate)
        if self._deepgram_key:
            text = await self._deepgram(wav)
            if text:
                return text
        if self._gemini_key:
            text = await self._gemini(wav)
            if text:
                return text
        return ""

    async def _deepgram(self, wav: bytes) -> str:
        client = await self._http()
        started = time.perf_counter()
        try:
            response = await client.post(
                DEEPGRAM_HTTP_URL,
                params={
                    "model": self._deepgram_model,
                    "language": "en-GB",
                    "smart_format": "true",
                    "mip_opt_out": "true",
                },
                headers={
                    "Authorization": f"Token {self._deepgram_key}",
                    "Content-Type": "audio/wav",
                },
                content=wav,
            )
            response.raise_for_status()
            data = response.json()
            channels = (data.get("results") or {}).get("channels") or []
            alternatives = (channels[0].get("alternatives") if channels else None) or []
            text = (alternatives[0].get("transcript") if alternatives else "") or ""
            logger.info(f"[STT] Deepgram batch {time.perf_counter() - started:.2f}s")
            return text.strip()
        except Exception as exc:
            logger.warning(
                f"[STT] Deepgram batch failed after {time.perf_counter() - started:.2f}s: {exc}"
            )
            return ""

    async def _gemini(self, wav: bytes) -> str:
        client = await self._http()
        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": (
                                "Transcribe the spoken words in this audio exactly. "
                                "Return only the transcript. If there is no speech, return NO_SPEECH."
                            )
                        },
                        {
                            "inlineData": {
                                "mimeType": "audio/wav",
                                "data": base64.b64encode(wav).decode("ascii"),
                            }
                        },
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.0,
                "maxOutputTokens": 150,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        started = time.perf_counter()
        try:
            response = await client.post(
                f"{GEMINI_ENDPOINT}/{self._gemini_model}:generateContent",
                headers={"x-goog-api-key": self._gemini_key},
                json=payload,
            )
            response.raise_for_status()
            candidates = response.json().get("candidates") or []
            parts = (candidates[0].get("content", {}).get("parts") if candidates else None) or []
            text = (parts[0].get("text") if parts else "") or ""
            logger.info(f"[STT] Gemini batch {time.perf_counter() - started:.2f}s")
            if "NO_SPEECH" in text.upper():
                return ""
            return text.strip()
        except Exception as exc:
            logger.warning(f"[STT] Gemini batch failed: {type(exc).__name__}: {exc}")
            return ""


def _to_wav(pcm: bytes, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()

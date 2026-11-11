#!/usr/bin/env python3
#
# Copyright (c) 2026, QuantumLoopAI
# SPDX-License-Identifier: BSD 2-Clause License
#

"""EMMA receptionist agent for a LiveKit room.

The browser joins a room as the caller and publishes a microphone track; this
process joins the same room as ``emma-assistant`` and owns the whole voice loop:

    LiveKit caller audio → Deepgram streaming STT (batch fallback)
        → Gemini with the clinic tools (appointment, prescription, records)
        → ElevenLabs streaming TTS (local ``say`` fallback)
        → LiveKit assistant audio track + JSON control events

Every claim the assistant makes is grounded in a tool result:
:mod:`emma.services.appointment_service` stores the appointment,
:mod:`emma.services.prescription_service` verifies the patient and sends the
email, and :mod:`emma.storage.call_records_excel` writes the call record.

Control messages sent to the browser on the reliable data channel:

    {"event": "state", "state": "listening" | "thinking" | "speaking"}
    {"event": "transcript", "kind": "partial" | "final", "text": "..."}
    {"event": "assistant", "text": "..."}
    {"event": "notice", "text": "..."}

The browser may send ``{"event": "barge-in"}``, ``{"event": "end-call"}`` or a
text turn ``{"event": "user_input", "text": "..."}``.

Run it with::

    python scripts/run_agent_session.py --room emma-123 --caller '{"firstName": "Sam"}'
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

try:
    from dotenv import load_dotenv

    # The repository .env is this agent's configuration. Override inherited
    # values: a stale ELEVENLABS_API_KEY exported in the shell that started the
    # Next server would otherwise win and every synthesis would return 401.
    load_dotenv(REPO_ROOT / ".env", override=True)
except ImportError:  # pragma: no cover - dotenv is a developer convenience
    pass

import numpy as np
from livekit import api, rtc

from emma.config import RuntimeConfig
from emma.cost.budget import SessionBudget
from emma.security.redaction import redact
from emma.services.gemini_client import GeminiClient
from emma.services.streaming_stt import BatchTranscriber, DeepgramStreamingTranscriber
from emma.services.streaming_tts import (
    DEFAULT_SAMPLE_RATE,
    build_tts_router,
)
from emma.services.voice_assistant import (
    AssistantTools,
    CallerProfile,
    build_system_instruction,
    is_duplicate_transcript,
    is_echo,
    normalize_text,
)

SAMPLE_RATE = 24_000
NUM_CHANNELS = 1
SAMPLES_PER_FRAME = 480  # 20 ms at 24 kHz
BYTES_PER_FRAME = SAMPLES_PER_FRAME * 2

INPUT_SAMPLE_RATE = 16_000

# Voice activity detection for the batch fallback and for barge-in. Values are
# RMS over 16-bit samples (0..32767).
SPEECH_RMS = 900
BARGE_IN_RMS = 2600
BARGE_IN_MIN_MS = 220
BARGE_IN_GRACE_MS = 600
SILENCE_END_MS = 700
MIN_UTTERANCE_MS = 350

MAX_LLM_TOOL_ROUNDS = 3
# Tools whose own message is a complete spoken reply. Speaking it directly
# skips a second model round trip and removes any chance of the model
# paraphrasing a confirmation into something the backend did not do.
DIRECT_SPEECH_TOOLS = frozenset(
    {
        "book_appointment",
        "request_prescription",
        "get_clinic_info",
        "lookup_appointment",
    }
)
# Deepgram is configured with endpointing=700 and utterance_end_ms=1400. The
# debounce is only a safety net for a final that never gets an UtteranceEnd.
FINAL_DEBOUNCE_S = 1.2
# When a final stops on a connector ("... tomorrow at"), the provider has cut
# the sentence, not the caller. Wait much longer before answering that fragment.
INCOMPLETE_WAIT_S = 5.0
# New partials after a final mean the caller is still talking; keep extending
# the flush deadline instead of answering the first half of the sentence.
PARTIAL_EXTENDS_FINAL_S = 2.0
# After asking the provider to finalize an utterance, how long to wait for the
# final result before falling back to the last partial. This is deliberately
# generous: partials arrive late on a slow link and cutting a sentence in the
# middle is worse than a slightly slower reply.
PARTIAL_GRACE_AFTER_FINALIZE_S = 2.5

_INCOMPLETE_ENDINGS = frozenset(
    {
        "a",
        "an",
        "and",
        "at",
        "for",
        "from",
        "i",
        "in",
        "is",
        "my",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
    }
)


def log(message: str) -> None:
    """Print a redacted, flushed log line."""
    print(redact(message), flush=True)


def warn(message: str) -> None:
    """Print a redacted warning to stderr."""
    print(redact(f"[Agent] {message}"), file=sys.stderr, flush=True)


@dataclass
class ProviderPlan:
    """The providers this call will use, after policy checks.

    Parameters:
        llm: Language-model provider identifier.
        stt: Speech-to-text provider identifier.
        tts: Text-to-speech provider identifier.
    """

    llm: str = "gemini"
    stt: str = "deepgram"
    tts: str = "elevenlabs"

    def providers(self) -> tuple[str, ...]:
        """Every billable provider in the plan."""
        names = [self.llm, self.stt]
        if self.tts != "system":
            names.append(self.tts)
        return tuple(names)


def resolve_provider_plan(
    config: RuntimeConfig, environ: dict[str, str] | None = None
) -> ProviderPlan:
    """Resolve and authorise the providers named by the environment.

    This is the runtime's cost and data gate: a provider whose cost class has
    not been opted into, or whose data terms conflict with the deployment's
    data mode, stops the call before it starts.

    Args:
        config: Parsed runtime configuration.
        environ: Environment mapping; defaults to ``os.environ``.

    Returns:
        The authorised provider plan.

    Raises:
        RuntimeError: If a provider is refused, with the variables to set.
    """
    env = os.environ if environ is None else environ
    tts_choice = (env.get("EMMA_TTS_PROVIDER") or env.get("TTS_PROVIDER") or "elevenlabs").lower()
    if tts_choice in ("local", "system", "say", "macos"):
        tts_choice = "system"
    plan = ProviderPlan(
        llm=(env.get("EMMA_LLM_PROVIDER") or "gemini").lower(),
        stt=(env.get("EMMA_STT_PROVIDER") or "deepgram").lower(),
        tts=tts_choice,
    )

    policy = config.cost_policy()
    data_policy = config.data_policy()
    for provider in plan.providers():
        try:
            policy.assert_allowed(provider)
            data_policy.assert_allowed(provider)
        except Exception as exc:
            raise RuntimeError(
                f"provider '{provider}' is refused by the runtime policy: {exc}. "
                "To run this provider set EMMA_ALLOW_PAID=true and/or "
                "EMMA_ALLOW_TRIAL=true for its cost class, and EMMA_DATA_MODE=approved "
                "once a human has approved its data terms."
            ) from exc
    return plan


@dataclass
class TurnMetrics:
    """Latency telemetry for one caller turn."""

    stt_final_ms: float = 0.0
    llm_ms: float = 0.0
    tts_first_audio_ms: float = 0.0
    turn_total_ms: float = 0.0
    tool_calls: list[str] = field(default_factory=list)


class EmmaAgent:
    """One receptionist session in a LiveKit room."""

    def __init__(
        self, room_name: str, caller: dict[str, Any], *, plan: ProviderPlan | None = None
    ) -> None:
        self.room_name = room_name
        self.caller = CallerProfile.from_mapping(caller)
        self.plan = plan or resolve_provider_plan(RuntimeConfig.from_env())
        self.runtime_config = RuntimeConfig.from_env()
        self.budget = SessionBudget(self.runtime_config.limits)

        self.tools = AssistantTools(caller=self.caller, budget=self.budget)
        self.system_instruction = build_system_instruction(self.caller, self.tools.schedule)

        self.llm = GeminiClient(
            api_key=os.getenv("GEMINI_API_KEY", ""),
            model=os.getenv("EMMA_GEMINI_MODEL", "gemini-3.8-flash"),
            budget=self.budget,
        )
        self.stt = DeepgramStreamingTranscriber(
            api_key=os.getenv("DEEPGRAM_API_KEY", ""),
            model=os.getenv("DEEPGRAM_MODEL", "nova-3"),
            sample_rate=INPUT_SAMPLE_RATE,
            language=os.getenv("EMMA_STT_LANGUAGE", "en-GB"),
            budget=self.budget,
        )
        self.batch_stt = BatchTranscriber(
            deepgram_api_key=os.getenv("DEEPGRAM_API_KEY", ""),
            deepgram_model=os.getenv("DEEPGRAM_MODEL", "nova-3"),
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
            gemini_model=os.getenv("EMMA_GEMINI_MODEL", "gemini-3.8-flash"),
            budget=self.budget,
        )
        self.tts = build_tts_router(
            provider=self.plan.tts,
            elevenlabs_api_key=os.getenv("ELEVENLABS_API_KEY", ""),
            voice_id=os.getenv("ELEVENLABS_VOICE_ID", "CwhRBWXzGAHq8TQ4Fs17"),
            model=os.getenv("ELEVENLABS_MODEL", "eleven_flash_v2_5"),
            sample_rate=DEFAULT_SAMPLE_RATE,
            budget=self.budget,
            on_fallback=self._on_tts_fallback,
        )

        log(
            "[Agent] credentials "
            f"gemini={_fingerprint(os.getenv('GEMINI_API_KEY', ''))} "
            f"deepgram={_fingerprint(os.getenv('DEEPGRAM_API_KEY', ''))} "
            f"elevenlabs={_fingerprint(os.getenv('ELEVENLABS_API_KEY', ''))} "
            f"voice={os.getenv('ELEVENLABS_VOICE_ID', '')}"
        )

        self.room: rtc.Room | None = None
        self.audio_source: rtc.AudioSource | None = None
        self.audio_track: rtc.LocalAudioTrack | None = None

        self.contents: list[dict[str, Any]] = []
        self.recent_assistant_text = ""
        self.recent_user_texts: list[str] = []
        self.last_transcript_partial = ""

        self.speaking = False
        self.thinking = False
        self.interrupted = False
        # Incremented on every barge-in. A turn that was already waiting on the
        # model checks it before speaking, so an interrupted answer is dropped
        # instead of talking over the caller's new request.
        self._generation = 0
        self.greeting_done = False
        self.greeting_started_at = 0.0
        self.speech_started_at = 0.0
        self.caller_turns = 0
        self.stop_event = asyncio.Event()

        self._turn_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=8)
        self._turn_worker: asyncio.Task[None] | None = None
        self._stt_pump: asyncio.Task[None] | None = None
        self._greeting_task: asyncio.Task[None] | None = None
        self._pending_final: list[str] = []
        self._pending_final_due = 0.0
        # Last partial and when it last changed. Deepgram can keep emitting
        # partial results without ever marking a speech_final (continuous room
        # noise or echo above its VAD floor); a partial that stops changing is
        # then the only available end-of-turn signal.
        self._partial_text = ""
        self._partial_at = 0.0
        self._stt_partials = 0
        self._stt_finals = 0
        # Set by the audio loop after the caller has been silent for a moment;
        # the pump then asks Deepgram to finalize the current utterance even if
        # the provider's own endpointing never fired.
        self._force_finalize = False
        self._finalize_requested_at = 0.0
        # Half-duplex: caller audio is only forwarded to the transcriber while
        # EMMA is silent. The room echo of her own voice can never become a
        # caller turn, which is what made the assistant answer itself.
        self._stt_resume_at = 0.0
        # Updated by the event pump; the audio loop uses it to notice a stream
        # that is open but silent and transcribe the utterance in batch instead.
        self._last_stt_event_at = 0.0
        self._barge_ms = 0.0
        self._stt_failed_reported = False
        self._audio_task: asyncio.Task[None] | None = None

    # -- control channel --------------------------------------------------

    async def send_control(self, payload: dict[str, Any]) -> None:
        """Publish one reliable JSON control message to the room."""
        if self.room is None or not self.room.isconnected:
            return
        try:
            await self.room.local_participant.publish_data(
                json.dumps(payload).encode("utf-8"), reliable=True
            )
        except Exception as exc:  # the UI is optional; audio is the call
            warn(f"control message failed: {type(exc).__name__}: {exc}")

    async def _set_state(self, state: str) -> None:
        await self.send_control({"event": "state", "state": state})

    async def _notice(self, text: str) -> None:
        await self.send_control({"event": "notice", "text": text})

    def _on_tts_fallback(self, reason: str) -> None:
        if self.room is not None and self.room.isconnected:
            asyncio.create_task(self._notice(reason))

    # -- audio output -----------------------------------------------------

    async def _publish_pcm(self, pcm: bytes) -> None:
        """Publish PCM16 at 24 kHz in 20 ms LiveKit frames."""
        source = self.audio_source
        if source is None or not pcm:
            return
        remaining = pcm
        while remaining:
            if self.interrupted or self.stop_event.is_set():
                source.clear_queue()
                return
            chunk = remaining[:BYTES_PER_FRAME]
            remaining = remaining[BYTES_PER_FRAME:]
            if len(chunk) < BYTES_PER_FRAME:
                chunk = chunk.ljust(BYTES_PER_FRAME, b"\x00")
            frame = rtc.AudioFrame(
                data=chunk,
                sample_rate=SAMPLE_RATE,
                num_channels=NUM_CHANNELS,
                samples_per_channel=SAMPLES_PER_FRAME,
            )
            try:
                await source.capture_frame(frame)
            except Exception as exc:  # room closing
                warn(f"publishing audio failed: {type(exc).__name__}: {exc}")
                return

    async def speak(self, text: str, *, purpose: str = "assistant") -> None:
        """Speak ``text`` sentence by sentence, honouring barge-in."""
        clean = (text or "").strip()
        if not clean or self.stop_event.is_set():
            return

        self.speaking = True
        self.interrupted = False
        self.speech_started_at = time.monotonic()
        self._barge_ms = 0.0
        self.recent_assistant_text = clean

        await self._set_state("speaking")
        if purpose != "filler":
            await self.send_control({"event": "assistant", "text": clean})

        started = time.monotonic()
        first_audio_ms = 0.0
        try:
            async for chunk in self.tts.stream(clean):
                if self.interrupted or self.stop_event.is_set():
                    break
                if not first_audio_ms:
                    first_audio_ms = (time.monotonic() - started) * 1000.0
                    log(f"[TTS] first audio to room in {first_audio_ms:.0f}ms")
                await self._publish_pcm(chunk)
        finally:
            self.speaking = False
            # Let the room's echo tail decay before the transcriber listens.
            # After a barge-in the caller is already speaking, so resume at once.
            if not self.interrupted:
                self._stt_resume_at = time.monotonic() + 0.25
            if not self.interrupted and not self.stop_event.is_set():
                await self._set_state("listening")

    def interrupt(self, reason: str = "caller") -> None:
        """Stop the assistant's current utterance immediately."""
        if not self.speaking and not self.thinking:
            return
        if self.speaking:
            log(f"[Agent] barge-in ({reason}) — stopping output")
        self.interrupted = True
        self.speaking = False
        self._generation += 1
        # A barge-in is caller speech: start forwarding it immediately.
        self._stt_resume_at = 0.0
        if self.audio_source is not None:
            self.audio_source.clear_queue()

    # -- conversation -----------------------------------------------------

    async def ensure_greeting(self) -> None:
        """Greet the caller once, whichever event delivered them."""
        if self._greeting_task is not None:
            return
        self._greeting_task = asyncio.create_task(self.greet(), name="greeting")

    async def greet(self) -> None:
        """Greet the caller once, then start accepting turns."""
        name = self.caller.first_name or "there"
        greeting = (
            f"Hello {name}, you've reached {self.tools.schedule.name}. "
            "I'm EMMA, the receptionist. How can I help you today?"
        )
        log(f"[Agent] greeting: {greeting}")
        self.greeting_started_at = time.monotonic()
        # Let the browser attach the remote audio element before the first
        # frames arrive, otherwise the greeting loses its first word.
        await asyncio.sleep(0.4)
        # Warm the LLM connection while the greeting plays: the first caller
        # turn then skips the TLS handshake that dominates first-token latency.
        warmup = asyncio.create_task(self.llm.warmup(), name="gemini-warmup")
        await self.speak(greeting, purpose="greeting")
        if not self.greeting_done:
            self.greeting_done = True
        if not warmup.done():
            try:
                await asyncio.wait_for(warmup, timeout=15.0)
            except TimeoutError:
                pass

    async def enqueue_turn(self, text: str, source: str) -> None:
        """Validate and queue one caller turn."""
        clean = " ".join(text.split())
        if not clean:
            return

        if self.speaking:
            if is_echo(clean, self.recent_assistant_text):
                log(f"[Agent] ignoring echo of assistant speech ({source})")
                return
            if (time.monotonic() - self.speech_started_at) * 1000.0 < BARGE_IN_GRACE_MS:
                log(f"[Agent] ignoring {source} transcript during speech grace period")
                return
            self.interrupt("new transcript")

        if is_echo(clean, self.recent_assistant_text):
            log(f"[Agent] ignoring echo transcript ({source})")
            return
        if is_duplicate_transcript(clean, self.recent_user_texts[-4:], threshold=0.8):
            log(f"[Agent] ignoring duplicate transcript from {source}")
            return

        self.recent_user_texts.append(clean)
        if len(self.recent_user_texts) > 8:
            self.recent_user_texts.pop(0)
        try:
            self._turn_queue.put_nowait((clean, source))
        except asyncio.QueueFull:
            warn("turn queue full; dropping the oldest caller turn")
            try:
                self._turn_queue.get_nowait()
                self._turn_queue.put_nowait((clean, source))
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    async def _turn_worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                text, source = await asyncio.wait_for(self._turn_queue.get(), timeout=0.5)
            except TimeoutError:
                continue
            try:
                await self._run_turn(text, source)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never let one turn kill the call
                warn(f"turn failed: {type(exc).__name__}: {exc}")
                await self.speak(
                    "I'm sorry, something went wrong on my side. Could you try that again?",
                )

    async def _run_turn(self, text: str, source: str) -> None:
        started = time.monotonic()
        generation = self._generation
        self.caller_turns += 1
        log(f"[Agent] caller ({source}): {text}")
        await self.send_control({"event": "transcript", "kind": "final", "text": text})

        self.thinking = True
        await self._set_state("thinking")
        self.contents.append({"role": "user", "parts": [{"text": text}]})
        self._trim_contents()

        reply_text = ""
        tool_names: list[str] = []
        try:
            for _round in range(MAX_LLM_TOOL_ROUNDS):
                reply = await self.llm.generate(
                    system_instruction=self.system_instruction,
                    contents=self.contents,
                    tools=self.tools.gemini_tools(),
                )
                if reply.error and not reply.text:
                    reply_text = (
                        "I'm having trouble reaching the practice system. Could you say that again?"
                    )
                    break

                if not reply.function_calls:
                    reply_text = reply.text
                    if reply_text:
                        self.contents.append({"role": "model", "parts": [{"text": reply_text}]})
                    break

                responses: list[dict[str, Any]] = []
                direct: list[str] = []
                for call in reply.function_calls:
                    tool_names.append(call.name)
                    log(f"[Tools] {call.name} {redact(json.dumps(call.arguments))}")
                    result, spoken = self.tools.execute(call.name, call.arguments)
                    log(
                        f"[Tools] {call.name} -> success={result.get('success')} "
                        f"{redact(str(result.get('error', '')))}"
                    )
                    responses.append(
                        {
                            "functionResponse": {
                                "name": call.name,
                                "response": _jsonable(result),
                            }
                        }
                    )
                    if call.name in DIRECT_SPEECH_TOOLS and spoken:
                        direct.append(spoken)
                    elif spoken:
                        reply_text = spoken
                self.contents.append({"role": "user", "parts": responses})

                if direct:
                    # The tool's message is already a grounded, spoken
                    # confirmation. Speaking it directly removes a whole model
                    # round trip from the turn (seconds on a slow link) and
                    # guarantees the caller hears what the backend actually did.
                    reply_text = " ".join(direct)
                    self.contents.append({"role": "model", "parts": [{"text": reply_text}]})
                    break
            else:
                reply_text = reply_text or "Let me get the reception team to help with that."

            if not reply_text:
                reply_text = "I'm sorry, I didn't catch that. Could you say it again?"
        finally:
            self.thinking = False

        metrics = TurnMetrics(
            llm_ms=(time.monotonic() - started) * 1000.0,
            tool_calls=tool_names,
        )
        log(
            f"[Latency] LLM {metrics.llm_ms:.0f}ms tools={tool_names or 'none'} "
            f"budget={self.budget.usage}"
        )
        if generation != self._generation:
            log("[Agent] turn superseded by a barge-in; not speaking the stale reply")
            return
        await self.speak(reply_text, purpose="assistant")

        metrics.turn_total_ms = (time.monotonic() - started) * 1000.0
        self._last_metrics = metrics

    def _trim_contents(self) -> None:
        """Keep the prompt bounded: the system instruction carries the facts."""
        if len(self.contents) > 24:
            del self.contents[: len(self.contents) - 24]

    # -- caller audio -----------------------------------------------------

    def handle_data_message(self, payload: bytes) -> None:
        """Dispatch a JSON message from the browser."""
        try:
            message = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        event = message.get("event")
        if event == "barge-in":
            self.interrupt("browser barge-in")
        elif event == "end-call":
            log("[Agent] caller ended the call")
            self.stop_event.set()
        elif event in ("user_input", "text") and isinstance(message.get("text"), str):
            asyncio.create_task(
                self.enqueue_turn(message["text"], "browser"),
                name="browser-turn",
            )

    async def listen_to_track(self, track: rtc.Track) -> None:
        """Consume the caller's microphone track for the rest of the call."""
        if self._audio_task is not None and not self._audio_task.done():
            log("[Agent] caller audio already being consumed; ignoring duplicate track")
            return
        self._audio_task = asyncio.current_task()
        # A muted microphone can unsubscribe and republish the track. Reuse the
        # open STT socket (and its single event pump) rather than starting a
        # second reader on the same connection.
        log("[Agent] subscribed to caller audio")
        stream = rtc.AudioStream(
            track,
            sample_rate=INPUT_SAMPLE_RATE,
            num_channels=1,
            frame_size_ms=20,
        )

        # Open the socket in the background: the handshake takes up to several
        # seconds on a slow link, and the caller may already be speaking. Audio
        # that arrives before the socket is ready is buffered and flushed.
        start_task = asyncio.create_task(self.stt.start(), name="stt-connect")
        if self._stt_pump is None or self._stt_pump.done():
            self._stt_pump = asyncio.create_task(self._pump_stt_events(), name="stt-pump")

        prebuffer = bytearray()
        PREBUFFER_MAX = INPUT_SAMPLE_RATE * 2 * 2  # two seconds of PCM16
        batch_buffer = bytearray()
        utterance_ms = 0.0
        silence_ms = 0.0
        in_speech = False
        peak_rms = 0.0
        last_diag = time.monotonic()
        caller_active = False
        voice_ms = 0.0
        last_voice_at = 0.0
        # Watchdog: if the stream is open but returns nothing for this
        # utterance, batch-transcribe it so the caller is still heard.
        watchdog = bytearray()
        watchdog_voice_ms = 0.0
        watchdog_silence_ms = 0.0
        watchdog_in_speech = False

        try:
            async for event in stream:
                if self.stop_event.is_set():
                    break
                pcm = event.frame.data.tobytes()
                if not pcm:
                    continue
                rms = frame_rms(pcm)
                duration_ms = len(pcm) / 2 / INPUT_SAMPLE_RATE * 1000.0
                peak_rms = max(peak_rms, rms)

                # One diagnostic line every five seconds: distinguishes "the
                # microphone is silent" from "Deepgram is receiving audio but
                # returning nothing" when a caller reports silence.
                if time.monotonic() - last_diag >= 5.0:
                    log(
                        f"[STT] audio rms={rms:.0f} peak={peak_rms:.0f} "
                        f"partials={self._stt_partials} finals={self._stt_finals} "
                        f"stream={'open' if self.stt.healthy else 'down'}"
                    )
                    peak_rms = 0.0
                    last_diag = time.monotonic()

                # Barge-in: sustained caller energy while EMMA is speaking.
                if self.speaking:
                    since_speech = (time.monotonic() - self.speech_started_at) * 1000.0
                    if since_speech > BARGE_IN_GRACE_MS and rms > BARGE_IN_RMS:
                        self._barge_ms += duration_ms
                        if self._barge_ms >= BARGE_IN_MIN_MS:
                            self._barge_ms = 0.0
                            self.interrupt("caller energy")
                    elif rms < SPEECH_RMS:
                        self._barge_ms = max(0.0, self._barge_ms - duration_ms)
                else:
                    self._barge_ms = 0.0

                listening = not self.speaking and time.monotonic() >= self._stt_resume_at
                if listening and self.stt.healthy:
                    if prebuffer:
                        log(f"[STT] flushing {len(prebuffer)} buffered bytes to the stream")
                        await self.stt.send(bytes(prebuffer))
                        prebuffer.clear()
                    await self.stt.send(pcm)
                elif listening and not self.stt.failed:
                    prebuffer.extend(pcm)
                    if len(prebuffer) > PREBUFFER_MAX:
                        del prebuffer[: len(prebuffer) - PREBUFFER_MAX]

                # Energy-based end of speech. Deepgram's endpointing can fail
                # to fire on a noisy browser microphone, leaving the stream
                # with partials but no final; silence is the independent signal.
                now = time.monotonic()
                if not listening:
                    voice_ms = 0.0
                    caller_active = False
                elif rms >= SPEECH_RMS:
                    voice_ms += duration_ms
                    last_voice_at = now
                    if voice_ms >= 120.0:
                        caller_active = True
                elif caller_active and now - last_voice_at >= 1.3:
                    caller_active = False
                    voice_ms = 0.0
                    self._force_finalize = True
                if rms < SPEECH_RMS and not caller_active:
                    voice_ms = 0.0

                # Streaming is open: keep an energy-gated copy of the caller's
                # speech. If no STT event arrives for it, batch transcription
                # is the safety net.
                if listening and self.stt.healthy:
                    if rms >= SPEECH_RMS:
                        watchdog_voice_ms += duration_ms
                        watchdog_silence_ms = 0.0
                        if watchdog_voice_ms >= 120.0:
                            watchdog_in_speech = True
                        if watchdog_in_speech:
                            watchdog.extend(pcm)
                    elif watchdog_in_speech:
                        watchdog.extend(pcm)
                        watchdog_silence_ms += duration_ms
                        if watchdog_silence_ms >= 1000.0:
                            utterance = bytes(watchdog)
                            voice_seconds = watchdog_voice_ms / 1000.0
                            watchdog.clear()
                            watchdog_in_speech = False
                            watchdog_voice_ms = 0.0
                            watchdog_silence_ms = 0.0
                            stream_silent_for = time.monotonic() - self._last_stt_event_at
                            if voice_seconds >= 0.8 and stream_silent_for >= 4.0:
                                log(
                                    "[STT] stream returned no transcript for this "
                                    "utterance; using the batch fallback"
                                )
                                asyncio.create_task(
                                    self._transcribe_and_enqueue(utterance),
                                    name="batch-watchdog",
                                )
                    if len(watchdog) > INPUT_SAMPLE_RATE * 2 * 15:
                        watchdog.clear()
                        watchdog_in_speech = False
                        watchdog_voice_ms = 0.0
                        watchdog_silence_ms = 0.0

                if self.stt.failed and listening:
                    if rms >= SPEECH_RMS:
                        utterance_ms += duration_ms
                        silence_ms = 0.0
                        if not in_speech and utterance_ms >= 60:
                            in_speech = True
                            if self.speaking:
                                self.interrupt("caller speech")
                        if in_speech:
                            batch_buffer.extend(pcm)
                    elif in_speech:
                        batch_buffer.extend(pcm)
                        silence_ms += duration_ms
                        if silence_ms >= SILENCE_END_MS:
                            in_speech = False
                            silence_ms = 0.0
                            if utterance_ms >= MIN_UTTERANCE_MS:
                                audio = bytes(batch_buffer)
                                batch_buffer.clear()
                                utterance_ms = 0.0
                                asyncio.create_task(
                                    self._transcribe_and_enqueue(audio),
                                    name="batch-stt",
                                )
                            else:
                                batch_buffer.clear()
                                utterance_ms = 0.0
                    else:
                        utterance_ms = 0.0
                        if len(batch_buffer) > INPUT_SAMPLE_RATE * 2 * 15:
                            batch_buffer.clear()

                if self.stt.failed and not self._stt_failed_reported:
                    self._stt_failed_reported = True
                    warn("streaming STT unavailable; using batch transcription per utterance")
                    await self._notice(
                        "Live transcription is degraded; EMMA will answer after each turn."
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            warn(f"caller audio loop ended: {type(exc).__name__}: {exc}")
        finally:
            if not start_task.done():
                start_task.cancel()
            try:
                await start_task
            except (asyncio.CancelledError, Exception):
                pass
            await stream.aclose()
            if self._audio_task is asyncio.current_task():
                self._audio_task = None

    async def _transcribe_and_enqueue(self, pcm: bytes) -> None:
        started = time.monotonic()
        text = await self.batch_stt.transcribe(pcm, INPUT_SAMPLE_RATE)
        if text:
            log(f"[STT] batch transcript in {(time.monotonic() - started) * 1000:.0f}ms")
            await self.send_control({"event": "transcript", "kind": "final", "text": text})
            await self.enqueue_turn(text, "deepgram-batch")

    async def _pump_stt_events(self) -> None:
        """Turn Deepgram events into transcript updates and caller turns.

        Deadline checks run at the top of every iteration, not only when the
        event queue is idle: a stream that keeps emitting partial results would
        otherwise never reach the idle branch and a pending final would be
        stranded — the caller speaks and EMMA simply never answers.
        """
        while not self.stop_event.is_set():
            now = time.monotonic()

            # A final that was not marked speech_final: flush it on a timer.
            if self._pending_final and now >= self._pending_final_due:
                await self._flush_pending_final()

            # The caller stopped speaking but no final arrived: ask the provider
            # to flush the utterance, then let the normal final path run.
            if self._force_finalize and self.stt.healthy:
                self._force_finalize = False
                await self.stt.finalize()
                self._finalize_requested_at = time.monotonic()

            # Last resort for a provider that never returns a final: once the
            # grace period after Finalize has passed, use the last partial. Only
            # attempted after a caller silence, never while words are arriving.
            if (
                self._finalize_requested_at
                and not self._pending_final
                and time.monotonic() - self._finalize_requested_at >= PARTIAL_GRACE_AFTER_FINALIZE_S
            ):
                self._finalize_requested_at = 0.0
                text = self._partial_text.strip()
                self._partial_text = ""
                self._partial_at = 0.0
                self.last_transcript_partial = ""
                if len(text.split()) >= 2:
                    log("[STT] no final result; using the last partial as the caller turn")
                    await self.enqueue_turn(text, "deepgram-partial")

            try:
                event = await self.stt.next_event(timeout=0.1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep the pump alive no matter what
                warn(f"STT event pump error: {type(exc).__name__}: {exc}")
                await asyncio.sleep(0.2)
                continue

            self._last_stt_event_at = time.monotonic()
            if event is None:
                continue

            if event.type == "partial":
                if event.text and event.text != self.last_transcript_partial:
                    self.last_transcript_partial = event.text
                    self._partial_text = event.text
                    self._partial_at = time.monotonic()
                    self._stt_partials += 1
                    if self._pending_final:
                        # The caller is still speaking after a final result.
                        # Do not answer the fragment; wait for the next final.
                        self._pending_final_due = time.monotonic() + PARTIAL_EXTENDS_FINAL_S
                    await self.send_control(
                        {"event": "transcript", "kind": "partial", "text": event.text}
                    )
            elif event.type == "final":
                self.last_transcript_partial = ""
                self._partial_text = ""
                self._partial_at = 0.0
                self._finalize_requested_at = 0.0
                if event.text:
                    self._stt_finals += 1
                    self._pending_final.append(event.text)
                    joined = " ".join(self._pending_final)
                    if _ends_utterance(joined):
                        await self._flush_pending_final()
                    else:
                        # Cut mid-phrase: give the caller time to continue.
                        self._pending_final_due = time.monotonic() + INCOMPLETE_WAIT_S
            elif event.type == "utterance_end":
                self._finalize_requested_at = 0.0
                await self._flush_pending_final()

    async def _flush_pending_final(self) -> None:
        if not self._pending_final and not self._partial_text:
            return
        parts = [part for part in self._pending_final if part]
        # A final may be followed by an un-finalized continuation. Include the
        # last partial when it adds words the pending text does not have.
        partial = self._partial_text.strip()
        if partial:
            joined = " ".join(parts).strip()
            if normalize_text(partial) not in normalize_text(joined):
                parts.append(partial)
        text = " ".join(parts).strip()
        self._pending_final.clear()
        self._partial_text = ""
        self._partial_at = 0.0
        self.last_transcript_partial = ""
        self._pending_final_due = 0.0
        if text:
            await self.enqueue_turn(text, "deepgram-stream")

    # -- lifecycle --------------------------------------------------------

    def _agent_token(self, livekit_url: str) -> str:
        del livekit_url
        return (
            api.AccessToken(
                os.getenv("LIVEKIT_API_KEY", "emma_local"),
                os.getenv("LIVEKIT_API_SECRET", ""),
            )
            .with_identity("emma-assistant")
            .with_name("EMMA Receptionist")
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=self.room_name,
                    can_publish=True,
                    can_subscribe=True,
                    can_publish_data=True,
                )
            )
            .to_jwt()
        )

    def _wire_room(self) -> None:
        assert self.room is not None

        @self.room.on("data_received")
        def _on_data(packet: rtc.DataPacket) -> None:
            self.handle_data_message(packet.data)

        @self.room.on("participant_connected")
        def _on_join(participant: rtc.RemoteParticipant) -> None:
            log(f"[Agent] participant joined: {participant.identity}")
            if participant.identity.startswith("caller"):
                asyncio.create_task(self.ensure_greeting(), name="greeting")

        @self.room.on("track_subscribed")
        def _on_track(
            track: rtc.Track,
            publication: rtc.RemoteTrackPublication,
            participant: rtc.RemoteParticipant,
        ) -> None:
            del publication
            if track.kind == rtc.TrackKind.KIND_AUDIO and participant.identity != "emma-assistant":
                asyncio.create_task(self.listen_to_track(track), name="caller-audio")

        @self.room.on("participant_disconnected")
        def _on_leave(participant: rtc.RemoteParticipant) -> None:
            log(f"[Agent] participant left: {participant.identity}")
            if participant.identity != "emma-assistant":
                asyncio.create_task(self._end_after_departure(), name="end-call")

        @self.room.on("disconnected")
        def _on_disconnect(reason: Any = None) -> None:
            log(f"[Agent] room disconnected: {reason}")
            self.stop_event.set()

    async def _end_after_departure(self) -> None:
        await asyncio.sleep(1.5)
        if self.room is not None and len(self.room.remote_participants) == 0:
            self.stop_event.set()

    async def run(self) -> None:
        """Connect, run the call, and clean up exactly once."""
        livekit_url = os.getenv("LIVEKIT_URL", "ws://127.0.0.1:7880")
        token = self._agent_token(livekit_url)

        self.audio_source = rtc.AudioSource(
            sample_rate=SAMPLE_RATE, num_channels=NUM_CHANNELS, queue_size_ms=200
        )
        self.audio_track = rtc.LocalAudioTrack.create_audio_track("emma-audio", self.audio_source)
        self.room = rtc.Room()
        self._wire_room()

        log(
            f"[Agent] connecting to {livekit_url} room={self.room_name} "
            f"providers={self.plan.providers()} caller={self.caller.full_name or 'caller'}"
        )
        await self.room.connect(livekit_url, token)
        await self.room.local_participant.publish_track(self.audio_track)
        log("[Agent] connected and publishing 'emma-audio'")

        self._turn_worker = asyncio.create_task(self._turn_worker_loop(), name="turn-worker")

        # The caller may already be in the room — the token endpoint spawns this
        # process before the browser finishes connecting.
        if any(
            participant.identity.startswith("caller")
            for participant in self.room.remote_participants.values()
        ):
            asyncio.create_task(self.ensure_greeting(), name="greeting")

        watchdog = asyncio.create_task(self._watchdog(), name="watchdog")
        try:
            await self.stop_event.wait()
        finally:
            watchdog.cancel()
            await self._shutdown()

    async def _watchdog(self) -> None:
        """End a session that has no caller: never leave an agent running idle."""
        waited = 0.0
        while not self.stop_event.is_set():
            await asyncio.sleep(1.0)
            waited += 1.0
            has_caller = (
                any(
                    participant.identity.startswith("caller")
                    for participant in self.room.remote_participants.values()
                )
                if self.room
                else False
            )
            if has_caller:
                waited = 0.0
                continue
            if waited >= 120.0:
                log("[Agent] no caller in the room for 120s; exiting")
                self.stop_event.set()
                return

    async def _shutdown(self) -> None:
        """Stop every task and close every provider exactly once."""
        self.stop_event.set()
        for task in (self._stt_pump, self._turn_worker):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        try:
            await self.stt.close()
        except Exception:
            pass
        try:
            await self.batch_stt.aclose()
        except Exception:
            pass
        try:
            await self.tts.aclose()
        except Exception:
            pass
        try:
            await self.llm.aclose()
        except Exception:
            pass

        if self.caller_turns > 0:
            summary = self._closing_summary()
            try:
                self.tools.record_fallback_summary(summary)
            except Exception as exc:
                warn(f"could not write the call record: {type(exc).__name__}: {exc}")

        log(f"[Agent] closing call after {self.caller_turns} caller turns")
        if self.room is not None:
            try:
                await self.room.disconnect()
            except Exception:
                pass

    def _closing_summary(self) -> str:
        if self.caller_turns == 0:
            return "Call connected; the caller did not speak before the line closed."
        last = self.recent_user_texts[-1] if self.recent_user_texts else ""
        return (
            f"Call ended after {self.caller_turns} caller turn(s). "
            f"Last caller request: {last or 'not transcribed'}."
        )


def _ends_utterance(text: str) -> bool:
    """Whether a transcript looks finished rather than cut mid-phrase."""
    words = text.lower().replace(",", " ").split()
    if not words:
        return False
    if text.rstrip().endswith((".", "?", "!")):
        return True
    return words[-1] not in _INCOMPLETE_ENDINGS


def frame_rms(pcm: bytes) -> float:
    """Root-mean-square level of PCM16 samples."""
    samples = np.frombuffer(pcm, dtype=np.int16)
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))


def split_sentences(text: str, *, max_chars: int = 220) -> list[str]:
    """Split a reply into speakable sentences.

    Short fragments are merged so the voice does not pause between them, and a
    very long sentence is cut at a comma or a word boundary.

    Args:
        text: Assistant reply.
        max_chars: Longest chunk passed to the synthesiser.

    Returns:
        One or more non-empty sentence strings.
    """
    normalized = " ".join(text.split())
    if not normalized:
        return []
    raw = re.split(r"(?<=[.!?])\s+", normalized)
    chunks: list[str] = []
    current = ""
    for part in raw:
        candidate = f"{current} {part}".strip() if current else part
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(part) > max_chars:
            cut = part.rfind(",", 0, max_chars)
            if cut < 40:
                cut = part.rfind(" ", 0, max_chars)
            if cut < 40:
                cut = max_chars
            chunks.append(part[:cut].strip())
            part = part[cut:].lstrip(", ")
        current = part
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk.strip()]


def _fingerprint(value: str) -> str:
    """Short, non-reversible identifier for a credential, for diagnostics."""
    if not value:
        return "missing"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _jsonable(value: Any) -> Any:
    """Make a tool result safe for a JSON API payload."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return json.loads(json.dumps(value, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an EMMA voice agent session")
    parser.add_argument("--room", required=True, help="LiveKit room name")
    parser.add_argument("--caller", default="{}", help="JSON encoded caller intake form")
    args = parser.parse_args()

    try:
        caller = json.loads(args.caller)
    except ValueError:
        caller = {}
    if not isinstance(caller, dict):
        caller = {}

    try:
        plan = resolve_provider_plan(RuntimeConfig.from_env())
    except RuntimeError as exc:
        warn(str(exc))
        return 2

    try:
        agent = EmmaAgent(args.room, caller, plan=plan)
    except Exception as exc:
        warn(f"could not start the session: {type(exc).__name__}: {exc}")
        return 1

    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        warn(f"session ended with an error: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

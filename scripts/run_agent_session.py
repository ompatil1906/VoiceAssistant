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
from emma.services.gemini_client import FunctionCall, GeminiClient
from emma.services.response_stream import SpeechSink
from emma.services.streaming_stt import BatchTranscriber, DeepgramStreamingTranscriber
from emma.services.streaming_tts import (
    DEFAULT_SAMPLE_RATE,
    build_tts_router,
)
from emma.services.turn_manager import (
    CallerTurn,
    TurnLedger,
    UtteranceAssembler,
    assembler_config_from_env,
)
from emma.services.voice_assistant import (
    AssistantTools,
    CallerProfile,
    build_system_instruction,
    is_echo,
)

SAMPLE_RATE = 24_000
NUM_CHANNELS = 1
SAMPLES_PER_FRAME = 480  # 20 ms at 24 kHz
BYTES_PER_FRAME = SAMPLES_PER_FRAME * 2

INPUT_SAMPLE_RATE = 16_000

# Voice activity detection for the batch fallback and for barge-in. Values are
# RMS over 16-bit samples (0..32767).
SPEECH_RMS = 900
# Barge-in must not depend on a fixed loudness: a quiet caller on a quiet link
# would never exceed it, and a noisy room would trigger it on its own. The
# threshold tracks the room's noise floor between turns and is clamped.
BARGE_IN_MIN_RMS = 2200
BARGE_IN_NOISE_FACTOR = 3.0
BARGE_IN_MAX_RMS = 9000
BARGE_IN_MIN_MS = 160
BARGE_IN_GRACE_MS = 250
# Cancelling a response that is still generating needs more evidence than
# stopping playback: a short "mm-hm" must not discard the answer to the
# question the caller just asked.
BARGE_IN_THINKING_MIN_MS = 400
SILENCE_END_MS = 700
MIN_UTTERANCE_MS = 350
# Seconds of caller audio held while EMMA speaks, so the first words of an
# interruption are still transcribed once barge-in fires.
PREBUFFER_SECONDS = 3
# How much of that buffer is forwarded when barge-in fires: enough to contain
# the caller's onset without replaying seconds of EMMA's own voice.
BARGE_IN_PREROLL_MS = 800

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


def log(message: str) -> None:
    """Print a redacted, flushed log line."""
    print(redact(message), flush=True)


def warn(message: str) -> None:
    """Print a redacted warning to stderr."""
    print(redact(f"[Agent] {message}"), file=sys.stderr, flush=True)


def _env_int(name: str, default: int) -> int:
    """Read an integer override from the environment, keeping the default on error."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        warn(f"{name}={raw!r} is not an integer; using {default}")
        return default


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
    """Latency telemetry for one caller turn, all from monotonic clocks.

    Attributes:
        turn_id: Turn the numbers belong to.
        stt_ms: Caller silence to finalized transcript.
        llm_first_ms: Caller silence to the model's first streamed text.
        first_audio_ms: Caller silence to the first PCM chunk published.
        turn_total_ms: Caller silence to the end of playback.
        tool_calls: Tools executed for this turn.
        superseded: True when a newer turn or barge-in cancelled the response.
    """

    turn_id: int = 0
    stt_ms: float = 0.0
    llm_first_ms: float = 0.0
    first_audio_ms: float = 0.0
    turn_total_ms: float = 0.0
    tool_calls: list[str] = field(default_factory=list)
    superseded: bool = False


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
            endpointing_ms=_env_int("EMMA_ENDPOINTING_MS", 300),
            utterance_end_ms=_env_int("EMMA_UTTERANCE_END_MS", 1000),
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
        self.greeting_done = False
        self.greeting_started_at = 0.0
        self.speech_started_at = 0.0
        self.caller_turns = 0
        self.stop_event = asyncio.Event()

        # Turn assembly and lifecycle. The assembler decides when a spoken
        # utterance is complete; the ledger decides which response owns the
        # conversation and invalidates stale ones.
        self._assembler = UtteranceAssembler(assembler_config_from_env())
        self._ledger = TurnLedger(max_pending=_env_int("EMMA_MAX_PENDING_TURNS", 4))
        self._sink: SpeechSink | None = None
        self._turn_task: asyncio.Task[None] | None = None

        self._turn_worker: asyncio.Task[None] | None = None
        self._stt_pump: asyncio.Task[None] | None = None
        self._greeting_task: asyncio.Task[None] | None = None
        self._stt_partials = 0
        self._stt_finals = 0
        # Half-duplex: caller audio is only forwarded to the transcriber while
        # EMMA is silent. The room echo of her own voice can never become a
        # caller turn, which is what made the assistant answer itself.
        self._stt_resume_at = 0.0
        # Updated by the event pump; the audio loop uses it to notice a stream
        # that is open but silent and transcribe the utterance in batch instead.
        self._last_stt_event_at = 0.0
        self._barge_ms = 0.0
        self._noise_rms = 0.0
        self._frames_published = 0
        self._stt_failed_reported = False
        self._audio_task: asyncio.Task[None] | None = None
        self._last_metrics: TurnMetrics | None = None

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

    async def _publish_pcm(self, pcm: bytes, generation: int) -> None:
        """Publish PCM16 at 24 kHz in 20 ms LiveKit frames.

        Args:
            pcm: Mono PCM16 at 24 kHz.
            generation: Turn generation this audio belongs to. Audio from a
                superseded generation is never published.
        """
        source = self.audio_source
        if source is None or not pcm:
            return
        if generation != self._ledger.generation or self.stop_event.is_set():
            return
        remaining = pcm
        while remaining:
            if generation != self._ledger.generation or self.stop_event.is_set():
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
                self._frames_published += 1
            except Exception as exc:  # room closing
                warn(f"publishing audio failed: {type(exc).__name__}: {exc}")
                return

    async def _on_first_audio(self) -> None:
        """Move to the speaking state when the first frame is about to play."""
        self.speaking = True
        self.speech_started_at = time.monotonic()
        self._barge_ms = 0.0
        turn = self._ledger.active_turn
        if turn is not None and turn.speech_end_at:
            log(
                f"[Latency] turn={turn.turn_id} speech_end->final={turn.latency_ms():.0f}ms "
                f"speech_end->first_audio={(self.speech_started_at - turn.speech_end_at) * 1000:.0f}ms"
            )
        await self._set_state("speaking")

    async def _start_sink(self, generation: int) -> SpeechSink:
        """Create the response output pipeline for one generation."""

        def is_current(candidate: int) -> bool:
            return candidate == self._ledger.generation and not self.stop_event.is_set()

        sink = SpeechSink(
            self.tts,
            self._publish_pcm,
            is_current=is_current,
            on_first_audio=self._on_first_audio,
        )
        self._sink = sink
        await sink.start(generation)
        return sink

    async def speak(self, text: str, *, purpose: str = "assistant") -> bool:
        """Speak ``text`` sentence by sentence, honouring barge-in.

        Args:
            text: Reply text to synthesise and play.
            purpose: ``greeting``, ``filler`` or ``assistant``.

        Returns:
            True when the whole reply played, False when it was superseded.
        """
        clean = (text or "").strip()
        if not clean or self.stop_event.is_set():
            return False

        generation = self._ledger.generation
        self.speaking = True
        self.speech_started_at = time.monotonic()
        self._barge_ms = 0.0
        self.recent_assistant_text = clean

        if purpose != "filler":
            await self.send_control({"event": "assistant", "text": clean})

        started = time.monotonic()
        sink = await self._start_sink(generation)
        completed = False
        try:
            await sink.feed(clean)
            await sink.finish()
            completed = generation == self._ledger.generation
        finally:
            if self._sink is sink:
                self._sink = None
            self.speaking = False
            # Let the room's echo tail decay before the transcriber listens.
            # After a barge-in the caller is already speaking, so resume at once.
            if generation == self._ledger.generation and not self.stop_event.is_set():
                self._stt_resume_at = time.monotonic() + 0.2
                await self._set_state("listening")
        if sink.first_audio_at:
            elapsed = (sink.first_audio_at - started) * 1000.0
            log(f"[TTS] first audio to room in {elapsed:.0f}ms")
        return completed

    def interrupt(self, reason: str = "caller") -> None:
        """Stop the current response immediately and prioritise the caller.

        A barge-in invalidates the generation, aborts synthesis and playback,
        cancels the model request that is still in flight and tells the browser
        to drop any buffered audio. Nothing from the obsolete response can be
        spoken afterwards, and the next turn starts without waiting for the
        abandoned request to finish.

        Args:
            reason: Where the interruption came from, for the log.
        """
        if not self.speaking and not self.thinking and self._turn_task is None:
            return
        if self.speaking:
            log(f"[Agent] barge-in ({reason}) — stopping output")
        elif self.thinking:
            log(f"[Agent] barge-in ({reason}) — cancelling the reply in progress")
        self._ledger.supersede()
        self.speaking = False
        self.thinking = False

        sink = self._sink
        self._sink = None
        if sink is not None:
            sink.abort()
        task = self._turn_task
        self._turn_task = None
        if task is not None and not task.done():
            task.cancel()
        if self.audio_source is not None:
            self.audio_source.clear_queue()
        # A barge-in is caller speech: start forwarding it immediately.
        self._stt_resume_at = 0.0
        self._barge_ms = 0.0
        asyncio.create_task(self._notify_barge_in(), name="barge-in-notice")

    async def _notify_barge_in(self) -> None:
        """Tell the browser to stop buffered playback and show listening."""
        if self.stop_event.is_set():
            return
        await self.send_control({"event": "barge-in"})
        await self._set_state("listening")

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

    async def accept_turn(self, turn: CallerTurn) -> bool:
        """Validate and queue one completed caller turn.

        Args:
            turn: Assembled caller utterance.

        Returns:
            True when the turn was accepted for a response.
        """
        if self.stop_event.is_set() or not turn.text.strip():
            return False
        if is_echo(turn.text, self.recent_assistant_text):
            log(f"[Agent] ignoring echo of assistant speech ({turn.source})")
            return False
        if self.speaking and (
            (time.monotonic() - self.speech_started_at) * 1000.0 < BARGE_IN_GRACE_MS
        ):
            # The assistant's own first 250 ms can leak into the microphone
            # before echo cancellation settles; do not answer it.
            log(f"[Agent] ignoring {turn.source} transcript during speech grace period")
            return False

        registered = self._ledger.register(
            turn, text_dedupe=turn.source != "deepgram-stream"
        )
        if registered is None:
            log(f"[Agent] ignoring duplicate transcript from {turn.source}")
            return False

        if self.speaking or self.thinking or self._turn_task is not None:
            # A newer turn supersedes the response in flight instead of
            # queueing behind it, which is what kept the assistant a turn
            # behind the caller.
            self.interrupt("new caller turn")
        self.recent_user_texts.append(registered.text)
        self.recent_user_texts = self.recent_user_texts[-8:]
        self._ledger.enqueue(registered)
        return True

    async def _turn_worker_loop(self) -> None:
        """Answer turns one at a time, allowing each to be superseded."""
        while not self.stop_event.is_set():
            turn = await self._ledger.next_turn(timeout=0.25)
            if turn is None:
                continue
            task = asyncio.create_task(self._run_turn(turn), name=f"turn-{turn.turn_id}")
            self._turn_task = task
            try:
                await task
            except asyncio.CancelledError:
                if self.stop_event.is_set():
                    raise
                log(f"[Agent] turn {turn.turn_id} superseded before it finished")
            except Exception as exc:  # never let one turn kill the call
                warn(f"turn failed: {type(exc).__name__}: {exc}")
                if not self.stop_event.is_set():
                    await self.speak(
                        "I'm sorry, something went wrong on my side. Could you try that again?",
                    )
            finally:
                if self._turn_task is task:
                    self._turn_task = None

    async def _run_turn(self, turn: CallerTurn) -> None:
        """Answer one assembled caller turn.

        The turn is streamed through the model into sentence-level synthesis,
        so playback starts on the first sentence instead of after the whole
        reply. Every step is guarded by the turn's generation, so a barge-in at
        any point stops the response rather than letting it play stale.
        """
        generation = self._ledger.begin_response(turn)
        self.caller_turns += 1
        log(
            f"[Agent] caller turn {turn.turn_id} ({turn.source}, {turn.reason}, "
            f"segments={turn.segment_count}): {turn.text}"
        )
        await self.send_control({"event": "transcript", "kind": "final", "text": turn.text})

        self.thinking = True
        await self._set_state("thinking")
        self.contents.append({"role": "user", "parts": [{"text": turn.text}]})
        self._trim_contents()

        sink = await self._start_sink(generation)
        reply_text = ""
        tool_names: list[str] = []
        first_text_ms = 0.0
        try:
            reply_text, tool_names, first_text_ms = await self._respond(
                turn, generation, sink
            )
        finally:
            self.thinking = False
            if not self._ledger.is_current(turn.turn_id, generation):
                sink.abort()
            else:
                await sink.finish()
            if self._sink is sink:
                self._sink = None
            self.speaking = False

        if not self._ledger.is_current(turn.turn_id, generation):
            log("[Agent] turn superseded by a barge-in; stale reply not committed")
            return

        if reply_text:
            # Only a response that actually finished is added to the context:
            # a cancelled generation must not leave half an answer behind.
            self.contents.append({"role": "model", "parts": [{"text": reply_text}]})
            self.recent_assistant_text = reply_text
        if reply_text:
            await self.send_control({"event": "assistant", "text": reply_text})
        self._ledger.complete_response(turn, generation)
        self._stt_resume_at = time.monotonic() + 0.2
        await self._set_state("listening")

        metrics = TurnMetrics(
            turn_id=turn.turn_id,
            stt_ms=turn.latency_ms(),
            llm_first_ms=turn.latency_ms() + first_text_ms,
            tool_calls=tool_names,
        )
        if sink.first_audio_at and turn.speech_end_at:
            metrics.first_audio_ms = (sink.first_audio_at - turn.speech_end_at) * 1000.0
        metrics.turn_total_ms = (time.monotonic() - turn.speech_end_at) * 1000.0
        self._last_metrics = metrics
        log(
            f"[Latency] turn={turn.turn_id} stt_final={metrics.stt_ms:.0f}ms "
            f"llm_first_text={metrics.llm_first_ms:.0f}ms "
            f"first_audio={metrics.first_audio_ms:.0f}ms "
            f"turn_total={metrics.turn_total_ms:.0f}ms tools={tool_names or 'none'} "
            f"chunks={sink.chunks_published} turns={self._assembler.turns_finalized} "
            f"dupes={self._assembler.segments_duplicate} late={self._assembler.segments_late} "
            f"ledger={self._ledger.stats()} budget={self.budget.usage}"
        )

    async def _respond(
        self, turn: CallerTurn, generation: int, sink: SpeechSink
    ) -> tuple[str, list[str], float]:
        """Stream the model reply into the sink, executing tool rounds.

        Args:
            turn: Turn being answered.
            generation: Generation the response speaks for.
            sink: Output pipeline receiving the text.

        Returns:
            The complete reply text, the tools executed, and the model's
            time-to-first-text in milliseconds.
        """
        reply_parts: list[str] = []
        tool_names: list[str] = []
        first_text_ms = 0.0

        def current() -> bool:
            return self._ledger.is_current(turn.turn_id, generation)

        def assembled() -> str:
            return " ".join("".join(reply_parts).split())

        for _round in range(MAX_LLM_TOOL_ROUNDS):
            if not current():
                break
            round_parts: list[str] = []
            calls: list[FunctionCall] = []
            failure = ""
            async for chunk in self.llm.stream(
                system_instruction=self.system_instruction,
                contents=self.contents,
                tools=self.tools.gemini_tools(),
            ):
                if not current():
                    return assembled(), tool_names, first_text_ms
                if chunk.text:
                    round_parts.append(chunk.text)
                    reply_parts.append(chunk.text)
                    if not first_text_ms and chunk.text.strip():
                        first_text_ms = chunk.first_text_ms or chunk.latency_ms
                    await sink.feed(chunk.text)
                calls.extend(chunk.function_calls)
                if not first_text_ms and chunk.latency_ms:
                    # A tool round produces no text; the model's latency is when
                    # it decided to call the tool.
                    first_text_ms = chunk.latency_ms
                if chunk.error and not chunk.text and not chunk.function_calls:
                    failure = chunk.error

            if failure and not calls and not round_parts:
                # The streaming endpoint failed before producing anything; one
                # non-streaming retry keeps the call usable on an incompatible
                # gateway.
                log(f"[Agent] streaming model failed ({failure}); retrying without streaming")
                reply = await self.llm.generate(
                    system_instruction=self.system_instruction,
                    contents=self.contents,
                    tools=self.tools.gemini_tools(),
                )
                if reply.text:
                    reply_parts.append(reply.text)
                    await sink.feed(reply.text)
                calls = list(reply.function_calls)
                failure = reply.error or ""

            if calls:
                if not current():
                    return assembled(), tool_names, first_text_ms
                responses: list[dict[str, Any]] = []
                direct: list[str] = []
                for call in calls:
                    tool_names.append(call.name)
                    log(f"[Tools] {call.name} {redact(json.dumps(call.arguments))}")
                    # Tool execution can touch disk and SMTP; keep it off the
                    # event loop so barge-in detection stays responsive.
                    result, spoken = await asyncio.to_thread(
                        self.tools.execute, call.name, call.arguments
                    )
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
                    elif spoken and not round_parts:
                        # A non-direct tool message is a fallback for a model
                        # round that produced no text of its own.
                        reply_parts.append(f" {spoken}")
                self.contents.append({"role": "user", "parts": responses})

                if direct:
                    # The tool's message is already a grounded, spoken
                    # confirmation. Speaking it directly removes a whole model
                    # round trip from the turn (seconds on a slow link) and
                    # guarantees the caller hears what the backend actually did.
                    for spoken in direct:
                        reply_parts.append(f" {spoken}")
                        await sink.feed(f" {spoken}")
                    break
                if not current():
                    return assembled(), tool_names, first_text_ms
                continue

            if failure and not round_parts:
                fallback = (
                    "I'm having trouble reaching the practice system. "
                    "Could you say that again?"
                )
                reply_parts.append(f" {fallback}")
                await sink.feed(f" {fallback}")
            break
        else:
            if not any(part.strip() for part in reply_parts):
                fallback = "Let me get the reception team to help with that."
                reply_parts.append(f" {fallback}")
                await sink.feed(f" {fallback}")

        if not any(part.strip() for part in reply_parts):
            fallback = "I'm sorry, I didn't catch that. Could you say it again?"
            reply_parts.append(f" {fallback}")
            await sink.feed(f" {fallback}")
        return assembled(), tool_names, first_text_ms

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
                self.accept_turn(
                    CallerTurn(
                        text=message["text"],
                        source="browser",
                        reason="text",
                        speech_end_at=time.monotonic(),
                        created_at=time.monotonic(),
                    )
                ),
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
        PREBUFFER_MAX = INPUT_SAMPLE_RATE * 2 * PREBUFFER_SECONDS
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

                # Barge-in: sustained caller energy while EMMA is speaking or
                # still generating. The threshold tracks the room's noise
                # floor, so a quiet caller on a quiet link interrupts as
                # reliably as a loud one in a noisy room, and noise alone does
                # not stop her mid-sentence.
                if self.speaking or self.thinking:
                    since_speech = (time.monotonic() - self.speech_started_at) * 1000.0
                    threshold = max(
                        BARGE_IN_MIN_RMS,
                        min(BARGE_IN_MAX_RMS, self._noise_rms * BARGE_IN_NOISE_FACTOR),
                    )
                    confirm_ms = (
                        BARGE_IN_MIN_MS if self.speaking else BARGE_IN_THINKING_MIN_MS
                    )
                    grace_ms = BARGE_IN_GRACE_MS if self.speaking else 0.0
                    if since_speech > grace_ms and rms > threshold:
                        self._barge_ms += duration_ms
                        if self._barge_ms >= confirm_ms:
                            self._barge_ms = 0.0
                            self.interrupt(
                                "caller energy" if self.speaking else "caller speech while thinking"
                            )
                            # The caller started speaking while playback was
                            # still muting the transcriber. Forward the held
                            # pre-roll so the interruption loses no words.
                            self._assembler.on_caller_speech(now=time.monotonic())
                            if prebuffer and self.stt.healthy:
                                # Forward only the run-up to the interruption:
                                # the buffer also holds EMMA's own previous
                                # speech, which must not become a caller turn.
                                keep = INPUT_SAMPLE_RATE * 2 * BARGE_IN_PREROLL_MS // 1000
                                if len(prebuffer) > keep:
                                    del prebuffer[: len(prebuffer) - keep]
                                log(f"[STT] flushing {len(prebuffer)} buffered bytes after barge-in")
                                await self.stt.send(bytes(prebuffer))
                                prebuffer.clear()
                    elif rms < SPEECH_RMS:
                        self._barge_ms = max(0.0, self._barge_ms - duration_ms)
                else:
                    self._barge_ms = 0.0
                    # Between turns the caller's own silence defines the floor.
                    sample = min(float(rms), float(SPEECH_RMS))
                    self._noise_rms = (
                        sample if not self._noise_rms else self._noise_rms * 0.95 + sample * 0.05
                    )

                listening = not self.speaking and time.monotonic() >= self._stt_resume_at
                if listening and self.stt.healthy:
                    if prebuffer:
                        log(f"[STT] flushing {len(prebuffer)} buffered bytes to the stream")
                        await self.stt.send(bytes(prebuffer))
                        prebuffer.clear()
                    await self.stt.send(pcm)
                elif not self.stt.failed:
                    # Hold the caller's audio while EMMA speaks (or while the
                    # socket is still connecting). It is not transcribed yet —
                    # half-duplex keeps EMMA's echo out of the turn — but an
                    # interruption would otherwise lose its first words.
                    prebuffer.extend(pcm)
                    if len(prebuffer) > PREBUFFER_MAX:
                        del prebuffer[: len(prebuffer) - PREBUFFER_MAX]

                # Energy-based end of speech. Deepgram's endpointing can fail
                # to fire on a noisy browser microphone, leaving the stream
                # with partials but no final; silence is the independent signal
                # the assembler requires before it will finalize a turn.
                now = time.monotonic()
                if not listening:
                    voice_ms = 0.0
                    caller_active = False
                elif rms >= SPEECH_RMS:
                    voice_ms += duration_ms
                    last_voice_at = now
                    self._assembler.on_caller_speech(now=now)
                    if voice_ms >= 120.0:
                        caller_active = True
                elif caller_active and now - last_voice_at >= 1.3:
                    caller_active = False
                    voice_ms = 0.0
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
                                    self._transcribe_and_enqueue(
                                        utterance, speech_end_at=time.monotonic()
                                    ),
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
                            speech_end = time.monotonic() - SILENCE_END_MS / 1000.0
                            if utterance_ms >= MIN_UTTERANCE_MS:
                                audio = bytes(batch_buffer)
                                batch_buffer.clear()
                                utterance_ms = 0.0
                                asyncio.create_task(
                                    self._transcribe_and_enqueue(
                                        audio, speech_end_at=speech_end
                                    ),
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

    async def _transcribe_and_enqueue(self, pcm: bytes, *, speech_end_at: float = 0.0) -> None:
        """Batch-transcribe one utterance and feed it through the turn ledger."""
        started = time.monotonic()
        text = await self.batch_stt.transcribe(pcm, INPUT_SAMPLE_RATE)
        if not text:
            return
        log(f"[STT] batch transcript in {(time.monotonic() - started) * 1000:.0f}ms")
        await self.accept_turn(
            CallerTurn(
                text=text,
                source="deepgram-batch",
                reason="batch",
                speech_end_at=speech_end_at or started,
                created_at=time.monotonic(),
            )
        )

    async def _pump_stt_events(self) -> None:
        """Turn provider events into transcript updates and caller turns.

        The pump only translates events. Whether an utterance is complete, and
        when a turn may be answered, is decided by
        :class:`~emma.services.turn_manager.UtteranceAssembler` from provider
        signals plus the audio loop's energy silence.
        """
        while not self.stop_event.is_set():
            now = time.monotonic()

            # Deadlines are checked on every iteration, not only when the event
            # queue is idle: a stream that keeps emitting partials would
            # otherwise strand a finished utterance.
            turn = self._assembler.poll(now=now)
            if turn is not None:
                await self.accept_turn(turn)

            # The caller has gone quiet and the provider committed no final:
            # ask it to flush the utterance it is still holding.
            if self.stt.healthy and self._assembler.take_finalize_request(now=now):
                await self.stt.finalize()

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
                    self._stt_partials += 1
                    self._assembler.on_partial(event, now=time.monotonic())
                    await self.send_control(
                        {"event": "transcript", "kind": "partial", "text": event.text}
                    )
            elif event.type == "final":
                self.last_transcript_partial = ""
                if event.text:
                    self._stt_finals += 1
                    turn = self._assembler.on_final(event, now=time.monotonic())
                    if turn is not None:
                        await self.accept_turn(turn)
            elif event.type == "utterance_end":
                turn = self._assembler.on_utterance_end(now=time.monotonic())
                if turn is not None:
                    await self.accept_turn(turn)
            elif event.type == "speech_started":
                self._assembler.on_caller_speech(now=time.monotonic())

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
        if self._sink is not None:
            self._sink.abort()
            self._sink = None
        for task in (self._stt_pump, self._turn_worker, self._turn_task, self._greeting_task):
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

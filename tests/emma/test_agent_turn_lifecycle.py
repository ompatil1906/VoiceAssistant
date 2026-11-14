#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""End-to-end turn lifecycle tests for the LiveKit agent.

The agent is constructed with its real turn assembler and turn ledger, but with
stand-in providers and a stand-in room, so these tests can assert the
conversation-level guarantees without a network call:

* one spoken sentence produces exactly one response;
* provider segments arriving in fragments do not each produce an answer;
* a barge-in cancels the response in flight and the next turn is answered
  immediately rather than queueing behind it;
* no control message announces an answer that was superseded.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from emma.services.gemini_client import FunctionCall, StreamChunk
from emma.services.streaming_stt import TranscriptEvent
from emma.services.turn_manager import CallerTurn

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_agent_module():
    """Import ``scripts/run_agent_session.py`` without making scripts a package."""
    path = REPO_ROOT / "scripts" / "run_agent_session.py"
    spec = importlib.util.spec_from_file_location("emma_agent_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeParticipant:
    """Captures reliable control messages published to the room."""

    def __init__(self, owner: FakeRoom) -> None:
        self._owner = owner

    async def publish_data(self, payload: bytes, reliable: bool = True) -> None:
        del reliable
        self._owner.messages.append(json.loads(payload.decode("utf-8")))


class FakeRoom:
    """A connected room that records control messages."""

    def __init__(self) -> None:
        self.isconnected = True
        self.messages: list[dict] = []
        self.remote_participants: dict[str, object] = {}
        self.local_participant = FakeParticipant(self)

    def events(self, name: str) -> list[dict]:
        return [message for message in self.messages if message.get("event") == name]


class FakeAudioSource:
    """Records published frames and clears like the LiveKit source."""

    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.clears = 0

    async def capture_frame(self, frame) -> None:
        self.frames.append(frame.data)

    def clear_queue(self) -> None:
        self.clears += 1


class FakeTTS:
    """Synthesiser that yields a fixed chunk per sentence."""

    def __init__(self, chunks: int = 2) -> None:
        self.chunks = chunks
        self.sentences: list[str] = []

    async def stream(self, text: str):
        self.sentences.append(text)
        for _ in range(self.chunks):
            await asyncio.sleep(0)
            yield b"\x00\x01" * 240

    async def aclose(self) -> None:
        return None


class FakeLLM:
    """Model that streams text deltas, optionally slowly or with tool calls."""

    def __init__(
        self,
        rounds: list[list[str]],
        *,
        calls: list[list[FunctionCall]] | None = None,
        delay: float = 0.0,
    ) -> None:
        self.rounds = rounds
        self.calls = calls or [[] for _ in rounds]
        self.delay = delay
        self.stream_calls = 0
        self.completed = 0
        self.deltas_emitted = 0

    async def stream(self, **_kwargs):
        index = self.stream_calls
        self.stream_calls += 1
        if index >= len(self.rounds):
            self.completed += 1
            yield StreamChunk(final=True)
            return
        for delta in self.rounds[index]:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.deltas_emitted += 1
            yield StreamChunk(text=delta, first_text_ms=1.0, latency_ms=1.0)
        self.completed += 1
        yield StreamChunk(final=True, function_calls=self.calls[index], tokens=10)

    async def generate(self, **_kwargs):
        raise AssertionError("streaming path should be used")

    async def warmup(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


class FakeSTT:
    """Event source for the pump, driven by the test."""

    def __init__(self) -> None:
        self.events: asyncio.Queue[TranscriptEvent | None] = asyncio.Queue()
        self.healthy = True
        self.failed = False
        self.finalize_calls = 0

    async def next_event(self, timeout: float = 0.1):
        try:
            return await asyncio.wait_for(self.events.get(), timeout=timeout)
        except TimeoutError:
            return None

    async def finalize(self) -> None:
        self.finalize_calls += 1

    def push(self, event: TranscriptEvent) -> None:
        self.events.put_nowait(event)


@pytest.fixture
def agent_module(monkeypatch, tmp_path):
    """Load the agent module with a policy configuration that permits the plan."""
    monkeypatch.setenv("EMMA_PROVIDER_PROFILE", "custom")
    monkeypatch.setenv("EMMA_PROVIDERS", "gemini,deepgram,elevenlabs")
    monkeypatch.setenv("EMMA_ALLOW_PAID", "true")
    monkeypatch.setenv("EMMA_ALLOW_TRIAL", "true")
    monkeypatch.setenv("EMMA_DATA_MODE", "approved")
    monkeypatch.setenv("EMMA_MAX_SESSION_REQUESTS", "100")
    monkeypatch.setenv("EMMA_MAX_SESSION_TOKENS", "10000")
    monkeypatch.setenv("EMMA_MAX_SESSION_CHARACTERS", "10000")
    monkeypatch.setenv("EMMA_MAX_SESSION_AUDIO_SECONDS", "600")
    monkeypatch.setenv("EMMA_MAX_SESSION_MINUTES", "60")
    monkeypatch.setenv("EMMA_MAX_SESSION_USD", "0")
    monkeypatch.setenv("CALL_RECORDS_DIR", str(tmp_path))
    monkeypatch.setenv("APPOINTMENT_LEDGER_PATH", str(tmp_path / "appointments.json"))
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    return load_agent_module()


async def build_agent(module, *, llm: FakeLLM, tts: FakeTTS | None = None):
    """Construct an agent wired to stand-in providers and a fake room."""
    agent = module.EmmaAgent(
        "test-room",
        {"firstName": "Sam"},
        plan=module.ProviderPlan(llm="gemini", stt="deepgram", tts="elevenlabs"),
    )
    agent.llm = llm
    agent.tts = tts or FakeTTS()
    agent.batch_stt = object()
    agent.stt = FakeSTT()
    agent.room = FakeRoom()
    agent.audio_source = FakeAudioSource()
    return agent


async def stop_agent(agent, *tasks: asyncio.Task) -> None:
    """Stop the agent's loops and wait for their cancellation."""
    agent.stop_event.set()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def wait_until(predicate, timeout: float = 3.0) -> None:
    """Poll ``predicate`` until true, failing the test on timeout."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met before the deadline")


def segment(
    text: str,
    *,
    start: float,
    duration: float,
    speech_final: bool = False,
) -> TranscriptEvent:
    """Build a provider final segment."""
    return TranscriptEvent(
        type="final",
        text=text,
        start=start,
        duration=duration,
        speech_final=speech_final,
        is_final=True,
    )


@pytest.mark.asyncio
async def test_one_spoken_sentence_produces_exactly_one_response(agent_module):
    """A sentence split into provider finals is answered once, in full."""
    llm = FakeLLM([["We are open from eight until six on weekdays."]])
    agent = await build_agent(agent_module, llm=llm)
    pump = asyncio.create_task(agent._pump_stt_events(), name="pump")
    worker = asyncio.create_task(agent._turn_worker_loop(), name="worker")
    try:
        agent._assembler.on_caller_speech()
        agent.stt.push(
            TranscriptEvent(type="partial", text="Hi I wanted to ask", start=0.0, duration=1.0)
        )
        agent.stt.push(segment("Hi I wanted to ask", start=0.0, duration=1.1))
        await asyncio.sleep(0.25)
        agent._assembler.on_caller_speech()
        agent.stt.push(segment("about your opening hours", start=1.3, duration=1.2))
        await asyncio.sleep(0.25)
        agent._assembler.on_caller_speech()
        agent.stt.push(
            segment(
                "on a weekday please?",
                start=2.6,
                duration=1.4,
                speech_final=True,
            )
        )
        await wait_until(lambda: llm.stream_calls >= 1)
        await wait_until(lambda: agent.room.events("state") and agent._turn_task is None)
        await asyncio.sleep(0.1)

        assert llm.stream_calls == 1, "one utterance must not produce several model calls"
        finals = [message["text"] for message in agent.room.events("transcript") if message["kind"] == "final"]
        assert finals == ["Hi I wanted to ask about your opening hours on a weekday please?"]
        assert agent.caller_turns == 1
        assistant_lines = agent.room.events("assistant")
        assert len(assistant_lines) == 1
        assert agent.audio_source.frames
    finally:
        await stop_agent(agent, pump, worker)


@pytest.mark.asyncio
async def test_duplicate_final_and_utterance_end_answer_only_once(agent_module):
    """Repeated provider events for one utterance do not reopen the turn."""
    llm = FakeLLM([["Yes, the practice is open today."]])
    agent = await build_agent(agent_module, llm=llm)
    pump = asyncio.create_task(agent._pump_stt_events(), name="pump")
    worker = asyncio.create_task(agent._turn_worker_loop(), name="worker")
    try:
        agent._assembler.on_caller_speech()
        segment_one = segment(
            "Are you open today?", start=0.0, duration=1.5, speech_final=True
        )
        agent.stt.push(segment_one)
        agent.stt.push(segment_one)
        agent.stt.push(TranscriptEvent(type="utterance_end"))
        agent.stt.push(TranscriptEvent(type="utterance_end"))

        await wait_until(lambda: llm.stream_calls >= 1)
        await asyncio.sleep(0.4)
        assert llm.stream_calls == 1
        assert agent.caller_turns == 1
        finals = [message for message in agent.room.events("transcript") if message["kind"] == "final"]
        assert len(finals) == 1
    finally:
        await stop_agent(agent, pump, worker)


@pytest.mark.asyncio
async def test_barge_in_cancels_the_reply_and_answers_the_new_turn(agent_module):
    """An interruption stops the stale reply and the next turn is not backlogged."""
    llm = FakeLLM(
        [
            ["This is a long answer that the caller will interrupt. "] * 8,
            ["Certainly, Friday at three is available."],
        ],
        delay=0.05,
    )
    tts = FakeTTS()
    agent = await build_agent(agent_module, llm=llm, tts=tts)
    worker = asyncio.create_task(agent._turn_worker_loop(), name="worker")
    try:
        first = agent._ledger.register(
            CallerTurn(text="Tell me about the practice", source="deepgram-stream")
        )
        assert first is not None
        agent._ledger.enqueue(first)

        await wait_until(lambda: agent.thinking or agent.speaking)
        if agent._turn_task is not None:
            cancelled = agent._turn_task
            agent.interrupt("test barge-in")
            await asyncio.sleep(0.05)
            assert cancelled.cancelled() or cancelled.done()
        assert agent.room.events("barge-in"), "the browser must be told to drop buffered audio"

        frames_after_interrupt = len(agent.audio_source.frames)
        await asyncio.sleep(0.2)
        assert len(agent.audio_source.frames) == frames_after_interrupt

        second = agent._ledger.register(
            CallerTurn(text="Actually, book me in for Friday at three", source="deepgram-stream")
        )
        assert second is not None
        agent._ledger.enqueue(second)

        await wait_until(lambda: llm.stream_calls >= 2)
        await wait_until(lambda: agent._turn_task is None)
        assert agent.contents[0]["parts"][0]["text"] == "Tell me about the practice"
        # The interrupted reply is not committed as an assistant turn.
        assistant_turns = [
            part for part in agent.contents if part["role"] == "model"
        ]
        assert len(assistant_turns) == 1
        assert "Friday at three" in assistant_turns[0]["parts"][0]["text"]
    finally:
        await stop_agent(agent, worker)


@pytest.mark.asyncio
async def test_streamed_reply_starts_speaking_before_the_model_finishes(agent_module):
    """First audio is published while later sentences are still arriving."""
    deltas = [f"Sentence number {index} is spoken now. " for index in range(1, 6)]
    llm = FakeLLM([deltas], delay=0.05)
    agent = await build_agent(agent_module, llm=llm)
    worker = asyncio.create_task(agent._turn_worker_loop(), name="worker")
    try:
        turn = agent._ledger.register(CallerTurn(text="Say something long please"))
        assert turn is not None
        agent._ledger.enqueue(turn)

        await wait_until(lambda: bool(agent.audio_source.frames))
        assert llm.completed == 0, "audio must exist while the model is still streaming"
        assert llm.stream_calls == 1

        await wait_until(lambda: llm.completed >= 1, timeout=5.0)
        await wait_until(lambda: agent._turn_task is None, timeout=5.0)
        assert len(tts_sentences(agent)) >= 2
        assert agent._last_metrics is not None
        assert agent._last_metrics.turn_id == turn.turn_id
    finally:
        await stop_agent(agent, worker)


def tts_sentences(agent) -> list[str]:
    """Sentences the fake synthesiser was asked to speak."""
    return list(agent.tts.sentences)


@pytest.mark.asyncio
async def test_a_tool_round_speaks_the_grounded_confirmation_directly(agent_module):
    """A direct-speech tool reply skips the extra model round trip."""
    llm = FakeLLM(
        rounds=[[], ["You can reach the practice on the main line."]],
        calls=[[FunctionCall(name="get_clinic_info", arguments={"topic": "contact"})], []],
    )
    agent = await build_agent(agent_module, llm=llm)
    seen: list[str] = []

    def execute(name, arguments):
        seen.append(name)
        return {"success": True, "message": "The practice number is 020 7946 0011."}, (
            "The practice number is 020 7946 0011."
        )

    agent.tools.execute = execute  # type: ignore[method-assign]
    worker = asyncio.create_task(agent._turn_worker_loop(), name="worker")
    try:
        turn = agent._ledger.register(CallerTurn(text="What is your phone number?"))
        assert turn is not None
        agent._ledger.enqueue(turn)
        await wait_until(lambda: llm.stream_calls >= 1, timeout=3.0)
        await wait_until(lambda: agent._turn_task is None, timeout=3.0)

        assert seen == ["get_clinic_info"]
        assert llm.stream_calls == 1, "the tool's message is spoken without another round"
        assert "020 7946 0011" in agent.recent_assistant_text
        assert any(
            "020 7946 0011" in message.get("text", "")
            for message in agent.room.events("assistant")
        )
    finally:
        await stop_agent(agent, worker)

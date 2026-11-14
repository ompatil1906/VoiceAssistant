#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Tests for sentence-level synthesis pipelined with model streaming.

These assert the latency property the live call needs: the first PCM chunk is
published long before the model has finished, synthesis of the next sentence
overlaps playback of the current one, and a superseded generation publishes
nothing at all.
"""

from __future__ import annotations

import asyncio

import pytest

from emma.services.response_stream import SpeechSink


class FakeTTS:
    """A synthesiser that records which sentence it is working on."""

    def __init__(self, chunks_per_sentence: int = 3, delay: float = 0.0) -> None:
        self.chunks_per_sentence = chunks_per_sentence
        self.delay = delay
        self.sentences: list[str] = []
        self.started_at: dict[str, float] = {}
        self._loop = asyncio.get_event_loop()

    async def stream(self, text: str):
        """Yield PCM chunks for one sentence."""
        self.sentences.append(text)
        self.started_at[text] = self._loop.time()
        for index in range(self.chunks_per_sentence):
            if self.delay:
                await asyncio.sleep(self.delay)
            yield f"{text}:{index}".encode()


class Recorder:
    """The publisher callback, recording order and timing."""

    def __init__(self, delay: float = 0.0) -> None:
        self.chunks: list[tuple[bytes, int]] = []
        self.times: list[float] = []
        self.delay = delay
        self._loop = asyncio.get_event_loop()

    async def __call__(self, chunk: bytes, generation: int) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.chunks.append((chunk, generation))
        self.times.append(self._loop.time())

    @property
    def text(self) -> list[str]:
        return [chunk.decode().rsplit(":", 1)[0] for chunk, _ in self.chunks]


async def run_sink(
    sink: SpeechSink, deltas: list[str], *, abort_after: int | None = None
) -> None:
    """Feed ``deltas`` and finish, optionally aborting after N published chunks."""
    await sink.start(0)
    for delta in deltas:
        await sink.feed(delta)
        if abort_after is not None and sink.chunks_published >= abort_after:
            sink.abort()
            return
    await sink.finish()


@pytest.mark.asyncio
async def test_first_sentence_is_published_before_the_model_finishes():
    """The sink starts playing once the first sentence exists."""
    tts = FakeTTS(chunks_per_sentence=2)
    recorder = Recorder()
    first_chunk = asyncio.Event()

    async def publish(chunk: bytes, generation: int) -> None:
        await recorder(chunk, generation)
        first_chunk.set()

    sink = SpeechSink(tts, publish)
    await sink.start(0)
    await sink.feed("The clinic is open until six. ")

    # The model has not produced the rest of the reply yet, but audio is out.
    await asyncio.wait_for(first_chunk.wait(), timeout=1.0)
    assert recorder.text[0] == "The clinic is open until six."
    assert tts.sentences == ["The clinic is open until six."]

    await sink.feed("We are closed on Sundays. ")
    await sink.finish()
    assert recorder.text == [
        "The clinic is open until six.",
        "The clinic is open until six.",
        "We are closed on Sundays.",
        "We are closed on Sundays.",
    ]


@pytest.mark.asyncio
async def test_synthesis_of_the_next_sentence_overlaps_playback():
    """Sentence N+1 is synthesised while sentence N is still playing."""
    tts = FakeTTS(chunks_per_sentence=3)
    published_first = asyncio.Event()
    release_playback = asyncio.Event()

    async def publish(chunk: bytes, generation: int) -> None:
        del chunk, generation
        published_first.set()
        # Hold playback until the test says so, simulating real-time audio.
        await release_playback.wait()

    sink = SpeechSink(tts, publish)
    await sink.start(0)
    await sink.feed("First sentence is here. Second sentence follows. ")

    await asyncio.wait_for(published_first.wait(), timeout=1.0)
    # Synthesis must not be waiting for the first sentence's playback.
    for _ in range(100):
        if len(tts.sentences) >= 2:
            break
        await asyncio.sleep(0.01)
    assert tts.sentences[:2] == ["First sentence is here.", "Second sentence follows."]

    release_playback.set()
    await sink.finish()


@pytest.mark.asyncio
async def test_chunks_are_published_in_order_and_exactly_once():
    """The audio queue preserves synthesis order."""
    tts = FakeTTS(chunks_per_sentence=3)
    recorder = Recorder()
    sink = SpeechSink(tts, recorder)

    await sink.start(0)
    await sink.feed("One sentence with several chunks. ")
    await sink.feed("Another sentence here. ")
    await sink.finish()

    assert [chunk for chunk, _ in recorder.chunks] == [
        b"One sentence with several chunks.:0",
        b"One sentence with several chunks.:1",
        b"One sentence with several chunks.:2",
        b"Another sentence here.:0",
        b"Another sentence here.:1",
        b"Another sentence here.:2",
    ]
    assert {generation for _, generation in recorder.chunks} == {0}


@pytest.mark.asyncio
async def test_abort_stops_playback_immediately():
    """A barge-in publishes nothing after the abort."""
    tts = FakeTTS(chunks_per_sentence=10)
    recorder = Recorder()
    sink = SpeechSink(tts, recorder)

    await sink.start(0)
    await sink.feed("A long sentence that keeps producing chunks. ")
    await asyncio.sleep(0)
    while sink.chunks_published < 2:
        await asyncio.sleep(0.005)
    sink.abort()

    published = sink.chunks_published
    await asyncio.sleep(0.05)
    assert sink.chunks_published == published
    assert len(recorder.chunks) == published
    # finish() after an abort is a no-op rather than a hang or an exception.
    await sink.finish()
    assert len(recorder.chunks) == published
    assert tts.sentences


@pytest.mark.asyncio
async def test_a_superseded_generation_publishes_nothing_further():
    """When the ledger generation moves on, the sink goes silent."""
    tts = FakeTTS(chunks_per_sentence=10)
    recorder = Recorder()
    generation = {"value": 0}
    sink = SpeechSink(tts, recorder, is_current=lambda candidate: candidate == generation["value"])

    await sink.start(generation["value"])
    await sink.feed("Playing a sentence that is about to be superseded. ")
    while sink.chunks_published < 2:
        await asyncio.sleep(0.005)

    generation["value"] = 1
    published = sink.chunks_published
    await asyncio.sleep(0.05)
    assert sink.chunks_published == published
    sink.abort()


@pytest.mark.asyncio
async def test_first_audio_callback_runs_once_before_the_first_chunk():
    """The speaking state is announced exactly once, before any audio."""
    tts = FakeTTS(chunks_per_sentence=2)
    recorder = Recorder()
    calls: list[float] = []

    async def on_first_audio() -> None:
        calls.append(len(recorder.chunks))

    sink = SpeechSink(tts, recorder, on_first_audio=on_first_audio)
    await sink.start(0)
    await sink.feed("Hello there, this is EMMA. ")
    await sink.feed("How can I help today? ")
    await sink.finish()

    assert calls == [0]
    assert len(recorder.chunks) == 4


@pytest.mark.asyncio
async def test_empty_text_finishes_without_audio():
    """A response with no text must not hang the turn."""
    tts = FakeTTS()
    recorder = Recorder()
    sink = SpeechSink(tts, recorder)
    await sink.start(0)
    await sink.finish()
    assert recorder.chunks == []
    assert tts.sentences == []

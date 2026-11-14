#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Streaming response output: model text → synthesis → playback.

A reply must start playing as soon as its first sentence exists. Waiting for
the model's stop token and then for a single large synthesis request is what
made the assistant answer one or two turns behind the caller.

:class:`SpeechSink` wires three stages together with bounded queues:

    model deltas ──► SentenceChunker ──► TTS stream ──► publish(PCM)

Each stage runs in its own task, so synthesis of sentence *n+1* overlaps
playback of sentence *n*, and the queues are bounded so a fast model cannot
buffer minutes of audio. Every stage checks the turn generation it was started
with: once a barge-in or a newer turn bumps the generation, no further audio is
published, which is what guarantees an obsolete reply cannot play after the
conversation has moved on.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

try:
    from loguru import logger
except ImportError:  # pragma: no cover - loguru is a runtime dependency
    import logging

    logger = logging.getLogger("emma.response_stream")

SENTINEL_AUDIO = b""
SENTINEL_SENTENCE = ""


class SpeechSink:
    """Pipeline model text through synthesis into published PCM frames.

    Args:
        tts: Object with ``stream(text) -> AsyncIterator[bytes]``.
        publish: Awaitable publishing one PCM chunk for a generation.
        is_current: Predicate telling the sink whether its generation is still
            the live one. Defaults to comparing against the generation passed
            to :meth:`start`.
        on_first_audio: Called once, immediately before the first chunk is
            published. Used to move the UI into the speaking state.
        sentence_queue_size: Sentences waiting for synthesis.
        audio_queue_size: Synthesised chunks waiting for playback.
    """

    def __init__(
        self,
        tts: Any,
        publish: Callable[[bytes, int], Awaitable[None]],
        *,
        is_current: Callable[[int], bool] | None = None,
        on_first_audio: Callable[[], Awaitable[None]] | None = None,
        sentence_queue_size: int = 8,
        audio_queue_size: int = 96,
    ) -> None:
        """Configure the provider chain, the publisher and the queue bounds."""
        from emma.services.turn_manager import SentenceChunker

        self._tts = tts
        self._publish = publish
        self._is_current = is_current or (lambda generation: True)
        self._on_first_audio = on_first_audio
        self._sentences: asyncio.Queue[str] = asyncio.Queue(maxsize=sentence_queue_size)
        self._audio: asyncio.Queue[bytes] = asyncio.Queue(maxsize=audio_queue_size)
        self._chunker = SentenceChunker()
        self._generation = 0
        self._tasks: list[asyncio.Task[None]] = []
        self._aborted = False
        self._first_audio_at = 0.0
        self._chunks_published = 0
        self._synth_done = False

    @property
    def generation(self) -> int:
        """Generation this sink speaks for."""
        return self._generation

    @property
    def first_audio_at(self) -> float:
        """Monotonic time the first chunk was published, or 0."""
        return self._first_audio_at

    @property
    def chunks_published(self) -> int:
        """Number of PCM chunks published for this response."""
        return self._chunks_published

    def _allowed(self, generation: int) -> bool:
        return not self._aborted and self._is_current(generation)

    async def start(self, generation: int) -> None:
        """Start the synthesis and playback tasks.

        Args:
            generation: Turn generation this response belongs to.
        """
        self._generation = generation
        self._aborted = False
        self._synth_done = False
        self._tasks = [
            asyncio.create_task(self._synthesize(), name="speech-synth"),
            asyncio.create_task(self._play(), name="speech-play"),
        ]

    async def feed(self, text: str) -> None:
        """Add streamed model text, queueing any sentences it completed.

        Args:
            text: A model delta.
        """
        for sentence in self._chunker.feed(text):
            if not self._allowed(self._generation):
                return
            try:
                await self._sentences.put(sentence)
            except asyncio.CancelledError:
                raise

    async def finish(self) -> None:
        """Signal end of text and wait until every queued chunk is published."""
        tail = self._chunker.flush()
        if tail and self._allowed(self._generation):
            try:
                await self._sentences.put(tail)
            except asyncio.CancelledError:
                raise
        try:
            self._sentences.put_nowait(SENTINEL_SENTENCE)
        except asyncio.QueueFull:
            # The queue is full, so the sentinel cannot be inserted before the
            # remaining sentences; wait for room instead of dropping audio.
            await self._sentences.put(SENTINEL_SENTENCE)
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                continue
            except Exception as exc:  # a synthesis failure must not kill the turn
                logger.warning(f"[Speech] stage failed: {type(exc).__name__}: {exc}")
        self._tasks = []

    async def _synthesize(self) -> None:
        """Consume sentences and produce audio chunks."""
        generation = self._generation
        try:
            while True:
                sentence = await self._sentences.get()
                if sentence == SENTINEL_SENTENCE:
                    return
                if not self._allowed(generation):
                    continue
                async for chunk in self._tts.stream(sentence):
                    if not self._allowed(generation):
                        return
                    if not chunk:
                        continue
                    await self._audio.put(chunk)
        finally:
            self._synth_done = True
            try:
                self._audio.put_nowait(SENTINEL_AUDIO)
            except asyncio.QueueFull:
                # _play notices _synth_done once it has drained the queue.
                pass

    async def _play(self) -> None:
        """Publish audio chunks in order until synthesis ends or is superseded."""
        generation = self._generation
        while True:
            try:
                chunk = await asyncio.wait_for(self._audio.get(), timeout=0.25)
            except TimeoutError:
                if self._synth_done and self._audio.empty():
                    return
                if not self._allowed(generation):
                    return
                continue
            if chunk == SENTINEL_AUDIO:
                return
            if not self._allowed(generation):
                # Drain without publishing: a superseded response must not put
                # a single additional frame on the wire.
                continue
            if not self._first_audio_at:
                self._first_audio_at = asyncio.get_running_loop().time()
                if self._on_first_audio is not None:
                    await self._on_first_audio()
            try:
                await self._publish(chunk, generation)
                self._chunks_published += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the room may have closed mid-reply
                logger.warning(f"[Speech] publish failed: {type(exc).__name__}: {exc}")
                return

    def abort(self) -> None:
        """Stop publishing immediately.

        Safe to call from a synchronous event handler: the tasks are cancelled,
        not awaited, and the caller clears the transport queue.
        """
        self._aborted = True
        for task in self._tasks:
            task.cancel()
        self._tasks = []
        while not self._audio.empty():
            try:
                self._audio.get_nowait()
            except asyncio.QueueEmpty:
                break
        while not self._sentences.empty():
            try:
                self._sentences.get_nowait()
            except asyncio.QueueEmpty:
                break

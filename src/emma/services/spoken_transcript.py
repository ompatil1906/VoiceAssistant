#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Assistant transcript streamed word by word, in step with the voice.

A reply used to reach the caller's screen only after it had finished playing:
one ``assistant`` control message carrying the whole text. That is a transcript
of the past, not a caption of the present — by the time it appears, the caller
has already heard it.

:class:`SpokenTranscript` turns the words the audio has reached into three
control messages:

    {"event": "assistant-start", "id": "a-7"}
    {"event": "assistant-delta", "id": "a-7", "text": "Hello"}
    {"event": "assistant-end",   "id": "a-7", "text": "Hello Sarah, ..."}

Only a line that actually started is closed, and a line that was interrupted
keeps the words the caller heard — text that was never voiced is never
presented as if it had been.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

try:
    from loguru import logger
except ImportError:  # pragma: no cover - loguru is a runtime dependency
    import logging

    logger = logging.getLogger("emma.spoken_transcript")

ControlSender = Callable[[dict[str, Any]], Awaitable[None]]


class SpokenTranscript:
    """One assistant line, revealed as its audio plays.

    Args:
        send: Publishes one control message to the caller's UI.
        line_id: Stable id of this line, so deltas update one paragraph.
    """

    def __init__(self, send: ControlSender, *, line_id: str) -> None:
        """Bind the line to its id and the room's control channel."""
        self._send = send
        self._line_id = line_id
        self._spoken: list[str] = []
        self._started = False
        self._closed = False

    @property
    def line_id(self) -> str:
        """Id the UI uses to update this paragraph in place."""
        return self._line_id

    @property
    def started(self) -> bool:
        """True once at least one word has been sent to the UI."""
        return self._started

    @property
    def text(self) -> str:
        """Words revealed so far, joined for display and logs."""
        return " ".join(self._spoken)

    async def push(self, words: list[str]) -> None:
        """Reveal the words the audio has just reached.

        Args:
            words: Words that are being voiced now, in order.
        """
        if self._closed or not words:
            return
        if not self._started:
            self._started = True
            await self._emit({"event": "assistant-start", "id": self._line_id})
            if self._closed:  # closed while the start message was in flight
                return
        self._spoken.extend(words)
        await self._emit(
            {
                "event": "assistant-delta",
                "id": self._line_id,
                "text": " ".join(words),
            }
        )

    async def close(self, text: str | None = None) -> None:
        """Finish the line.

        Args:
            text: The reply as written, when it completed. ``None`` means the
                line was interrupted, so only the words actually spoken are
                reported.
        """
        if self._closed:
            return
        self._closed = True
        if not self._started:
            return
        final = (text or "").strip() or self.text
        if not final:
            return
        await self._emit(
            {"event": "assistant-end", "id": self._line_id, "text": final}
        )

    async def _emit(self, payload: dict[str, Any]) -> None:
        """Send one control message; a UI failure must not break the voice."""
        try:
            await self._send(payload)
        except Exception as exc:  # the room may be closing mid-reply
            logger.warning(
                f"[Transcript] control message failed: {type(exc).__name__}: {exc}"
            )

#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Tests for the assistant transcript streamed in step with its audio.

The caller must see each word as it is voiced, not the whole reply after it has
finished playing. These tests pin the three-message protocol and the two cases
that matter on a live call: an interrupted line claims only the words the
caller actually heard, and a line that never spoke announces nothing.
"""

from __future__ import annotations

import pytest

from emma.services.spoken_transcript import SpokenTranscript


class Sender:
    """Records the control messages a line publishes."""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, payload: dict) -> None:
        self.messages.append(payload)

    def events(self, name: str) -> list[dict]:
        return [message for message in self.messages if message["event"] == name]


@pytest.mark.asyncio
async def test_words_arrive_before_the_line_is_closed():
    """Start/delta/end are published in that order for one line id."""
    sender = Sender()
    line = SpokenTranscript(sender, line_id="a-1")

    await line.push(["Hello"])
    await line.push(["Sarah,"])
    await line.push(["how", "can", "I", "help"])
    await line.close("Hello Sarah, how can I help you today?")

    assert [message["event"] for message in sender.messages] == [
        "assistant-start",
        "assistant-delta",
        "assistant-delta",
        "assistant-delta",
        "assistant-end",
    ]
    assert {message["id"] for message in sender.messages} == {"a-1"}
    assert sender.events("assistant-end")[0]["text"] == "Hello Sarah, how can I help you today?"


@pytest.mark.asyncio
async def test_an_interrupted_line_reports_only_the_words_spoken():
    """A barge-in never presents text the caller did not hear."""
    sender = Sender()
    line = SpokenTranscript(sender, line_id="a-2")

    await line.push(["I", "can", "book"])
    await line.push(["you", "in"])
    await line.close()

    final = sender.events("assistant-end")[0]
    assert final["text"] == "I can book you in"
    assert line.text == "I can book you in"


@pytest.mark.asyncio
async def test_a_line_that_never_spoke_announces_nothing():
    """A reply cancelled before its first word leaves the UI untouched."""
    sender = Sender()
    line = SpokenTranscript(sender, line_id="a-3")

    await line.close("An answer that was never spoken.")

    assert sender.messages == []
    assert line.started is False


@pytest.mark.asyncio
async def test_closing_twice_does_not_duplicate_the_final_line():
    """The turn and a barge-in may both close the line; the UI sees it once."""
    sender = Sender()
    line = SpokenTranscript(sender, line_id="a-4")

    await line.push(["Sure."])
    await line.close("Sure.")
    await line.close("Sure.")

    assert len(sender.events("assistant-end")) == 1


@pytest.mark.asyncio
async def test_a_failing_control_channel_does_not_break_the_voice():
    """A UI failure is swallowed so audio is never blocked by the transcript."""

    async def broken(_payload: dict) -> None:
        raise RuntimeError("room closed")

    line = SpokenTranscript(broken, line_id="a-5")
    await line.push(["Still speaking."])
    await line.close("Still speaking.")
    assert line.text == "Still speaking."

#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Tests for the streaming STT event shape the turn assembler depends on.

Deepgram distinguishes ``is_final`` (a committed segment) from ``speech_final``
(the provider's endpointing saw silence). Collapsing the two is what made a
sentence look finished halfway through, so these tests pin the distinction and
the segment metadata used for idempotency.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from emma.services.streaming_stt import DeepgramStreamingTranscriber


def transcriber(**kwargs) -> DeepgramStreamingTranscriber:
    """A transcriber with no network connection."""
    return DeepgramStreamingTranscriber(api_key="test", **kwargs)


def results(message: dict) -> dict:
    """A Deepgram Results frame around one alternative."""
    return {
        "type": "Results",
        "start": message.get("start", 0.0),
        "duration": message.get("duration", 1.0),
        "is_final": message.get("is_final", False),
        "speech_final": message.get("speech_final", False),
        "from_finalize": message.get("from_finalize", False),
        "channel": {"alternatives": [{"transcript": message["transcript"]}]},
    }


def test_a_committed_segment_is_not_mistaken_for_the_end_of_the_utterance():
    """is_final and speech_final are separate facts."""
    stt = transcriber()
    event = stt._to_event(  # noqa: SLF001 - provider frame parsing
        results({"transcript": "Hey. I want to understand", "is_final": True})
    )
    assert event is not None
    assert event.type == "final"
    assert event.is_final is True
    assert event.speech_final is False


def test_speech_final_is_preserved_on_the_event():
    """The assembler uses speech_final to pick its end-of-turn deadline."""
    stt = transcriber()
    event = stt._to_event(  # noqa: SLF001
        results(
            {
                "transcript": "how they interact with external tools?",
                "is_final": True,
                "speech_final": True,
                "from_finalize": True,
            }
        )
    )
    assert event is not None
    assert event.speech_final is True
    assert event.is_final is True
    assert event.from_finalize is True


def test_events_are_sequenced_in_arrival_order():
    """Sequence numbers let delayed events be ordered deterministically."""
    stt = transcriber()
    first = stt._to_event(results({"transcript": "one"}))  # noqa: SLF001
    second = stt._to_event(results({"transcript": "two"}))  # noqa: SLF001
    end = stt._to_event({"type": "UtteranceEnd"})  # noqa: SLF001
    assert first is not None and second is not None and end is not None
    assert first.sequence < second.sequence < end.sequence
    assert end.type == "utterance_end"


def test_empty_alternatives_do_not_create_events():
    """Silence frames must not open an utterance or advance the sequence."""
    stt = transcriber()
    assert stt._to_event(results({"transcript": "   "})) is None  # noqa: SLF001
    event = stt._to_event(results({"transcript": "hello"}))  # noqa: SLF001
    assert event is not None
    assert event.sequence == 1


@pytest.mark.parametrize(
    ("endpointing_ms", "utterance_end_ms", "expected_endpointing", "expected_end"),
    [
        (300, 1000, "300", "1000"),
        (200, 600, "200", "600"),
        # The provider requires UtteranceEnd to exceed endpointing.
        (700, 500, "700", "800"),
    ],
)
def test_stream_url_carries_configured_endpointing(
    endpointing_ms, utterance_end_ms, expected_endpointing, expected_end
):
    """Endpointing is configuration, not a constant buried in the URL."""
    stt = transcriber(endpointing_ms=endpointing_ms, utterance_end_ms=utterance_end_ms)
    query = parse_qs(urlparse(stt._url()).query)  # noqa: SLF001
    assert query["endpointing"] == [expected_endpointing]
    assert query["utterance_end_ms"] == [expected_end]
    assert query["interim_results"] == ["true"]
    assert query["vad_events"] == ["true"]
    assert query["smart_format"] == ["true"]

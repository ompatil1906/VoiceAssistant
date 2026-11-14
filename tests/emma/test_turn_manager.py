#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Regression tests for turn assembly, idempotency and response generations.

The failure these cover is the one reported in a live call: one spoken sentence
arriving as several provider finals, each answered separately, with the answer
starting while the caller was still talking.
"""

from __future__ import annotations

import asyncio

import pytest

from emma.services.streaming_stt import TranscriptEvent
from emma.services.turn_manager import (
    AssemblerConfig,
    CallerTurn,
    SentenceChunker,
    TurnLedger,
    UtteranceAssembler,
    assembler_config_from_env,
    ends_with_connector,
    has_terminal_punctuation,
)

FULL = (
    "Hey, I want to understand how artificial intelligence agents work "
    "and how they can interact with external tools."
)


class FakeClock:
    """A monotonic clock the test controls."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, ms: float) -> None:
        self.now += ms / 1000.0


def final(
    text: str,
    *,
    start: float,
    duration: float,
    speech_final: bool = False,
    from_finalize: bool = False,
    sequence: int = 0,
) -> TranscriptEvent:
    """Build a provider final segment."""
    return TranscriptEvent(
        type="final",
        text=text,
        start=start,
        duration=duration,
        speech_final=speech_final,
        is_final=True,
        from_finalize=from_finalize,
        sequence=sequence,
    )


def partial(text: str, *, start: float = 0.0, duration: float = 0.0) -> TranscriptEvent:
    """Build a provider interim hypothesis."""
    return TranscriptEvent(
        type="partial", text=text, start=start, duration=duration, is_final=False
    )


def build(clock: FakeClock, **overrides: float) -> UtteranceAssembler:
    """An assembler with fast, deterministic thresholds for tests."""
    config = AssemblerConfig(
        final_punctuation_grace_s=overrides.get("final_punctuation_grace_s", 0.30),
        final_pause_grace_s=overrides.get("final_pause_grace_s", 1.25),
        connector_wait_s=overrides.get("connector_wait_s", 3.0),
        partial_only_grace_s=overrides.get("partial_only_grace_s", 1.4),
        silence_confirm_s=overrides.get("silence_confirm_s", 1.1),
        late_segment_tolerance_s=overrides.get("late_segment_tolerance_s", 2.5),
        max_utterance_s=overrides.get("max_utterance_s", 20.0),
    )
    return UtteranceAssembler(config, clock=clock)


# -- fragmentation ---------------------------------------------------------


def test_one_sentence_split_by_provider_pauses_is_one_turn():
    """Segments of a sentence with natural pauses assemble into one turn."""
    clock = FakeClock()
    assembler = build(clock)

    assembler.on_caller_speech()
    assembler.on_partial(partial("Hey."))
    clock.advance(700)
    assembler.on_caller_speech()

    assert (
        assembler.on_final(
            final("Hey. I want to understand", start=0.0, duration=1.2), now=clock()
        )
        is None
    )
    clock.advance(400)
    # A short pause with no audio energy: the caller is thinking, not finished.
    assembler.on_caller_speech()
    assert (
        assembler.on_final(
            final("how artificial intelligence agents work", start=1.4, duration=2.1),
            now=clock(),
        )
        is None
    )
    clock.advance(500)
    assembler.on_caller_speech()
    assert (
        assembler.on_final(
            final(
                "and how they can interact with external tools.",
                start=3.2,
                duration=2.4,
                speech_final=True,
            ),
            now=clock(),
        )
        is None
    )

    clock.advance(350)
    turn = assembler.poll(now=clock())

    assert turn is not None, "the completed sentence must produce exactly one turn"
    assert "Hey. I want to understand" in turn.text
    assert "how artificial intelligence agents work" in turn.text
    assert "and how they can interact with external tools." in turn.text
    assert turn.segment_count == 3
    assert turn.reason == "speech_final"
    assert assembler.poll(now=clock()) is None


def test_natural_pause_before_punctuated_final_does_not_emit_a_turn_early():
    """A pause shorter than the grace window never answers a fragment."""
    clock = FakeClock()
    assembler = build(clock)
    assembler.on_caller_speech()
    assembler.on_final(final("Can you tell me", start=0.0, duration=1.0), now=clock())

    # 0.9 s of silence, then the caller continues.
    clock.advance(900)
    assert assembler.poll(now=clock()) is None
    assembler.on_caller_speech()
    assembler.on_final(
        final("when the clinic opens?", start=1.4, duration=1.1, speech_final=True),
        now=clock(),
    )

    clock.advance(320)
    turn = assembler.poll(now=clock())
    assert turn is not None
    assert turn.text == "Can you tell me when the clinic opens?"


def test_a_punctuated_finalize_result_completes_the_utterance():
    """A final the provider committed on request ends a punctuated sentence."""
    clock = FakeClock()
    assembler = build(clock)
    assembler.on_caller_speech()
    assembler.on_final(
        final("I would like to book an appointment on Friday", start=0.0, duration=2.2),
        now=clock(),
    )
    clock.advance(700)
    committed = final(
        "I would like to book an appointment on Friday.",
        start=0.0,
        duration=2.4,
        from_finalize=True,
    )
    assert assembler.on_final(committed, now=clock()) is None
    clock.advance(340)
    turn = assembler.poll(now=clock())
    assert turn is not None
    assert turn.reason == "speech_final"
    assert turn.text == "I would like to book an appointment on Friday."


def test_utterance_end_finishes_the_turn_without_waiting_for_a_long_deadline():
    """The provider's UtteranceEnd is the authoritative end-of-turn signal."""
    clock = FakeClock()
    assembler = build(clock)
    assembler.on_caller_speech()
    assembler.on_final(
        final("I need to speak to the reception team", start=0.0, duration=2.2),
        now=clock(),
    )

    clock.advance(1000)
    turn = assembler.on_utterance_end(now=clock())
    assert turn is not None
    assert turn.reason == "utterance_end"
    assert turn.text == "I need to speak to the reception team"


def test_final_stopping_on_a_connector_waits_much_longer():
    """A provider final that stops on "at" is not the end of the utterance."""
    clock = FakeClock()
    assembler = build(clock)
    assembler.on_caller_speech()
    assert (
        assembler.on_final(
            final("I'd like an appointment on Friday at", start=0.0, duration=2.0),
            now=clock(),
        )
        is None
    )

    clock.advance(1400)
    assert assembler.poll(now=clock()) is None, "a connector must not flush the fragment"

    assembler.on_caller_speech()
    turn = assembler.on_final(
        final("three in the afternoon.", start=2.4, duration=1.4, speech_final=True),
        now=clock(),
    )
    clock.advance(320)
    turn = turn or assembler.poll(now=clock())

    assert turn is not None
    assert turn.text == "I'd like an appointment on Friday at three in the afternoon."


# -- duplicate and delayed events -----------------------------------------


def test_duplicate_final_does_not_duplicate_text_or_open_a_second_turn():
    """The same segment delivered twice is assembled once."""
    clock = FakeClock()
    assembler = build(clock)
    assembler.on_caller_speech()
    segment = final("What are your opening hours?", start=0.0, duration=2.0, speech_final=True)

    assembler.on_final(segment, now=clock())
    assembler.on_final(segment, now=clock())
    clock.advance(320)
    turn = assembler.poll(now=clock())

    assert turn is not None
    assert turn.text == "What are your opening hours?"
    assert assembler.segments_duplicate == 1
    assert assembler.poll(now=clock()) is None


def test_finalize_revision_after_a_flush_does_not_reopen_the_turn():
    """A delayed revision of a finalized segment must not become a new turn."""
    clock = FakeClock()
    assembler = build(clock)
    assembler.on_caller_speech()
    assembler.on_final(
        final("I need a repeat prescription.", start=0.0, duration=2.4, speech_final=True),
        now=clock(),
    )
    clock.advance(320)
    turn = assembler.poll(now=clock())
    assert turn is not None

    clock.advance(200)
    late = final(
        "I need a repeat prescription.",
        start=0.0,
        duration=2.6,
        from_finalize=True,
    )
    assert assembler.on_final(late, now=clock()) is None
    assert not assembler.has_open_utterance
    assert assembler.segments_late == 1
    # Nothing is answered a second time.
    clock.advance(5000)
    assert assembler.poll(now=clock()) is None


def test_a_provider_tail_after_the_flush_is_not_a_second_turn():
    """A late segment for audio already answered must not trigger a reply."""
    clock = FakeClock()
    assembler = build(clock)
    assembler.on_caller_speech()
    assembler.on_final(
        final(
            "how they can interact with external.",
            start=0.0,
            duration=6.0,
            speech_final=True,
        ),
        now=clock(),
    )
    clock.advance(350)
    assert assembler.poll(now=clock()) is not None

    # Deepgram commits the trailing word after the turn was answered; the
    # caller has been silent the whole time.
    clock.advance(200)
    tail = final("tools.", start=5.6, duration=0.6, from_finalize=True)
    assert assembler.on_final(tail, now=clock()) is None
    assert assembler.segments_late == 1
    clock.advance(4000)
    assert assembler.poll(now=clock()) is None


def test_new_speech_after_a_flush_still_opens_a_new_turn():
    """Late-event filtering must not swallow the caller's next sentence."""
    clock = FakeClock()
    assembler = build(clock)
    assembler.on_caller_speech()
    assembler.on_final(
        final("Thanks, that's all.", start=0.0, duration=1.4, speech_final=True),
        now=clock(),
    )
    clock.advance(320)
    assert assembler.poll(now=clock()) is not None

    clock.advance(600)
    assembler.on_caller_speech()
    assembler.on_partial(partial("One more thing", start=2.4, duration=0.6))
    clock.advance(400)
    assembler.on_caller_speech()
    assembler.on_final(
        final("One more thing, what time do you close?", start=2.4, duration=2.0, speech_final=True),
        now=clock(),
    )
    clock.advance(320)
    turn = assembler.poll(now=clock())

    assert turn is not None
    assert turn.text == "One more thing, what time do you close?"


# -- partials --------------------------------------------------------------


def test_partials_never_produce_a_turn_while_the_caller_is_speaking():
    """Interim results update the UI but must not trigger an answer."""
    clock = FakeClock()
    assembler = build(clock)
    for index, text in enumerate(
        ["Hey", "Hey I want", "Hey I want to understand how agents", FULL]
    ):
        assembler.on_caller_speech()
        assert assembler.on_partial(partial(text, start=0.0, duration=1.0)) is None
        assert assembler.poll(now=clock()) is None
        clock.advance(300)
        del index

    assert assembler.turns_finalized == 0


def test_partial_only_fallback_fires_once_after_a_real_silence():
    """A provider that never commits a final still gets its utterance answered."""
    clock = FakeClock()
    assembler = build(clock)
    assembler.on_caller_speech()
    assembler.on_partial(partial("I would like to cancel my appointment", start=0.0))
    clock.advance(800)

    assert assembler.poll(now=clock()) is None
    assert assembler.take_finalize_request(now=clock()) is True
    assert assembler.take_finalize_request(now=clock()) is False

    clock.advance(700)
    turn = assembler.poll(now=clock())
    assert turn is not None
    assert turn.partial_only is True
    assert turn.text == "I would like to cancel my appointment"
    assert assembler.partial_only_turns == 1


def test_provider_is_asked_to_commit_after_each_confirmed_silence():
    """A slow provider is asked to flush once per silence, not once per turn."""
    clock = FakeClock()
    assembler = build(clock)
    assembler.on_caller_speech()
    assembler.on_final(final("I need an appointment on Friday", start=0.0, duration=2.0), now=clock())

    clock.advance(400)
    assert assembler.take_finalize_request(now=clock()) is False
    clock.advance(250)
    assert assembler.take_finalize_request(now=clock()) is True
    assert assembler.take_finalize_request(now=clock()) is False

    # The caller resumes (a mid-sentence pause) and stops again: the provider
    # gets another request for the new silence.
    clock.advance(200)
    assembler.on_caller_speech()
    clock.advance(700)
    assert assembler.take_finalize_request(now=clock()) is True


def test_final_absorbs_a_cumulative_partial_continuation():
    """The provider's longer hypothesis supplies the words after the final."""
    clock = FakeClock()
    assembler = build(clock)
    assembler.on_caller_speech()
    assembler.on_final(final("My date of birth is", start=0.0, duration=1.2), now=clock())
    assembler.on_caller_speech()
    assembler.on_partial(
        partial("My date of birth is the twelfth of May", start=0.0, duration=2.4)
    )
    clock.advance(1400)
    turn = assembler.poll(now=clock())

    assert turn is not None
    assert turn.text == "My date of birth is the twelfth of May"


def test_a_partial_from_other_audio_is_not_spliced_into_the_turn():
    """A late hypothesis for already-committed audio must not duplicate words."""
    clock = FakeClock()
    assembler = build(clock)
    assembler.on_caller_speech()
    assembler.on_final(
        final("how they can interact with tools.", start=1.5, duration=4.0, speech_final=True),
        now=clock(),
    )
    assembler.on_partial(partial("external tools.", start=5.2, duration=0.3))
    clock.advance(320)
    turn = assembler.poll(now=clock())

    assert turn is not None
    assert turn.text == "how they can interact with tools."


def test_segments_sharing_a_word_are_concatenated_not_truncated():
    """A shared word between two segments must not drop earlier words."""
    clock = FakeClock()
    assembler = build(clock)
    assembler.on_caller_speech()
    assembler.on_final(
        final("Hey. I want to understand", start=0.0, duration=1.2), now=clock()
    )
    assembler.on_final(
        final(
            "how artificial intelligence agents work.",
            start=1.3,
            duration=1.5,
            speech_final=True,
        ),
        now=clock(),
    )
    clock.advance(320)
    turn = assembler.poll(now=clock())

    assert turn is not None
    assert turn.text == "Hey. I want to understand how artificial intelligence agents work."


def test_a_finalize_revision_of_the_same_segment_replaces_it():
    """A longer revision at the same offset replaces the shorter segment."""
    clock = FakeClock()
    assembler = build(clock)
    assembler.on_caller_speech()
    assembler.on_final(
        final("I need a repeat prescription", start=0.0, duration=1.8), now=clock()
    )
    assembler.on_final(
        final(
            "I need a repeat prescription for my inhaler.",
            start=0.0,
            duration=2.6,
            speech_final=True,
            from_finalize=True,
        ),
        now=clock(),
    )
    clock.advance(320)
    turn = assembler.poll(now=clock())

    assert turn is not None
    assert turn.text == "I need a repeat prescription for my inhaler."


# -- ledger ----------------------------------------------------------------


def turn(text: str, source: str = "deepgram-stream") -> CallerTurn:
    """Build a caller turn without timings."""
    return CallerTurn(text=text, source=source)


def test_ledger_rejects_a_duplicate_batch_delivery():
    """The batch fallback must not answer an utterance the stream delivered."""
    ledger = TurnLedger()
    first = ledger.register(turn("I need to book an appointment", source="deepgram-batch"))
    assert first is not None
    assert ledger.register(turn("I need to book an appointment", source="deepgram-batch")) is None
    # A truncated batch transcription of the same audio is a duplicate too.
    assert ledger.register(turn("I need to book", source="deepgram-batch")) is None
    assert ledger.duplicates == 2


def test_ledger_pending_overflow_merges_instead_of_dropping_callers_words():
    """Rapid consecutive turns keep their content and their order."""
    ledger = TurnLedger(max_pending=2)
    texts = ["first question", "second question", "third question"]
    for text in texts:
        registered = ledger.register(turn(text))
        assert registered is not None
        ledger.enqueue(registered)

    assert ledger.pending == 2
    assert ledger.merged == 1

    async def drain() -> list[str]:
        collected = []
        while True:
            item = await ledger.next_turn(timeout=0.01)
            if item is None:
                return collected
            collected.append(item.text)

    drained = asyncio.run(drain())
    assert "first question" in drained[0]
    assert drained[-1] == "third question"
    assert sum(text.count("question") for text in drained) == 3


def test_generation_invalidates_an_in_flight_response():
    """A barge-in cannot be outrun by a response that was already generating."""
    ledger = TurnLedger()
    first = ledger.register(turn("book me in for Friday"))
    assert first is not None
    generation = ledger.begin_response(first)
    assert ledger.is_current(first.turn_id, generation)

    ledger.supersede()
    assert not ledger.is_current(first.turn_id, generation)
    assert not ledger.complete_response(first, generation)

    second = ledger.register(turn("actually make it Monday"))
    assert second is not None
    new_generation = ledger.begin_response(second)
    assert new_generation != generation
    assert ledger.is_current(second.turn_id, new_generation)
    assert ledger.superseded == 1


def test_ledger_next_turn_waits_briefly_then_times_out():
    """The worker must not spin while there is nothing to answer."""
    ledger = TurnLedger()

    async def wait() -> CallerTurn | None:
        return await ledger.next_turn(timeout=0.02)

    assert asyncio.run(wait()) is None


# -- sentence chunking -----------------------------------------------------


def test_sentence_chunker_emits_the_first_sentence_before_the_rest_arrives():
    """Streamed model text is speakable as soon as one sentence exists."""
    chunker = SentenceChunker(min_chars=8, max_chars=120, first_chunk_chars=60)
    assert chunker.feed("The clinic is open ") == []
    chunker._buffer += "from eight"  # noqa: SLF001 - simulating a mid-word delta
    ready = chunker.feed(" until six on weekdays. We also ")
    assert ready == ["The clinic is open from eight until six on weekdays."]
    assert chunker.feed("open on Saturday mornings.") == []
    assert chunker.flush() == "We also open on Saturday mornings."


def test_sentence_chunker_splits_a_long_first_clause():
    """A long opening sentence starts speaking at its first clause boundary."""
    chunker = SentenceChunker(min_chars=10, max_chars=200, first_chunk_chars=40)
    ready = chunker.feed("I can certainly help you book that appointment, but first I need")
    assert ready, "a long first clause should start playback"
    assert ready[0].endswith(",")


def test_sentence_chunker_does_not_split_abbreviations():
    """Titles and initials are not sentence boundaries."""
    chunker = SentenceChunker(min_chars=5, max_chars=200)
    chunker.feed("You are booked with Dr. Sharma at 3")
    ready = chunker.feed(".30 tomorrow. Anything else?")
    assert ready == ["You are booked with Dr. Sharma at 3.30 tomorrow."]


def test_sentence_chunker_never_loses_text():
    """Every character fed in is spoken exactly once."""
    chunker = SentenceChunker()
    text = (
        "Hello Sam. The practice is open until six, and the nurse will call you "
        "back this afternoon. Is there anything else I can help with today?"
    )
    chunks = list(chunker.chunks(text))
    assert " ".join(chunks) == text
    # A very short opener is merged into the next sentence, so this text is two
    # speakable chunks rather than three.
    assert len(chunks) == 2
    assert chunks[0].startswith("Hello Sam.")


# -- helpers ---------------------------------------------------------------


def test_punctuation_and_connector_helpers():
    """The end-of-utterance helpers classify speech the way the assembler needs."""
    assert has_terminal_punctuation("How can I help?")
    assert not has_terminal_punctuation("How can I help")
    assert ends_with_connector("I would like to book an appointment at")
    assert ends_with_connector("and")
    assert not ends_with_connector("tomorrow")


def test_assembler_config_reads_environment_overrides():
    """Deployments tune endpointing without editing code."""
    config = assembler_config_from_env(
        {
            "EMMA_TURN_FINAL_PAUSE_GRACE_MS": "1500",
            "EMMA_TURN_CONNECTOR_WAIT_MS": "2500",
        }
    )
    assert config.final_pause_grace_s == pytest.approx(1.5)
    assert config.connector_wait_s == pytest.approx(2.5)
    assert config.silence_confirm_s == AssemblerConfig().silence_confirm_s

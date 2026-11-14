#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Turn assembly and conversation lifecycle for the real-time voice loop.

The speech providers do not deliver sentences. Deepgram delivers ``Results``
segments: interim (partial) hypotheses while the caller is still speaking, and
``is_final`` segments whenever its endpointing sees a pause. A single spoken
sentence therefore arrives as several finals, and ``speech_final`` may fire on a
natural mid-sentence pause. Treating each provider final as a caller message is
what makes an assistant answer the first half of a sentence, then the second.

This module is the layer that turns that event stream into conversation turns:

* :class:`UtteranceAssembler` accumulates segments that belong to one utterance
  and only finalizes when there is an end-of-utterance signal: a
  ``speech_final`` segment with terminal punctuation, a provider
  ``UtteranceEnd``, or a caller-silence deadline reported by the audio loop.
  It is idempotent with respect to duplicate, reordered and delayed segments.
* :class:`TurnLedger` owns turn identity and the response lifecycle: one
  logical response per finalized turn, bounded pending backlog that merges
  rather than drops, and a generation counter a stale response can be checked
  against before it speaks.
* :class:`SentenceChunker` splits streamed model output into speakable
  sentences so synthesis can start before the model has finished.

Nothing here performs I/O, which keeps the state machines unit-testable without
a room, a provider key or a clock.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace

from emma.services.streaming_stt import TranscriptEvent

__all__ = [
    "AssemblerConfig",
    "CallerTurn",
    "SentenceChunker",
    "TurnLedger",
    "UtteranceAssembler",
    "assembler_config_from_env",
    "has_terminal_punctuation",
    "ends_with_connector",
]

# Words that cannot end a thought: if a provider final stops here, it cut the
# sentence, not the caller. Kept deliberately small — a false positive only
# delays an answer, while a missing entry splits a turn.
_INCOMPLETE_ENDINGS = frozenset(
    {
        "a",
        "about",
        "after",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "because",
        "before",
        "but",
        "by",
        "can",
        "could",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "like",
        "me",
        "my",
        "of",
        "on",
        "or",
        "our",
        "please",
        "so",
        "some",
        "than",
        "that",
        "the",
        "their",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)

_TERMINAL = (".", "?", "!", "…")
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
# Abbreviations that must not be read as a sentence boundary, plus initials.
_ABBREVIATIONS = frozenset(
    {"dr", "mr", "mrs", "ms", "prof", "st", "vs", "etc", "e.g", "i.e", "no", "approx"}
)


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace."""
    return " ".join(re.sub(r"[^a-z0-9'\s]", " ", text.lower()).split())


def has_terminal_punctuation(text: str) -> bool:
    """Whether ``text`` ends with a sentence-ending mark.

    Args:
        text: Transcript or model output.

    Returns:
        True when the text ends with ``.``, ``?``, ``!`` or an ellipsis.
    """
    return text.rstrip().endswith(_TERMINAL)


def ends_with_connector(text: str) -> bool:
    """Whether ``text`` stops on a word that cannot end a thought.

    Args:
        text: Transcript segment.

    Returns:
        True when the last word is a connector or determiner.
    """
    words = text.lower().replace(",", " ").split()
    if not words:
        return False
    last = words[-1].strip(".!?…")
    return last in _INCOMPLETE_ENDINGS


def _append_segment(existing: str, incoming: str) -> str:
    """Append one provider segment, dropping only genuinely repeated words.

    Segments are concatenated in speaking order. Word-level overlap removal
    handles a provider that re-emits the tail of the previous segment (after
    ``Finalize``, for instance). It deliberately never replaces text in the
    middle of the assembled transcript: two segments that happen to share a
    common word ("...I want to understand" followed by "how agents work") are
    consecutive, not a revision of each other.

    Args:
        existing: Transcript assembled so far.
        incoming: Newly transcribed segment.

    Returns:
        The merged transcript.
    """
    left = existing.split()
    right = incoming.split()
    if not left:
        return " ".join(right)
    if not right:
        return " ".join(left)
    left_norm = [_normalize(word) for word in left]
    right_norm = [_normalize(word) for word in right]
    if right_norm == left_norm[-len(right_norm) :]:
        return " ".join(left)
    compact_left = "".join(left_norm)
    compact_right = "".join(right_norm)
    if compact_right and compact_right in compact_left:
        return " ".join(left)
    overlap = 0
    for size in range(min(len(left_norm), len(right_norm)), 0, -1):
        if left_norm[-size:] == right_norm[:size]:
            overlap = size
            break
    return " ".join(left + right[overlap:])


def _is_superset(partial: str, committed: str) -> bool:
    """Whether ``partial`` restates ``committed`` and adds words at the end.

    Deepgram interim results are a cumulative hypothesis for the current
    utterance, so a partial that begins with the committed words is a revision
    of it; one that does not is a hypothesis for different audio and must not be
    spliced onto this turn.

    Args:
        partial: Latest interim hypothesis.
        committed: Transcript assembled from final segments.

    Returns:
        True when the partial is a longer restatement of the committed text.
    """
    partial_words = _normalize(partial).split()
    committed_words = _normalize(committed).split()
    if not partial_words or not committed_words:
        return False
    return (
        len(partial_words) > len(committed_words)
        and partial_words[: len(committed_words)] == committed_words
    )


@dataclass(frozen=True)
class AssemblerConfig:
    """Timing and content thresholds for end-of-utterance detection.

    Attributes:
        final_punctuation_grace_s: Caller silence required before answering a
            punctuated ``speech_final``. The provider has already applied its
            own endpointing; this window covers a straggler segment that
            belongs to the same sentence.
        final_pause_grace_s: Caller silence required when the committed text
            has no terminal punctuation. A pivot turn is a short pause; this is
            long enough not to answer mid-thought.
        connector_wait_s: Wait after a segment that stops on a connector, which
            is the provider cutting a sentence rather than the caller pausing.
        partial_only_grace_s: Silence to accept before using a partial as the
            transcript when the provider never returns a final.
        silence_confirm_s: Caller energy silence required before any turn is
            finalized. The audio loop's energy detector is independent of the
            provider's VAD, so both have to agree that speech stopped.
        finalize_on_silence_s: Caller silence after which the provider is asked
            to commit the utterance (``Finalize``). The provider's own
            endpointing can lag by seconds on a slow link; asking it to commit
            bounds how long the caller waits for a final transcript.
        late_segment_tolerance_s: Window in which a segment that predates a
            finalized turn is treated as delayed rather than new.
        max_utterance_s: Hard ceiling on an open utterance, so a provider that
            never endpoints cannot strand a caller forever.
    """

    final_punctuation_grace_s: float = 0.45
    final_pause_grace_s: float = 1.25
    connector_wait_s: float = 3.0
    partial_only_grace_s: float = 1.4
    silence_confirm_s: float = 1.1
    finalize_on_silence_s: float = 0.6
    late_segment_tolerance_s: float = 2.5
    max_utterance_s: float = 20.0


def _env_float(name: str, default: float, environ: dict[str, str] | None = None) -> float:
    """Read a float override from the environment, keeping the default on error."""
    import os

    raw = (environ if environ is not None else os.environ).get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def assembler_config_from_env(environ: dict[str, str] | None = None) -> AssemblerConfig:
    """Build :class:`AssemblerConfig` from ``EMMA_TURN_*`` environment values.

    Args:
        environ: Environment mapping; defaults to ``os.environ``.

    Returns:
        A configuration with any explicit overrides applied.
    """
    base = AssemblerConfig()
    return AssemblerConfig(
        final_punctuation_grace_s=_env_float(
            "EMMA_TURN_FINAL_PUNCTUATION_GRACE_MS",
            base.final_punctuation_grace_s * 1000.0,
            environ,
        )
        / 1000.0,
        final_pause_grace_s=_env_float(
            "EMMA_TURN_FINAL_PAUSE_GRACE_MS", base.final_pause_grace_s * 1000.0, environ
        )
        / 1000.0,
        connector_wait_s=_env_float(
            "EMMA_TURN_CONNECTOR_WAIT_MS", base.connector_wait_s * 1000.0, environ
        )
        / 1000.0,
        partial_only_grace_s=_env_float(
            "EMMA_TURN_PARTIAL_ONLY_GRACE_MS", base.partial_only_grace_s * 1000.0, environ
        )
        / 1000.0,
        silence_confirm_s=_env_float(
            "EMMA_TURN_SILENCE_CONFIRM_MS", base.silence_confirm_s * 1000.0, environ
        )
        / 1000.0,
        finalize_on_silence_s=_env_float(
            "EMMA_TURN_FINALIZE_ON_SILENCE_MS",
            base.finalize_on_silence_s * 1000.0,
            environ,
        )
        / 1000.0,
        late_segment_tolerance_s=_env_float(
            "EMMA_TURN_LATE_SEGMENT_TOLERANCE_MS",
            base.late_segment_tolerance_s * 1000.0,
            environ,
        )
        / 1000.0,
        max_utterance_s=_env_float(
            "EMMA_TURN_MAX_UTTERANCE_MS", base.max_utterance_s * 1000.0, environ
        )
        / 1000.0,
    )


@dataclass
class CallerTurn:
    """One completed caller utterance, ready for the model.

    Attributes:
        turn_id: Identifier assigned by :class:`TurnLedger`. Zero until then.
        text: Assembled transcript, in speaking order.
        source: Which STT path produced it (``deepgram-stream``, ``batch``...).
        reason: Why the turn was finalized: ``speech_final``, ``utterance_end``,
            ``quiet``, ``partial`` or ``batch``.
        speech_end_at: Monotonic time the caller stopped speaking, as observed
            by the audio loop. Zero when unknown.
        created_at: Monotonic time the turn was finalized.
        segment_count: Number of provider segments assembled into the turn.
        partial_only: True when no final segment was ever received.
    """

    text: str
    source: str = "deepgram-stream"
    reason: str = "quiet"
    speech_end_at: float = 0.0
    created_at: float = 0.0
    turn_id: int = 0
    segment_count: int = 0
    partial_only: bool = False

    def latency_ms(self) -> float:
        """Milliseconds between the caller falling silent and finalization."""
        if not self.speech_end_at or not self.created_at:
            return 0.0
        return max(0.0, (self.created_at - self.speech_end_at) * 1000.0)

    def __post_init__(self) -> None:
        self.text = " ".join((self.text or "").split())


@dataclass
class _OpenUtterance:
    """Segments accumulated for the utterance currently being spoken."""

    segments: list[TranscriptEvent] = field(default_factory=list)
    text: str = ""
    partial: str = ""
    started_at: float = 0.0
    last_activity_at: float = 0.0
    last_final_at: float = 0.0
    speech_final: bool = False
    terminal: bool = False
    force_finalize_at: float = 0.0
    start_offset: float = 0.0
    end_offset: float = 0.0
    last_final_end: float = 0.0
    seen: set[tuple[str, int, int]] = field(default_factory=set)


class UtteranceAssembler:
    """Accumulate provider segments into one caller turn.

    Args:
        config: End-of-utterance thresholds.
        clock: Monotonic clock; injectable for tests.
    """

    def __init__(
        self,
        config: AssemblerConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Store the configuration and start with no open utterance."""
        self._config = config or AssemblerConfig()
        self._clock = clock
        self._open: _OpenUtterance | None = None
        self._last_caller_speech_at = 0.0
        self._last_flush_at = 0.0
        self._last_flush_start = 0.0
        self._stream_end_offset = 0.0
        self._last_flushed_text = ""
        self._turn_seq = 0
        self._finalize_asked = False
        # Counters, surfaced in the agent's latency log line.
        self.segments_received = 0
        self.segments_duplicate = 0
        self.segments_late = 0
        self.turns_finalized = 0
        self.partial_only_turns = 0

    @property
    def config(self) -> AssemblerConfig:
        """The active configuration."""
        return self._config

    @property
    def open_text(self) -> str:
        """Transcript assembled so far for the open utterance."""
        if self._open is None:
            return ""
        return self._assembled(self._open)

    def _assembled(self, utterance: _OpenUtterance) -> str:
        """Rebuild the utterance transcript from its committed segments.

        Segments are ordered by their stream offset, not by arrival: Deepgram
        can deliver two committed segments in one frame, and a revision after
        ``Finalize`` reuses the offset of the segment it replaces.

        Args:
            utterance: The open utterance.

        Returns:
            The transcript to keep so far.
        """
        ordered = sorted(utterance.segments, key=lambda event: (event.start, event.sequence))
        committed = ""
        for event in ordered:
            committed = _append_segment(committed, event.text)
        partial = utterance.partial.strip()
        if not committed:
            return partial
        if partial and _is_superset(partial, committed):
            # The provider's latest cumulative hypothesis is ahead of the
            # committed segments: keep it so the tail is not lost.
            return partial
        return committed

    @property
    def has_open_utterance(self) -> bool:
        """Whether an utterance is currently accumulating."""
        return self._open is not None

    def _now(self, now: float | None) -> float:
        return self._clock() if now is None else now

    # -- caller audio signals ---------------------------------------------

    def on_caller_speech(self, *, now: float | None = None) -> None:
        """Record that the audio loop measured caller speech energy."""
        moment = self._now(now)
        if moment - self._last_caller_speech_at > 0.3:
            # A new speech onset: the utterance may need another Finalize once
            # it stops again.
            self._finalize_asked = False
        self._last_caller_speech_at = moment

    def speech_end_at(self, *, now: float | None = None) -> float:
        """Best estimate of when the caller last spoke.

        Args:
            now: Monotonic time to use.

        Returns:
            The last speech-energy timestamp, or ``now`` when none was seen.
        """
        if self._last_caller_speech_at:
            return self._last_caller_speech_at
        return self._now(now)

    # -- transcription events ---------------------------------------------

    def _is_stale(self, event: TranscriptEvent, now: float) -> bool:
        """Whether ``event`` belongs to a turn that has already been finalized.

        A provider segment's ``duration`` includes the endpointing silence, so
        the end offset alone cannot prove a segment is old: a caller who
        resumes immediately can produce a new segment that starts before the
        previous turn's end offset. Two narrower rules are used instead: a
        segment whose text is already contained in the finalized transcript is
        a repeat (Deepgram re-sends the last segment after ``Finalize``), and a
        segment that starts before the finalized turn started cannot be new
        speech.

        Args:
            event: Incoming provider segment.
            now: Monotonic time of arrival.

        Returns:
            True when the segment must be dropped instead of opening a turn.
        """
        if not self._last_flush_at:
            return False
        if now - self._last_flush_at > self._config.late_segment_tolerance_s:
            return False
        if event.start > 0.0 and event.start + 0.05 < self._last_flush_start:
            return True
        normalized = _normalize(event.text)
        if len(normalized) < 4 or not self._last_flushed_text:
            return False
        if normalized in _normalize(self._last_flushed_text):
            return True
        # A segment for audio inside the turn that was just answered, arriving
        # while the caller is silent, is that turn's provider tail (the words
        # Deepgram committed late). Answering it would be a second reply to one
        # utterance; the caller's own 2.5 s of silence is the discriminator.
        silent_since_flush = now - self._last_caller_speech_at > 0.25
        if (
            silent_since_flush
            and event.start > 0.0
            and event.start + 0.05 < self._stream_end_offset
        ):
            return True
        return False

    def _open_utterance(self, event: TranscriptEvent, now: float) -> _OpenUtterance:
        self._open = _OpenUtterance(
            started_at=now,
            last_activity_at=now,
            start_offset=max(0.0, event.start),
            end_offset=max(0.0, event.start + event.duration),
        )
        self._finalize_asked = False
        return self._open

    def on_partial(
        self, event: TranscriptEvent, *, now: float | None = None
    ) -> CallerTurn | None:
        """Handle an interim transcript.

        A partial never finalizes a turn: it only records that the caller is
        still speaking and extends the deadline of the utterance it belongs to.

        Args:
            event: Interim hypothesis from the provider.
            now: Monotonic arrival time.

        Returns:
            Always None; partials never produce a turn.
        """
        moment = self._now(now)
        text = event.text.strip()
        if not text:
            return None
        self.segments_received += 1
        if self._is_stale(event, moment):
            self.segments_late += 1
            return None
        utterance = self._open or self._open_utterance(event, moment)
        previous = utterance.partial
        if not utterance.segments:
            utterance.partial = text
        elif _is_superset(text, utterance.text):
            utterance.partial = text
        elif _is_superset(utterance.text, text):
            # A stale, shorter hypothesis: keep the fuller transcript.
            utterance.partial = previous
        elif (
            event.start >= utterance.last_final_end - 0.05
            and not _normalize(text).split()[:1] == _normalize(utterance.text).split()[-1:]
        ):
            # Audio that starts after the last committed segment is a genuine
            # continuation, even if the provider restarted its hypothesis.
            utterance.partial = f"{utterance.text} {text}".strip()
        utterance.last_activity_at = moment
        utterance.end_offset = max(utterance.end_offset, event.start + event.duration)
        return None

    def on_final(self, event: TranscriptEvent, *, now: float | None = None) -> CallerTurn | None:
        """Handle a provider final segment.

        Args:
            event: Final segment from the provider.
            now: Monotonic arrival time.

        Returns:
            A completed turn when this segment finished the utterance,
            otherwise None.
        """
        moment = self._now(now)
        text = event.text.strip()
        if not text:
            return None
        self.segments_received += 1
        if self._is_stale(event, moment):
            self.segments_late += 1
            return None

        utterance = self._open or self._open_utterance(event, moment)
        key = (_normalize(text), round(event.start * 100), round(event.duration * 100))
        if key in utterance.seen:
            self.segments_duplicate += 1
            return None
        utterance.seen.add(key)

        # A segment with the same start offset is a revision of one already in
        # the turn (Deepgram re-sends it after Finalize with more text), so it
        # replaces that segment instead of being appended.
        replaced = False
        for index, existing in enumerate(utterance.segments):
            if abs(existing.start - event.start) <= 0.2:
                if len(text.split()) >= len(existing.text.split()):
                    utterance.segments[index] = event
                replaced = True
                break
        if not replaced:
            utterance.segments.append(event)
        utterance.text = self._assembled(utterance)
        if utterance.partial and _normalize(utterance.partial) in _normalize(utterance.text):
            utterance.partial = ""
        utterance.last_activity_at = moment
        utterance.last_final_at = moment
        utterance.last_final_end = max(utterance.last_final_end, event.start + event.duration)
        utterance.start_offset = min(utterance.start_offset, max(0.0, event.start))
        utterance.end_offset = max(utterance.end_offset, event.start + event.duration)

        terminal = has_terminal_punctuation(text)
        if event.speech_final or (event.from_finalize and terminal):
            # A punctuated result we asked the provider to commit after
            # confirmed caller silence means the utterance is complete, even
            # when the provider did not flag it speech_final.
            utterance.speech_final = True
            utterance.terminal = terminal
        elif terminal and event.is_final:
            # Smart formatting punctuates the last segment of an utterance even
            # when the provider did not flag it speech_final.
            utterance.terminal = True

        if self._ready(utterance, moment):
            return self._flush(moment, reason="speech_final" if utterance.speech_final else "quiet")
        return None

    def on_utterance_end(self, *, now: float | None = None) -> CallerTurn | None:
        """Handle the provider's authoritative end-of-utterance event.

        Args:
            now: Monotonic arrival time.

        Returns:
            A completed turn, or None when nothing is open.
        """
        moment = self._now(now)
        utterance = self._open
        if utterance is None:
            return None
        utterance.speech_final = True
        utterance.last_activity_at = max(utterance.last_activity_at, moment)
        # UtteranceEnd fires after a full second of provider-observed silence,
        # so it is already stronger evidence than the local silence timer.
        return self._flush(moment, reason="utterance_end")

    # -- finalization ------------------------------------------------------

    def _quiet_for(self, now: float, utterance: _OpenUtterance) -> float:
        """Seconds since the audio loop last measured caller speech."""
        # The audio loop's energy signal is the authoritative speech-end clock:
        # it does not lag behind the caller the way an STT event does. When no
        # energy signal is available (batch fallback, unit tests) the last
        # transcription activity is the best substitute.
        last_signal = self._last_caller_speech_at or utterance.last_activity_at
        return max(0.0, now - last_signal)

    def _ready(self, utterance: _OpenUtterance, now: float) -> bool:
        """Whether an utterance may be finalized now.

        Every deadline is measured against the caller's *silence*, not against
        when the transcript arrived: a slow provider must not add its own lag
        on top of the endpointing window. A short settle after the newest
        event still applies, so a straggler segment can extend the same turn.

        Args:
            utterance: The open utterance.
            now: Monotonic time.

        Returns:
            True when the turn is complete and safe to answer.
        """
        config = self._config
        if now - utterance.started_at >= config.max_utterance_s:
            return True

        quiet = self._quiet_for(now, utterance)
        since_event = now - utterance.last_activity_at
        settle = min(0.25, config.final_punctuation_grace_s)
        if utterance.speech_final and utterance.terminal:
            return quiet >= config.final_punctuation_grace_s and since_event >= settle
        if utterance.speech_final:
            return quiet >= config.silence_confirm_s and since_event >= settle
        if utterance.segments:
            if ends_with_connector(self.open_text):
                return quiet >= config.connector_wait_s and since_event >= settle
            return quiet >= config.final_pause_grace_s and since_event >= settle
        # Partials only: the provider never committed this utterance.
        return quiet >= config.partial_only_grace_s and since_event >= settle

    def poll(self, *, now: float | None = None) -> CallerTurn | None:
        """Check deadlines and finalize the open utterance when it is complete.

        Args:
            now: Monotonic time.

        Returns:
            A completed turn, or None while the caller may still be speaking.
        """
        moment = self._now(now)
        utterance = self._open
        if utterance is None:
            return None
        if self._ready(utterance, moment):
            if not utterance.segments:
                reason = "partial"
            elif utterance.speech_final:
                reason = "speech_final"
            else:
                reason = "quiet"
            return self._flush(moment, reason=reason)
        return None

    def take_finalize_request(self, *, now: float | None = None) -> bool:
        """Whether the provider should be asked to commit the open utterance.

        Called once per caller silence. The provider's own endpointing may not
        fire for seconds on a slow link, leaving the last words uncommitted
        while the caller waits; an explicit ``Finalize`` after confirmed
        silence makes it commit what it already has. A new speech onset resets
        the request, so a mid-sentence pause can ask again.

        Args:
            now: Monotonic time.

        Returns:
            True at most once per silence.
        """
        moment = self._now(now)
        utterance = self._open
        if utterance is None or (not utterance.segments and not utterance.partial):
            return False
        if self._finalize_asked:
            return False
        if self._quiet_for(moment, utterance) < self._config.finalize_on_silence_s:
            return False
        self._finalize_asked = True
        return True

    def _flush(self, now: float, *, reason: str) -> CallerTurn | None:
        utterance = self._open
        self._open = None
        self._finalize_asked = False
        if utterance is None:
            return None
        text = self._assembled(utterance).strip()
        if not text:
            return None
        self._turn_seq += 1
        self.turns_finalized += 1
        partial_only = not utterance.segments
        if partial_only:
            self.partial_only_turns += 1
        self._last_flush_at = now
        self._last_flushed_text = text
        self._last_flush_start = utterance.start_offset
        self._stream_end_offset = max(self._stream_end_offset, utterance.end_offset)
        return CallerTurn(
            text=text,
            source="deepgram-stream",
            reason=reason,
            speech_end_at=self._last_caller_speech_at or utterance.last_activity_at,
            created_at=now,
            segment_count=len(utterance.segments),
            partial_only=partial_only,
        )

    def reset(self) -> None:
        """Drop the open utterance, keeping the late-segment watermarks."""
        self._open = None
        self._finalize_asked = False


class TurnLedger:
    """Turn identity, idempotency and response generations.

    The ledger is the single authority on which caller turn a response belongs
    to. A response is only allowed to speak while its generation is current:
    every barge-in or newer turn bumps the generation, so a stale model result
    can never play after the conversation has moved on.

    Args:
        max_pending: Maximum turns waiting for a response. Overflow is merged
            into the next pending turn instead of being dropped, so no caller
            content is lost.
        dedupe_window_s: Window in which an identical transcript is treated as
            a duplicate delivery from the other STT path.
        clock: Monotonic clock; injectable for tests.
    """

    def __init__(
        self,
        *,
        max_pending: int = 4,
        dedupe_window_s: float = 6.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create an empty ledger with generation zero."""
        if max_pending < 1:
            raise ValueError("max_pending must be at least 1")
        self._max_pending = max_pending
        self._dedupe_window_s = dedupe_window_s
        self._clock = clock
        self._pending: deque[CallerTurn] = deque()
        self._not_empty = asyncio.Event()
        self._recent: deque[tuple[str, float]] = deque(maxlen=16)
        self._next_id = 0
        self._generation = 0
        self._active: CallerTurn | None = None
        self._completed_turn_ids: set[int] = set()
        self.accepted = 0
        self.duplicates = 0
        self.merged = 0
        self.superseded = 0

    @property
    def generation(self) -> int:
        """Current response generation; bumps invalidate in-flight responses."""
        return self._generation

    @property
    def active_turn(self) -> CallerTurn | None:
        """The turn whose response is currently owned by the pipeline."""
        return self._active

    @property
    def pending(self) -> int:
        """Number of turns waiting for a response."""
        return len(self._pending)

    def register(self, turn: CallerTurn, *, text_dedupe: bool = True) -> CallerTurn | None:
        """Assign an id to ``turn`` and reject duplicate deliveries.

        Args:
            turn: Finalized caller turn.
            text_dedupe: Apply the cross-path text duplicate check. Streaming
                turns are already deduplicated by segment offsets, so the check
                is only needed for the batch fallback.

        Returns:
            The registered turn, or None when it is a duplicate or empty.
        """
        text = " ".join((turn.text or "").split())
        if not text:
            return None
        moment = self._clock()
        if text_dedupe and self._is_duplicate(text, moment):
            self.duplicates += 1
            return None
        self._next_id += 1
        registered = replace(turn, text=text, turn_id=self._next_id)
        self._recent.append((_normalize(text), moment))
        self.accepted += 1
        return registered

    def _is_duplicate(self, text: str, now: float) -> bool:
        normalized = _normalize(text)
        if not normalized:
            return True
        for previous, at in reversed(self._recent):
            if now - at > self._dedupe_window_s:
                break
            if previous == normalized:
                return True
            # A batch transcription of an utterance the streaming path already
            # delivered is a substring of it (batch drops early frames).
            if len(previous) >= 12 and (
                previous.startswith(normalized) or normalized.startswith(previous)
            ):
                return True
        return False

    def enqueue(self, turn: CallerTurn) -> None:
        """Queue a registered turn, merging rather than dropping on overflow.

        Args:
            turn: A turn returned by :meth:`register`.
        """
        if len(self._pending) >= self._max_pending:
            # Never drop a caller message: fold the oldest pending turn into the
            # next one so its content is still answered, in order.
            oldest = self._pending.popleft()
            next_turn = self._pending.popleft()
            self._pending.appendleft(
                replace(next_turn, text=f"{oldest.text} {next_turn.text}".strip())
            )
            self.merged += 1
        self._pending.append(turn)
        self._not_empty.set()

    async def next_turn(self, timeout: float = 0.25) -> CallerTurn | None:
        """Wait briefly for the next pending turn.

        Args:
            timeout: Maximum wait in seconds.

        Returns:
            The next turn in conversational order, or None on timeout.
        """
        if not self._pending:
            self._not_empty.clear()
            try:
                await asyncio.wait_for(self._not_empty.wait(), timeout=timeout)
            except TimeoutError:
                return None
        if not self._pending:
            return None
        return self._pending.popleft()

    def begin_response(self, turn: CallerTurn) -> int:
        """Claim the response lifecycle for ``turn``.

        Args:
            turn: Turn about to be answered.

        Returns:
            The generation the response must present before speaking.
        """
        self._active = turn
        return self._generation

    def is_current(self, turn_id: int, generation: int) -> bool:
        """Whether a response may still speak.

        Args:
            turn_id: Turn the response belongs to.
            generation: Generation captured by the response.

        Returns:
            True when no newer turn or interruption superseded it.
        """
        return (
            generation == self._generation
            and self._active is not None
            and self._active.turn_id == turn_id
            and turn_id not in self._completed_turn_ids
        )

    def complete_response(self, turn: CallerTurn, generation: int) -> bool:
        """Mark a turn's response as finished.

        Args:
            turn: Turn whose response completed.
            generation: Generation the response presented.

        Returns:
            True when the completion is still current.
        """
        if not self.is_current(turn.turn_id, generation):
            return False
        self._completed_turn_ids.add(turn.turn_id)
        self._active = None
        return True

    def supersede(self) -> int:
        """Invalidate the in-flight response and bump the generation.

        Returns:
            The new generation.
        """
        self._generation += 1
        if self._active is not None:
            self.superseded += 1
        self._active = None
        return self._generation

    def stats(self) -> dict[str, int]:
        """Counters for the per-turn latency log line."""
        return {
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "merged": self.merged,
            "superseded": self.superseded,
            "pending": len(self._pending),
        }


class SentenceChunker:
    """Split streamed model text into speakable sentences.

    Synthesis can start as soon as the first sentence exists, instead of
    waiting for the model's stop token. The first chunk is allowed to end at a
    clause boundary so the caller hears something sooner on long opening
    sentences.

    Args:
        min_chars: Fragments shorter than this are held for the next sentence.
        max_chars: Longest chunk handed to the synthesiser.
        first_chunk_chars: Clause-boundary threshold for the very first chunk.
    """

    def __init__(
        self,
        *,
        min_chars: int = 12,
        max_chars: int = 220,
        first_chunk_chars: int = 64,
    ) -> None:
        """Configure the chunking thresholds."""
        self._min_chars = min_chars
        self._max_chars = max_chars
        self._first_chunk_chars = first_chunk_chars
        self._buffer = ""
        self._emitted = 0

    @property
    def buffered(self) -> str:
        """Text received but not yet emitted."""
        return self._buffer

    def feed(self, delta: str) -> list[str]:
        """Add streamed text and return any sentences it completed.

        Args:
            delta: New text from the model stream.

        Returns:
            Zero or more complete, speakable sentences.
        """
        if not delta:
            return []
        self._buffer += delta
        ready: list[str] = []
        while True:
            cut = self._find_boundary()
            if cut <= 0:
                break
            chunk, self._buffer = self._buffer[:cut].strip(), self._buffer[cut:].lstrip()
            if not chunk:
                continue
            if len(chunk) < self._min_chars:
                # Too short to say on its own; keep it with the next sentence.
                self._buffer = f"{chunk} {self._buffer}".strip()
                break
            ready.append(chunk)
            self._emitted += 1
        return ready

    def _find_boundary(self) -> int:
        """Index just past the earliest acceptable sentence boundary."""
        for match in _SENTENCE_END.finditer(self._buffer):
            end = match.end() - len(match.group(0))
            word = self._buffer[:end].split()[-1] if self._buffer[:end].split() else ""
            bare = word.rstrip(".,!?…").lower()
            if bare in _ABBREVIATIONS or (len(bare) == 1 and bare.isalpha()):
                continue
            if end < self._min_chars:
                continue
            return match.end()
        limit = self._first_chunk_chars if self._emitted == 0 else self._max_chars
        if len(self._buffer) > limit:
            # Cut at a clause boundary once the buffer is long enough to be
            # worth speaking; the slack lets the boundary arrive just after the
            # threshold instead of waiting for the whole sentence.
            window = min(len(self._buffer), limit + 24)
            best = -1
            for mark in ("; ", ": ", ", "):
                position = self._buffer.find(mark, self._min_chars, window)
                if position >= self._min_chars and position < window:
                    best = position + 1 if best < 0 else min(best, position + 1)
            if best > 0:
                return best
            if len(self._buffer) > self._max_chars:
                space = self._buffer.rfind(" ", 0, self._max_chars)
                return space if space > self._min_chars else self._max_chars
        return 0

    def flush(self) -> str | None:
        """Return whatever is buffered as a final chunk.

        Returns:
            The remaining text, or None when nothing is left.
        """
        text = self._buffer.strip()
        self._buffer = ""
        if not text:
            return None
        self._emitted += 1
        return text

    def chunks(self, text: str) -> Iterator[str]:
        """Split ``text`` synchronously, for non-streaming callers."""
        ready = self.feed(text)
        tail = self.flush()
        yield from ready
        if tail:
            yield tail

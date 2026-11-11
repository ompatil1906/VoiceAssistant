#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Per-session spend accounting with hard ceilings.

The budget is enforced *before* work is sent to a provider, so an over-running
session stops instead of overspending. Ceilings default to values that cannot
bill anything: a session may use local providers freely, and remote free-tier
providers up to a configured number of requests.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace

from emma.cost.policy import QuotaExhausted


@dataclass(frozen=True)
class BudgetLimits:
    """Hard ceilings for one session.

    Parameters:
        max_requests: Provider requests allowed in the session, or ``None`` for
            unlimited.
        max_tokens: LLM tokens allowed in the session, or ``None``.
        max_characters: TTS characters allowed in the session, or ``None``.
        max_audio_seconds: Transcribed or synthesised audio allowed, or ``None``.
        max_usd: Spend ceiling in US dollars, or ``None``. Only meaningful for
            providers whose unit price is known; the ledger refuses to guess.
        max_minutes: Wall-clock session length, or ``None``.
    """

    max_requests: int | None = 200
    max_tokens: int | None = 100_000
    max_characters: int | None = 200_000
    max_audio_seconds: float | None = 1_800.0
    max_usd: float | None = 0.0
    max_minutes: float | None = 15.0


#: A budget that permits local work only: no request allowance at all.
LOCAL_ONLY_BUDGET_LIMITS = BudgetLimits(
    max_requests=0,
    max_tokens=0,
    max_characters=0,
    max_audio_seconds=None,
    max_usd=0.0,
    max_minutes=None,
)


class SessionBudget:
    """Thread-safe accounting for one conversation.

    The runner creates one budget per session; providers record usage against it
    and ask :meth:`reserve_request` before calling out.
    """

    def __init__(self, limits: BudgetLimits | None = None) -> None:
        """Initialize the budget.

        Args:
            limits: Ceilings to enforce; defaults to :class:`BudgetLimits`.
        """
        self._limits = limits or BudgetLimits()
        self._lock = threading.Lock()
        self._requests = 0
        self._tokens = 0
        self._characters = 0
        self._audio_seconds = 0.0
        self._usd = 0.0

    @property
    def limits(self) -> BudgetLimits:
        """The ceilings in force."""
        return self._limits

    @property
    def usage(self) -> dict[str, float]:
        """A snapshot of what the session has consumed."""
        with self._lock:
            return {
                "requests": self._requests,
                "tokens": self._tokens,
                "characters": self._characters,
                "audio_seconds": self._audio_seconds,
                "usd": self._usd,
            }

    def with_limits(self, limits: BudgetLimits) -> "SessionBudget":
        """Return a new budget with different ceilings and no usage."""
        return SessionBudget(limits)

    def tighten(self, **changes: object) -> "SessionBudget":
        """Return a copy of the budget with some ceilings replaced."""
        return SessionBudget(replace(self._limits, **changes))  # type: ignore[arg-type]

    def reserve_request(self) -> None:
        """Account for one provider request, or refuse it.

        Raises:
            QuotaExhausted: If the request allowance is already spent.
        """
        with self._lock:
            limit = self._limits.max_requests
            if limit is not None and self._requests >= limit:
                raise QuotaExhausted(
                    f"session request allowance exhausted ({limit} requests); "
                    "switch to a local provider or start a new session"
                )
            self._requests += 1

    def record_tokens(self, tokens: int) -> None:
        """Record LLM token usage.

        Raises:
            QuotaExhausted: If the token allowance is exceeded.
        """
        with self._lock:
            self._tokens += max(0, tokens)
            limit = self._limits.max_tokens
            if limit is not None and self._tokens > limit:
                raise QuotaExhausted(
                    f"session token allowance exceeded ({self._tokens}/{limit})"
                )

    def record_characters(self, characters: int) -> None:
        """Record TTS character usage.

        Raises:
            QuotaExhausted: If the character allowance is exceeded.
        """
        with self._lock:
            self._characters += max(0, characters)
            limit = self._limits.max_characters
            if limit is not None and self._characters > limit:
                raise QuotaExhausted(
                    f"session character allowance exceeded ({self._characters}/{limit})"
                )

    def record_audio_seconds(self, seconds: float) -> None:
        """Record audio seconds transcribed or synthesised.

        Raises:
            QuotaExhausted: If the audio allowance is exceeded.
        """
        with self._lock:
            self._audio_seconds += max(0.0, seconds)
            limit = self._limits.max_audio_seconds
            if limit is not None and self._audio_seconds > limit:
                raise QuotaExhausted(
                    f"session audio allowance exceeded "
                    f"({self._audio_seconds:.1f}s/{limit:.1f}s)"
                )

    def record_spend(self, usd: float, *, unit_price_known: bool) -> None:
        """Record spend against a provider with a known unit price.

        Args:
            usd: Amount spent, in US dollars.
            unit_price_known: Whether the provider's unit price is established.
                When it is not, recording spend is a programming error: an
                unknown price may not be converted into a number.

        Raises:
            ValueError: If ``unit_price_known`` is false.
            QuotaExhausted: If the spend ceiling is exceeded.
        """
        if not unit_price_known:
            raise ValueError("cannot record spend for a provider without a known unit price")
        with self._lock:
            self._usd += max(0.0, usd)
            limit = self._limits.max_usd
            if limit is not None and self._usd > limit:
                raise QuotaExhausted(f"session spend ceiling exceeded (${self._usd:.4f}/${limit:.2f})")

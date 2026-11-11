#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Usage ledger: what each session consumed, and from whom.

Records carry provider, operation and units. They never carry the conversation
itself: text is reduced to a length, and anything that could identify a caller
passes through :func:`emma.security.redaction.redact` before it is written.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from emma.security.redaction import redact


@dataclass(frozen=True)
class UsageRecord:
    """One metered operation.

    Parameters:
        provider: Provider identifier, e.g. ``"gemini"``.
        operation: What was done, e.g. ``"llm.completion"``.
        units: Amount consumed, in ``unit``.
        unit: One of ``request``, ``token``, ``character``, ``audio_second``.
        cost_class: The provider's cost class at the time of the call.
        session_id: Opaque session identifier; never a caller identity.
        timestamp: Unix time the record was created.
        detail: Optional short, non-identifying note.
    """

    provider: str
    operation: str
    units: float
    unit: str
    cost_class: str
    session_id: str
    timestamp: float = field(default_factory=time.time)
    detail: str = ""


def _sanitise(record: UsageRecord) -> UsageRecord:
    """Strip anything identifying from a record before it is persisted."""
    return UsageRecord(
        provider=redact(record.provider),
        operation=record.operation,
        units=record.units,
        unit=record.unit,
        cost_class=record.cost_class,
        session_id=record.session_id,
        timestamp=record.timestamp,
        detail=redact(record.detail),
    )


class UsageLedger:
    """Append-only JSONL ledger of metered operations.

    Example::

        ledger = UsageLedger(Path("logs/usage.jsonl"))
        ledger.append(UsageRecord("gemini", "llm.completion", 812, "token", "free_tier", "s-1"))
    """

    def __init__(
        self,
        path: Path | None = None,
        sink: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        """Initialize the ledger.

        Args:
            path: JSONL file to append to. ``None`` keeps records in memory only.
            sink: Optional callback invoked with each record after it is stored.
        """
        self._path = path
        self._sink = sink
        self._records: list[UsageRecord] = []

    @property
    def records(self) -> Iterable[UsageRecord]:
        """Records written so far."""
        return tuple(self._records)

    def append(self, record: UsageRecord) -> UsageRecord:
        """Persist one record and return the sanitised version that was stored."""
        safe = _sanitise(record)
        self._records.append(safe)
        payload = asdict(safe)
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload) + "\n")
        if self._sink is not None:
            self._sink(payload)
        return safe

    def totals(self) -> dict[str, float]:
        """Aggregate units per provider, e.g. ``{"gemini": 812.0}``."""
        totals: dict[str, float] = {}
        for record in self._records:
            totals[record.provider] = totals.get(record.provider, 0.0) + record.units
        return totals

#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Healthcare log redaction.

The runtime logs a lot: transcripts, tool arguments, provider payloads. None of
it may retain NHS numbers, phone numbers, dates of birth, postcodes or email
addresses. :func:`redact` masks those patterns in free text; :func:`redact_mapping`
does the same through a structured payload such as function-call arguments.

This is deliberately conservative: a false positive costs a masked digit in a
log line, a false negative leaks a patient identifier.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

#: Replacement marker for a masked value.
MASK = "[redacted]"

_NHS_NUMBER = re.compile(r"\b\d{3}[ -]?\d{3}[ -]?\d{4}\b")
_UK_PHONE = re.compile(
    r"(?:(?:\+|00)44|0)\s?(?:\d\s?){9,10}\b"
)
_UK_MOBILE = re.compile(r"\b07\d{3}[ -]?\d{6}\b")
_DATE_OF_BIRTH = re.compile(
    r"\b(?:0?[1-9]|[12]\d|3[01])[/.-](?:0?[1-9]|1[0-2])[/.-](?:19|20)\d{2}\b"
)
_ISO_DATE = re.compile(r"\b(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])\b")
_UK_POSTCODE = re.compile(
    r"\b(?:[A-PR-UWYZ][A-HK-Y]?[0-9][0-9A-HJKPSTUW]?)\s?[0-9][ABD-HJLNP-UW-Z]{2}\b",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", _EMAIL),
    ("nhs_number", _NHS_NUMBER),
    ("uk_mobile", _UK_MOBILE),
    ("uk_phone", _UK_PHONE),
    ("uk_postcode", _UK_POSTCODE),
    ("iso_date", _ISO_DATE),
    ("date_of_birth", _DATE_OF_BIRTH),
)


def redact(text: str) -> str:
    """Mask patient-identifying patterns in free text.

    Args:
        text: Any string bound for a log sink.

    Returns:
        The text with identifiers replaced by :data:`MASK`.
    """
    if not text:
        return text
    result = text
    for _name, pattern in _PATTERNS:
        result = pattern.sub(MASK, result)
    return result


def redact_mapping(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Mask identifiers inside a structured payload, recursively.

    Keys are preserved so the shape of a function call stays debuggable; values
    that are strings are redacted.

    Args:
        payload: Mapping such as function-call arguments.

    Returns:
        A new mapping with string values redacted.
    """
    result: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, str):
            result[key] = redact(value)
        elif isinstance(value, Mapping):
            result[key] = redact_mapping(value)
        elif isinstance(value, (list, tuple)):
            result[key] = [
                redact(item)
                if isinstance(item, str)
                else redact_mapping(item)
                if isinstance(item, Mapping)
                else item
                for item in value
            ]
        else:
            result[key] = value
    return result


def summary(text: str, limit: int = 80) -> str:
    """Return a redacted, length-bounded rendering of ``text`` for logging."""
    masked = redact(text).replace("\n", " ")
    if len(masked) <= limit:
        return masked
    return masked[:limit] + "…"

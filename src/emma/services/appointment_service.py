#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Deterministic appointment booking for the EMMA receptionist.

The voice assistant must never *claim* an appointment exists unless this module
actually stored one. Everything here is pure Python (no network): spoken date
and time phrases are parsed, validated against practice hours, checked against
the appointment ledger for conflicts, and written to the ledger plus the Excel
call record.

The ledger is a small JSON document so a booking survives a process restart and
can be inspected during a demo::

    {"appointments": [{"reference": "APT-20261112-1500-01", ...}]}
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

try:
    from loguru import logger
except ImportError:  # pragma: no cover - loguru is a runtime dependency
    import logging

    logger = logging.getLogger("emma.appointment_service")

DEFAULT_LEDGER_PATH = Path(os.environ.get("APPOINTMENT_LEDGER_PATH", "data/appointments.json"))

WEEKDAYS: Mapping[str, int] = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}

_NUMBER_WORDS: Mapping[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}

_TIME_PATTERN = re.compile(
    r"\b(?P<hour>\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
    r"(?::(?P<minute>\d{2}))?\s*(?P<meridiem>a\.?m\.?|p\.?m\.?)?\b",
    re.IGNORECASE,
)

_ISO_DATE = re.compile(r"\b(?P<year>20\d{2})-(?P<month>\d{1,2})-(?P<day>\d{1,2})\b")
_DAY_MONTH = re.compile(
    r"\b(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?P<month>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b",
    re.IGNORECASE,
)
_MONTH_DAY = re.compile(
    r"\b(?P<month>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?\b",
    re.IGNORECASE,
)

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


@dataclass(frozen=True)
class ClinicSchedule:
    """Opening hours and practitioners for the practice.

    Parameters:
        name: Practice name used in spoken confirmations.
        address: Practice address, read only when a caller asks.
        phone: Practice telephone number.
        doctors: Practitioners an appointment may be booked with.
        slot_minutes: Length of one appointment slot.
        open_days: Weekday numbers (Monday is 0) the practice is open.
        opens: Opening time on an open day.
        closes: Closing time on an open day.
        saturday_opens: Saturday opening time, when Saturday is open.
        saturday_closes: Saturday closing time.
    """

    name: str = "QuantumLoopAI Health Clinic"
    address: str = "100 Innovation Way, Suite 400"
    phone: str = ""
    doctors: tuple[str, ...] = ("Dr. Sharma", "Dr. Jenkins")
    slot_minutes: int = 15
    open_days: tuple[int, ...] = (0, 1, 2, 3, 4, 5)
    opens: time = time(8, 0)
    closes: time = time(18, 0)
    saturday_opens: time = time(9, 0)
    saturday_closes: time = time(13, 0)

    def hours_for(self, day: date) -> tuple[time, time] | None:
        """Return the opening window for a date, or None when closed."""
        if day.weekday() not in self.open_days:
            return None
        if day.weekday() == 5:
            return self.saturday_opens, self.saturday_closes
        return self.opens, self.closes

    def is_open_at(self, day: date, when: time) -> bool:
        """Whether the practice is open at a specific date and time."""
        window = self.hours_for(day)
        if window is None:
            return False
        opens, closes = window
        return opens <= when < closes

    def describe_hours(self) -> str:
        """A spoken description of opening hours."""
        weekday = "Monday to Friday, 8 AM to 6 PM"
        parts = [weekday] if 0 in self.open_days else []
        if 5 in self.open_days:
            parts.append("Saturday, 9 AM to 1 PM")
        if 6 in self.open_days:
            parts.append("Sunday, 10 AM to 1 PM")
        return ", and ".join(parts) if parts else "by appointment only"


def load_clinic_schedule(environ: Mapping[str, str] | None = None) -> ClinicSchedule:
    """Build the schedule from environment overrides, keeping safe defaults."""
    env = os.environ if environ is None else environ
    doctors_raw = env.get("CLINIC_DOCTORS", "")
    doctors = tuple(name.strip() for name in doctors_raw.split(",") if name.strip())
    try:
        slot_minutes = int(env.get("CLINIC_SLOT_MINUTES", "15"))
    except ValueError:
        slot_minutes = 15
    return ClinicSchedule(
        name=env.get("CLINIC_NAME", ClinicSchedule.name),
        address=env.get("CLINIC_ADDRESS", ClinicSchedule.address),
        phone=env.get("CLINIC_PHONE", ""),
        doctors=doctors or ClinicSchedule.doctors,
        slot_minutes=slot_minutes,
    )


# ---------------------------------------------------------------------------
# Spoken date and time parsing
# ---------------------------------------------------------------------------


def _word_to_hour(token: str) -> int | None:
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token)


def parse_date_phrase(text: str, now: datetime | None = None) -> date | None:
    """Extract a date from a spoken or written phrase.

    Supports ``today``, ``tomorrow``, ``day after tomorrow``, weekday names with
    or without ``next``/``this``, ISO dates, and day-month / month-day names.

    Args:
        text: Caller utterance, e.g. ``"next Tuesday"`` or ``"15 November"``.
        now: Reference time; defaults to the current local time.

    Returns:
        The resolved date, or None when the phrase names no date.
    """
    now = now or datetime.now()
    lowered = text.lower()

    iso = _ISO_DATE.search(text)
    if iso:
        try:
            return date(int(iso.group("year")), int(iso.group("month")), int(iso.group("day")))
        except ValueError:
            return None

    day_month = _DAY_MONTH.search(text)
    if day_month:
        month = _MONTHS.get(day_month.group("month").lower()[:3])
        if month:
            candidate = _canonical_year(month, int(day_month.group("day")), now)
            if candidate:
                return candidate

    month_day = _MONTH_DAY.search(text)
    if month_day:
        month = _MONTHS.get(month_day.group("month").lower()[:3])
        if month:
            candidate = _canonical_year(month, int(month_day.group("day")), now)
            if candidate:
                return candidate

    if "day after tomorrow" in lowered:
        return (now + timedelta(days=2)).date()
    if "tomorrow" in lowered:
        return (now + timedelta(days=1)).date()
    if "today" in lowered or "this morning" in lowered or "this afternoon" in lowered:
        return now.date()

    # Longest names first so "thursday" is not read as "thu".
    for name in sorted(WEEKDAYS, key=len, reverse=True):
        if not re.search(rf"\b{name}\b", lowered):
            continue
        weekday = WEEKDAYS[name]
        delta = (weekday - now.weekday()) % 7
        if delta == 0:
            # A weekday named on that same weekday means the next one.
            delta = 7
        return (now + timedelta(days=delta)).date()

    return None


def _canonical_year(month: int, day: int, now: datetime) -> date | None:
    for year in (now.year, now.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= now.date():
            return candidate
    return None


def parse_time_phrase(text: str) -> time | None:
    """Extract a clock time from a spoken phrase.

    Args:
        text: Caller utterance, e.g. ``"3 PM"``, ``"15:30"`` or ``"half past 2"``.

    Returns:
        The resolved time, or None when the phrase names no time. A bare hour
        must be preceded by ``at`` so a date such as ``"15 November"`` is not
        mistaken for 15:00.
    """
    lowered = text.lower()
    if re.search(r"\b(noon|midday)\b", lowered):
        return time(12, 0)
    if re.search(r"\bmidnight\b", lowered):
        return time(0, 0)

    half_past = re.search(r"half past\s+(?P<hour>\d{1,2}|[a-z]+)", lowered)
    if half_past:
        hour = _word_to_hour(half_past.group("hour"))
        if hour is not None:
            return _to_24h(hour, 30, _meridiem(lowered))

    for match in _TIME_PATTERN.finditer(lowered):
        hour = _word_to_hour(match.group("hour"))
        if hour is None or hour > 24:
            continue
        minute = int(match.group("minute") or 0)
        if minute > 59:
            continue
        meridiem = _meridiem(match.group("meridiem") or "")
        if meridiem is None:
            # Without "am"/"pm" a number is only a time when it is written with
            # a colon ("15:30") or introduced by "at" ("at 3"). Otherwise
            # "15 November" would be read as 15:00.
            preceded_by_at = (
                lowered[max(0, match.start() - 4) : match.start()].strip().endswith("at")
            )
            has_colon = match.group("minute") is not None
            if not (preceded_by_at or has_colon):
                continue
        return _to_24h(hour, minute, meridiem)
    return None


def _meridiem(text: str) -> str | None:
    match = re.search(r"\b(a\.?m\.?|p\.?m\.?)\b", text, re.IGNORECASE)
    if not match:
        return None
    return "am" if match.group(1).lower().startswith("a") else "pm"


def _to_24h(hour: int, minute: int, meridiem: str | None) -> time | None:
    if hour == 12 and meridiem == "am":
        hour = 0
    elif hour < 12 and meridiem == "pm":
        hour += 12
    elif meridiem is None and hour <= 6:
        # "at 3" in the afternoon is the overwhelmingly common receptionist case.
        hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour, minute)


def parse_period(text: str) -> time | None:
    """Return a representative slot for a vague time of day."""
    lowered = text.lower()
    if "morning" in lowered:
        return time(9, 0)
    if "afternoon" in lowered:
        return time(14, 0)
    if "evening" in lowered:
        return time(17, 0)
    return None


def parse_appointment_request(
    text: str, now: datetime | None = None
) -> tuple[date | None, time | None]:
    """Parse a date and time from one utterance.

    Args:
        text: Caller utterance such as ``"tomorrow at 3 PM"``.
        now: Reference time; defaults to the current local time.

    Returns:
        A ``(date, time)`` pair; either element may be None when absent. A vague
        period (``"tomorrow afternoon"``) resolves to a representative time.
    """
    now = now or datetime.now()
    day = parse_date_phrase(text, now)
    at = parse_time_phrase(text)
    if at is None:
        at = parse_period(text)
    return day, at


# ---------------------------------------------------------------------------
# Booking
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Appointment:
    """One confirmed appointment."""

    reference: str
    appointment_date: str
    appointment_time: str
    doctor: str
    reason: str
    first_name: str
    last_name: str
    email: str
    mobile: str
    created_at: str

    def to_dict(self) -> dict[str, str]:
        """Serialize for the JSON ledger."""
        return {
            "reference": self.reference,
            "appointment_date": self.appointment_date,
            "appointment_time": self.appointment_time,
            "doctor": self.doctor,
            "reason": self.reason,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "email": self.email,
            "mobile": self.mobile,
            "created_at": self.created_at,
        }


def _ledger_path(path: Path | str | None = None) -> Path:
    return Path(path) if path else DEFAULT_LEDGER_PATH


def _read_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(f"[Appointments] Ignoring unreadable ledger {path}: {exc}")
        return []
    entries = payload.get("appointments", []) if isinstance(payload, dict) else payload
    return [entry for entry in entries if isinstance(entry, dict)]


def _write_ledger(path: Path, entries: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps({"appointments": list(entries)}, indent=2), "utf-8")
    temp.replace(path)


def booked_slots(
    day: date,
    doctor: str | None = None,
    *,
    ledger_path: Path | str | None = None,
) -> set[str]:
    """Return the ``HH:MM`` times already booked for a date.

    Args:
        day: Date to inspect.
        doctor: Restrict the answer to one practitioner.
        ledger_path: Override the ledger location (used by tests).

    Returns:
        A set of ``HH:MM`` strings.
    """
    entries = _read_ledger(_ledger_path(ledger_path))
    target = day.isoformat()
    return {
        str(entry.get("appointment_time", ""))
        for entry in entries
        if entry.get("appointment_date") == target
        and (doctor is None or entry.get("doctor") == doctor)
    }


def next_available_slots(
    day: date,
    *,
    count: int = 3,
    schedule: ClinicSchedule | None = None,
    ledger_path: Path | str | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Suggest the next free slots on a date.

    Args:
        day: Date to search.
        count: How many suggestions to return.
        schedule: Practice schedule; defaults to the environment configuration.
        ledger_path: Override the ledger location.
        now: Reference time, used to skip slots that have already passed.

    Returns:
        A list of ``HH:MM`` strings; empty when the practice is closed.
    """
    schedule = schedule or load_clinic_schedule()
    now = now or datetime.now()
    window = schedule.hours_for(day)
    if window is None:
        return []
    opens, closes = window
    taken = booked_slots(day, ledger_path=ledger_path)
    suggestions: list[str] = []
    cursor = datetime.combine(day, opens)
    end = datetime.combine(day, closes)
    while cursor < end and len(suggestions) < count:
        if cursor > now:
            label = cursor.strftime("%H:%M")
            if label not in taken:
                suggestions.append(label)
        cursor += timedelta(minutes=schedule.slot_minutes)
    return suggestions


def book_appointment(
    *,
    first_name: str,
    appointment_date: str,
    appointment_time: str,
    last_name: str = "",
    email: str = "",
    mobile: str = "",
    reason: str = "",
    doctor: str = "",
    schedule: ClinicSchedule | None = None,
    ledger_path: Path | str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate and store an appointment.

    Args:
        first_name: Caller first name.
        appointment_date: ISO date (``YYYY-MM-DD``).
        appointment_time: 24-hour time (``HH:MM``).
        last_name: Caller last name.
        email: Caller email address.
        mobile: Caller mobile number.
        reason: Reason for the visit.
        doctor: Requested practitioner; empty means any available one.
        schedule: Practice schedule; defaults to the environment configuration.
        ledger_path: Override the ledger location.
        now: Reference time, used to reject dates in the past.

    Returns:
        A result mapping with ``success``, a spoken ``message`` and, on success,
        the stored appointment fields. On failure, ``error`` explains why and
        ``alternatives`` may list free slots.
    """
    schedule = schedule or load_clinic_schedule()
    now = now or datetime.now()
    path = _ledger_path(ledger_path)

    try:
        day = date.fromisoformat(appointment_date)
    except (TypeError, ValueError):
        return {
            "success": False,
            "error": "INVALID_DATE",
            "message": "I did not catch the date. Which day would you like?",
        }

    parsed_time = _parse_iso_time(appointment_time)
    if parsed_time is None:
        return {
            "success": False,
            "error": "INVALID_TIME",
            "message": "I did not catch the time. What time would you prefer?",
        }

    if day < now.date():
        return {
            "success": False,
            "error": "DATE_IN_PAST",
            "message": "That date has already passed. Which upcoming day works for you?",
            "alternatives": [
                f"{day.isoformat()} {slot}"
                for slot in next_available_slots(day, count=2, schedule=schedule, ledger_path=path)
            ],
        }

    if not schedule.is_open_at(day, parsed_time):
        window = schedule.hours_for(day)
        if window is None:
            return {
                "success": False,
                "error": "CLOSED",
                "message": (
                    f"The practice is closed on {day.strftime('%A')}. "
                    f"We are open {schedule.describe_hours()}."
                ),
            }
        opens, closes = window
        return {
            "success": False,
            "error": "OUTSIDE_HOURS",
            "message": (
                f"On {day.strftime('%A')} we are open {_spoken_time(opens)} to "
                f"{_spoken_time(closes)}. What time in that window suits you?"
            ),
            "alternatives": _spoken_slots(
                next_available_slots(day, count=3, schedule=schedule, ledger_path=path)
            ),
        }

    requested_doctor = (doctor or "").strip()
    if requested_doctor and not _matches_doctor(requested_doctor, schedule.doctors):
        return {
            "success": False,
            "error": "UNKNOWN_DOCTOR",
            "message": (
                f"I can book with {', '.join(schedule.doctors)}. Which of those would you prefer?"
            ),
            "doctors": list(schedule.doctors),
        }

    chosen_doctor = _resolve_doctor(requested_doctor, schedule.doctors)
    if _slot_taken(day, parsed_time, chosen_doctor, path):
        return {
            "success": False,
            "error": "SLOT_TAKEN",
            "message": _slot_taken_message(
                chosen_doctor,
                day,
                parsed_time,
                next_available_slots(day, count=3, schedule=schedule, ledger_path=path, now=now),
            ),
            "alternatives": [
                label
                for label in next_available_slots(
                    day, count=3, schedule=schedule, ledger_path=path, now=now
                )
            ],
        }

    reference = _reference(day, parsed_time, path)
    appointment = Appointment(
        reference=reference,
        appointment_date=day.isoformat(),
        appointment_time=parsed_time.strftime("%H:%M"),
        doctor=chosen_doctor,
        reason=reason,
        first_name=first_name,
        last_name=last_name,
        email=email,
        mobile=mobile,
        created_at=now.isoformat(timespec="seconds"),
    )

    entries = _read_ledger(path)
    entries.append(appointment.to_dict())
    _write_ledger(path, entries)
    logger.info(
        f"[Appointments] Booked {reference} for {first_name} {last_name} "
        f"on {appointment.appointment_date} at {appointment.appointment_time} "
        f"with {chosen_doctor}"
    )

    spoken_date = day.strftime("%A %d %B")
    return {
        "success": True,
        "reference": reference,
        "appointment_date": appointment.appointment_date,
        "appointment_time": appointment.appointment_time,
        "doctor": chosen_doctor,
        "reason": reason,
        "message": (
            f"You're booked in for {spoken_date} at {_spoken_time(parsed_time)} with "
            f"{chosen_doctor}. Your reference is {reference}."
        ),
    }


def find_appointment(
    *,
    reference: str = "",
    first_name: str = "",
    ledger_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Look up stored appointments by reference or caller name."""
    entries = _read_ledger(_ledger_path(ledger_path))
    if reference:
        return [entry for entry in entries if entry.get("reference") == reference]
    if first_name:
        name = first_name.strip().lower()
        return [
            entry for entry in entries if str(entry.get("first_name", "")).strip().lower() == name
        ]
    return []


def cancel_appointment(
    reference: str,
    *,
    ledger_path: Path | str | None = None,
) -> dict[str, Any]:
    """Remove an appointment by reference."""
    path = _ledger_path(ledger_path)
    entries = _read_ledger(path)
    remaining = [entry for entry in entries if entry.get("reference") != reference]
    if len(remaining) == len(entries):
        return {
            "success": False,
            "error": "NOT_FOUND",
            "message": f"I could not find an appointment with reference {reference}.",
        }
    _write_ledger(path, remaining)
    return {
        "success": True,
        "reference": reference,
        "message": f"Appointment {reference} has been cancelled.",
    }


def _spoken_time(value: time) -> str:
    """Render a time the way a receptionist would say it ("3:15 PM")."""
    return value.strftime("%I:%M %p").lstrip("0")


def _spoken_slots(slots: Iterable[str]) -> list[str]:
    """Render ``HH:MM`` slot labels as spoken times."""
    spoken: list[str] = []
    for label in slots:
        try:
            spoken.append(_spoken_time(time.fromisoformat(label)))
        except ValueError:
            spoken.append(label)
    return spoken


def _slot_taken_message(doctor: str, day: date, when: time, alternatives: Iterable[str]) -> str:
    """Explain a clash and offer the next free times out loud."""
    message = f"{doctor} is already booked at {_spoken_time(when)} on {day.strftime('%A')}."
    options = _spoken_slots(alternatives)
    if options:
        message += f" I can offer {', '.join(options[:-1])}"
        message += f" or {options[-1]}" if len(options) > 1 else options[0]
        message += " instead. Which would you prefer?"
    else:
        message += " Would another day suit you?"
    return message


def _parse_iso_time(value: str) -> time | None:
    text = (value or "").strip()
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I%p"):
        try:
            return datetime.strptime(text.upper(), fmt).time()
        except ValueError:
            continue
    return None


def _matches_doctor(requested: str, doctors: Iterable[str]) -> bool:
    wanted = _surname(requested)
    return any(
        wanted in _surname(candidate) or _surname(candidate) in wanted for candidate in doctors
    )


def _surname(name: str) -> str:
    return name.lower().replace("dr.", "").replace("doctor", "").strip()


def _resolve_doctor(requested: str, doctors: Sequence[str]) -> str:
    if not doctors:
        return "the practice"
    if not requested:
        return doctors[0]
    wanted = _surname(requested)
    for candidate in doctors:
        if wanted in _surname(candidate) or _surname(candidate) in wanted:
            return candidate
    return doctors[0]


def _slot_taken(day: date, when: time, doctor: str, path: Path) -> bool:
    label = when.strftime("%H:%M")
    for entry in _read_ledger(path):
        if entry.get("appointment_date") != day.isoformat():
            continue
        if entry.get("appointment_time") != label:
            continue
        if _surname(str(entry.get("doctor", ""))) == _surname(doctor):
            return True
    return False


def _reference(day: date, when: time, path: Path) -> str:
    stem = f"APT-{day.strftime('%Y%m%d')}-{when.strftime('%H%M')}"
    existing = {
        str(entry.get("reference", ""))
        for entry in _read_ledger(path)
        if str(entry.get("reference", "")).startswith(stem)
    }
    suffix = 1
    while f"{stem}-{suffix:02d}" in existing:
        suffix += 1
    return f"{stem}-{suffix:02d}"

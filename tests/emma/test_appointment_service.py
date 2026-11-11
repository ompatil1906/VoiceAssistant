#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Appointment parsing and booking tests.

These are the claims the voice assistant is allowed to make, so the tests cover
the refusals as closely as the successes: a closed day, a past date, a slot
already taken and a time outside opening hours must all return a message
instead of a confirmation.
"""

from datetime import date, datetime, time

import pytest

from emma.services.appointment_service import (
    ClinicSchedule,
    book_appointment,
    find_appointment,
    next_available_slots,
    parse_appointment_request,
    parse_date_phrase,
    parse_time_phrase,
)

NOW = datetime(2026, 11, 11, 16, 0)  # Wednesday


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("today", date(2026, 11, 11)),
        ("tomorrow", date(2026, 11, 12)),
        ("the day after tomorrow", date(2026, 11, 13)),
        ("next Tuesday", date(2026, 11, 17)),
        ("Friday", date(2026, 11, 13)),
        ("15 November", date(2026, 11, 15)),
        ("November 20th", date(2026, 11, 20)),
        ("2026-12-01", date(2026, 12, 1)),
        ("", None),
    ],
)
def test_parse_date_phrase(phrase, expected):
    assert parse_date_phrase(phrase, NOW) == expected


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("at 3 PM", time(15, 0)),
        ("3pm", time(15, 0)),
        ("at three pm", time(15, 0)),
        ("at 15:30", time(15, 30)),
        ("15:30", time(15, 30)),
        ("at noon", time(12, 0)),
        ("afternoon", None),
        ("15 November", None),
        ("I have an appointment", None),
    ],
)
def test_parse_time_phrase(phrase, expected):
    assert parse_time_phrase(phrase) == expected


def test_parse_appointment_request_resolves_a_period():
    day, at = parse_appointment_request("next Tuesday afternoon", NOW)
    assert day == date(2026, 11, 17)
    assert at == time(14, 0)


def _schedule() -> ClinicSchedule:
    return ClinicSchedule(
        name="Test Practice",
        address="1 Test Street",
        doctors=("Dr. Sharma", "Dr. Jenkins"),
        slot_minutes=15,
    )


def test_booking_succeeds_and_persists(tmp_path):
    ledger = tmp_path / "appointments.json"
    result = book_appointment(
        first_name="Sam",
        last_name="Patel",
        email="sam@example.com",
        mobile="07700900123",
        reason="checkup",
        doctor="Sharma",
        appointment_date="2026-11-12",
        appointment_time="15:00",
        schedule=_schedule(),
        ledger_path=ledger,
        now=NOW,
    )

    assert result["success"] is True
    assert result["doctor"] == "Dr. Sharma"
    assert result["reference"].startswith("APT-20261112-1500")
    assert "3:00 PM" in result["message"]
    assert "15:00" not in result["message"]
    assert ledger.exists()
    assert find_appointment(reference=result["reference"], ledger_path=ledger)


def test_same_slot_is_refused(tmp_path):
    ledger = tmp_path / "appointments.json"
    kwargs = {
        "first_name": "Sam",
        "appointment_date": "2026-11-12",
        "appointment_time": "15:00",
        "schedule": _schedule(),
        "ledger_path": ledger,
        "now": NOW,
    }
    first = book_appointment(**kwargs)
    second = book_appointment(**kwargs)

    assert first["success"] is True
    assert second["success"] is False
    assert second["error"] == "SLOT_TAKEN"
    assert second["alternatives"]


def test_closed_day_is_refused(tmp_path):
    result = book_appointment(
        first_name="Sam",
        appointment_date="2026-11-15",  # Sunday
        appointment_time="10:00",
        schedule=_schedule(),
        ledger_path=tmp_path / "a.json",
        now=NOW,
    )
    assert result["success"] is False
    assert result["error"] == "CLOSED"


def test_outside_hours_is_refused(tmp_path):
    result = book_appointment(
        first_name="Sam",
        appointment_date="2026-11-12",
        appointment_time="19:00",
        schedule=_schedule(),
        ledger_path=tmp_path / "a.json",
        now=NOW,
    )
    assert result["success"] is False
    assert result["error"] == "OUTSIDE_HOURS"
    assert result["alternatives"]


def test_past_date_is_refused(tmp_path):
    result = book_appointment(
        first_name="Sam",
        appointment_date="2026-11-10",
        appointment_time="10:00",
        schedule=_schedule(),
        ledger_path=tmp_path / "a.json",
        now=NOW,
    )
    assert result["success"] is False
    assert result["error"] == "DATE_IN_PAST"


def test_unknown_doctor_names_the_available_practitioners(tmp_path):
    result = book_appointment(
        first_name="Sam",
        appointment_date="2026-11-12",
        appointment_time="10:00",
        doctor="Dr. House",
        schedule=_schedule(),
        ledger_path=tmp_path / "a.json",
        now=NOW,
    )
    assert result["success"] is False
    assert result["error"] == "UNKNOWN_DOCTOR"
    assert result["doctors"] == ["Dr. Sharma", "Dr. Jenkins"]


def test_next_available_slots_skips_taken_and_past_times(tmp_path):
    ledger = tmp_path / "a.json"
    book_appointment(
        first_name="Sam",
        appointment_date="2026-11-12",
        appointment_time="15:00",
        schedule=_schedule(),
        ledger_path=ledger,
        now=NOW,
    )
    slots = next_available_slots(
        date(2026, 11, 12),
        count=3,
        schedule=_schedule(),
        ledger_path=ledger,
        now=datetime(2026, 11, 12, 14, 50),
    )
    assert "15:00" not in slots
    assert slots == ["15:15", "15:30", "15:45"]

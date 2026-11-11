#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Tests for the tools the voice assistant may call.

The important property is honesty: a tool result must describe what the backend
actually did, and a refusal must never contain a confirmation. These tests also
pin the transcript hygiene that keeps EMMA from answering its own audio.
"""

import csv
from datetime import datetime

from emma.services.appointment_service import ClinicSchedule
from emma.services.voice_assistant import (
    AssistantTools,
    CallerProfile,
    build_system_instruction,
    is_duplicate_transcript,
    is_echo,
    similarity,
)

CALLER = CallerProfile(
    first_name="Sam",
    last_name="Patel",
    email="sam@example.com",
    mobile="07700900123",
    reason="checkup",
)


def _records(tmp_path) -> list[dict[str, str]]:
    """Read the CSV mirror the Excel writer keeps beside the workbook."""
    path = tmp_path / "calls.csv"
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _tools(tmp_path, *, record_calls: bool = True) -> AssistantTools:
    return AssistantTools(
        caller=CALLER,
        schedule=ClinicSchedule(name="Test Practice", address="1 Test Street"),
        appointment_ledger=tmp_path / "appointments.json",
        excel_path=tmp_path / "calls.xlsx",
        record_calls=record_calls,
    )


def test_declarations_cover_every_backend_feature(tmp_path):
    names = {item["name"] for item in _tools(tmp_path).function_declarations()}
    assert names == {
        "book_appointment",
        "request_prescription",
        "lookup_appointment",
        "get_clinic_info",
        "record_call_summary",
    }


def test_booking_tool_writes_the_ledger_and_the_call_record(tmp_path):
    tools = _tools(tmp_path)
    result, spoken = tools.execute(
        "book_appointment",
        {
            "appointment_date": "2026-11-12",
            "appointment_time": "15:00",
            "reason": "checkup",
            "doctor": "Sharma",
        },
    )

    assert result["success"] is True
    assert result["reference"].startswith("APT-20261112-1500")
    assert "booked in" in spoken.lower()
    assert result["reference"] in spoken
    assert (tmp_path / "appointments.json").exists()

    records = _records(tmp_path)
    assert len(records) == 1
    assert records[0]["Request Label"] == "Appointment Booking"
    assert records[0]["Intake Reason"] == "Appointment Booking"
    assert records[0]["Appointment Time"] == "15:00"


def test_booking_tool_reports_a_conflict_without_confirming(tmp_path):
    tools = _tools(tmp_path)
    payload = {"appointment_date": "2026-11-12", "appointment_time": "15:00"}
    assert tools.execute("book_appointment", payload)[0]["success"] is True

    result, spoken = tools.execute("book_appointment", payload)

    assert result["success"] is False
    assert result["error"] == "SLOT_TAKEN"
    assert "confirmed" not in spoken.lower()
    assert result["alternatives"]


def test_booking_without_a_time_asks_for_one(tmp_path):
    result, spoken = _tools(tmp_path).execute(
        "book_appointment", {"appointment_date": "2026-11-12"}
    )
    assert result["success"] is False
    assert result["error"] == "MISSING_DATE_OR_TIME"
    assert "time" in spoken.lower()


def test_prescription_tool_uses_the_verification_service(tmp_path, monkeypatch):
    calls = {}

    def fake_handle(**kwargs):
        calls.update(kwargs)
        return {
            "success": True,
            "verified": True,
            "patient_id": "110",
            "message": "Patient ID #110 confirmed. Prescription sent.",
        }

    monkeypatch.setattr("emma.services.voice_assistant.handle_prescription_request", fake_handle)

    result, spoken = _tools(tmp_path).execute("request_prescription", {"patient_id": "110"})

    assert result["verified"] is True
    assert calls["patient_id_input"] == "110"
    assert calls["to_email"] == "sam@example.com"
    assert "110" in spoken

    # The intake form said "checkup" but the call was a prescription request.
    # The record must describe what was handled, with the form note kept only
    # in the summary.
    records = _records(tmp_path)
    assert len(records) == 1
    assert records[0]["Request Label"] == "Prescription Request"
    assert records[0]["Intake Reason"] == "Prescription Request"
    assert "Intake form note: checkup" in records[0]["Call Summary"]


def test_unverified_prescription_is_never_spoken_as_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "emma.services.voice_assistant.handle_prescription_request",
        lambda **kwargs: {
            "success": False,
            "verified": False,
            "message": "That patient ID could not be verified.",
        },
    )
    result, spoken = _tools(tmp_path).execute("request_prescription", {"patient_id": "999"})
    assert result["verified"] is False
    assert "sent" not in spoken.lower()


def test_lookup_reports_a_stored_appointment(tmp_path):
    tools = _tools(tmp_path)
    booked, _ = tools.execute(
        "book_appointment",
        {"appointment_date": "2026-11-12", "appointment_time": "15:00", "doctor": "Sharma"},
    )
    result, spoken = tools.execute("lookup_appointment", {})

    assert result["found"] is True
    assert result["appointment"]["reference"] == booked["reference"]
    assert booked["reference"] in spoken
    assert "3:00 PM" in spoken


def test_lookup_never_invents_an_appointment(tmp_path):
    result, spoken = _tools(tmp_path).execute("lookup_appointment", {})
    assert result["found"] is False
    assert "don't have an appointment" in spoken
    assert "APT-" not in spoken


def test_clinic_info_is_read_from_the_schedule(tmp_path):
    tools = _tools(tmp_path)
    result, spoken = tools.execute("get_clinic_info", {"topic": "address"})
    assert result["success"] is True
    assert "1 Test Street" in spoken


def test_summary_tool_records_one_general_row(tmp_path):
    tools = _tools(tmp_path)
    tools.execute(
        "record_call_summary",
        {"summary": "Answered a question about opening hours.", "request_label": "General Inquiry"},
    )
    records = _records(tmp_path)
    assert len(records) == 1
    assert records[0]["Call Summary"].startswith("Answered a question")


def test_fallback_summary_is_skipped_once_a_tool_recorded(tmp_path):
    tools = _tools(tmp_path)
    tools.execute(
        "book_appointment",
        {"appointment_date": "2026-11-12", "appointment_time": "15:00"},
    )
    tools.record_fallback_summary("Call ended.")
    assert len(_records(tmp_path)) == 1


def test_system_instruction_is_grounded_and_forbids_invention():
    text = build_system_instruction(CALLER, now=datetime(2026, 11, 11, 9, 0))
    assert "Wednesday 11 November 2026" in text
    assert "Sam Patel" in text
    assert "book_appointment" in text
    assert "Never invent a practitioner" in text
    # The practice list is data from the schedule; a scripted confirmation is not.
    assert "confirm the booking with Dr. Sharma" not in text.lower()


def test_echo_detection_catches_the_assistant_s_own_sentence():
    said = (
        "Hello Sam, you've reached the clinic. I'm EMMA, the receptionist. "
        "How can I help you today?"
    )
    assert is_echo("How can I help you today?", said) is True
    assert is_echo("I need a repeat prescription", said) is False


def test_echo_detection_catches_a_partial_with_different_spacing():
    said = (
        "Hello Om, you've reached QuantumLoopAI Health Clinic. I'm EMMA, the "
        "receptionist. How can I help you today?"
    )
    # Exactly what the loopback test produced through a speaker.
    assert is_echo("Hello, Om. You've reached Quantum Loop AI Health Clinic.", said) is True
    # A genuine reply must not be mistaken for echo just because it shares
    # common words with the greeting.
    assert is_echo("Can you help me book an appointment today?", said) is False


def test_duplicate_transcripts_from_two_stt_paths_are_detected():
    previous = ["I would like to book an appointment tomorrow at 3 PM"]
    assert is_duplicate_transcript("I would like to book an appointment tomorrow at 3PM", previous)
    assert not is_duplicate_transcript("Actually make it Friday", previous)


def test_similarity_is_symmetric_and_bounded():
    assert similarity("book an appointment", "appointment book") == 1.0
    assert similarity("book an appointment", "prescription") == 0.0

#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Backend tools the EMMA voice assistant is allowed to use.

The language model never writes a booking, a prescription or a call record
itself: it may only ask for one of the tools declared here, and this module
executes it against the first-party services:

* :mod:`emma.services.appointment_service` — conflict-checked booking ledger.
* :mod:`emma.services.prescription_service` — patient-ID verification, SMTP
  dispatch and a local archive copy.
* :mod:`emma.storage.call_records_excel` — the clinic call record.
* :mod:`emma.cost.budget` — per-session ceilings for requests, tokens and
  synthesis characters.

The system instruction is generated from the same caller profile, so the model
is grounded in the practice record rather than a hard-coded script.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from emma.cost.budget import SessionBudget
from emma.services.appointment_service import (
    ClinicSchedule,
    book_appointment,
    find_appointment,
    load_clinic_schedule,
    parse_appointment_request,
)
from emma.services.prescription_service import VALID_PATIENT_ID, handle_prescription_request
from emma.storage.call_records_excel import record_call_to_excel

try:
    from loguru import logger
except ImportError:  # pragma: no cover - loguru is a runtime dependency
    import logging

    logger = logging.getLogger("emma.voice_assistant")


@dataclass
class CallerProfile:
    """The intake form the caller completed before the call started.

    Parameters:
        first_name: Caller first name.
        last_name: Caller last name.
        email: Caller email address.
        mobile: Caller mobile number.
        reason: Reason for calling, as typed into the intake form.
    """

    first_name: str = ""
    last_name: str = ""
    email: str = ""
    mobile: str = ""
    reason: str = ""

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> CallerProfile:
        """Build a profile from the token request's ``caller`` object."""
        data = payload or {}

        def pick(*names: str) -> str:
            for name in names:
                value = data.get(name)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return ""

        return cls(
            first_name=pick("firstName", "first_name"),
            last_name=pick("lastName", "last_name"),
            email=pick("email"),
            mobile=pick("mobile", "mobileNumber", "mobile_number", "phone"),
            reason=pick("reason"),
        )

    @property
    def full_name(self) -> str:
        """First and last name, for spoken confirmations."""
        return f"{self.first_name} {self.last_name}".strip()


@dataclass
class AssistantTools:
    """Executable backend features exposed to the voice model.

    Parameters:
        caller: Intake-form profile for this call.
        schedule: Practice schedule; defaults to the environment configuration.
        budget: Session budget used to record tool-side requests.
        appointment_ledger: Override for the appointment ledger path (tests).
        excel_path: Override for the Excel call record path (tests).
        record_calls: When false, tool execution does not touch the Excel
            ledger. Used by unit tests that only assert on the result.
    """

    caller: CallerProfile
    schedule: ClinicSchedule | None = None
    budget: SessionBudget | None = None
    appointment_ledger: Any | None = None
    excel_path: Any | None = None
    record_calls: bool = True
    _recorded_labels: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.schedule = self.schedule or load_clinic_schedule()

    # -- declarations -----------------------------------------------------

    def function_declarations(self) -> list[dict[str, Any]]:
        """Gemini ``function_declarations`` for every backend feature."""
        return [
            {
                "name": "book_appointment",
                "description": (
                    "Book a confirmed appointment in the practice diary. Call this only "
                    "after the caller has given both a day and a time. It checks opening "
                    "hours, existing bookings and returns a reference. Never tell the "
                    "caller an appointment is booked before this tool succeeds."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "appointment_date": {
                            "type": "STRING",
                            "description": (
                                "ISO date YYYY-MM-DD. Convert spoken dates such as "
                                "'tomorrow' or 'next Tuesday' using today's date."
                            ),
                        },
                        "appointment_time": {
                            "type": "STRING",
                            "description": "24-hour time HH:MM, e.g. '15:00' for 3 PM.",
                        },
                        "reason": {
                            "type": "STRING",
                            "description": "Purpose of the visit in the caller's words.",
                        },
                        "doctor": {
                            "type": "STRING",
                            "description": (
                                f"Requested practitioner. Available: {', '.join(self.schedule.doctors)}."
                            ),
                        },
                    },
                    "required": ["appointment_date", "appointment_time"],
                },
            },
            {
                "name": "request_prescription",
                "description": (
                    "Verify a caller's patient ID and dispatch an electronic prescription "
                    "to their email. Call this only after the caller has stated their "
                    "patient ID. It sends an email, so do not call it speculatively."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "patient_id": {
                            "type": "STRING",
                            "description": "Patient ID exactly as the caller said it.",
                        },
                        "medication": {
                            "type": "STRING",
                            "description": "Medication requested, if the caller named one.",
                        },
                        "email": {
                            "type": "STRING",
                            "description": "Destination email; defaults to the intake email.",
                        },
                    },
                    "required": ["patient_id"],
                },
            },
            {
                "name": "lookup_appointment",
                "description": (
                    "Look up appointments already stored for this caller. Use it whenever "
                    "the caller asks whether they have an appointment, and never guess."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "reference": {
                            "type": "STRING",
                            "description": "Booking reference, when the caller has one.",
                        }
                    },
                    "required": [],
                },
            },
            {
                "name": "get_clinic_info",
                "description": (
                    "Read verified practice facts: opening hours, address, contact details "
                    "or the services the practice offers."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "topic": {
                            "type": "STRING",
                            "enum": ["hours", "address", "contact", "services"],
                            "description": "Which fact the caller asked for.",
                        }
                    },
                    "required": ["topic"],
                },
            },
            {
                "name": "record_call_summary",
                "description": (
                    "Store the outcome of a call that did not end in an appointment or "
                    "prescription, for example a general question that was answered."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "summary": {
                            "type": "STRING",
                            "description": "One or two sentences describing the request and outcome.",
                        },
                        "request_label": {
                            "type": "STRING",
                            "enum": [
                                "General Inquiry",
                                "Appointment Booking",
                                "Prescription Request",
                                "Billing",
                            ],
                        },
                    },
                    "required": ["summary"],
                },
            },
        ]

    def gemini_tools(self) -> list[dict[str, Any]]:
        """The ``tools`` array for a Gemini ``generateContent`` request."""
        return [{"function_declarations": self.function_declarations()}]

    # -- execution --------------------------------------------------------

    def execute(self, name: str, arguments: Mapping[str, Any] | None) -> tuple[dict[str, Any], str]:
        """Run one tool call.

        Args:
            name: Declared function name.
            arguments: Model-supplied arguments.

        Returns:
            A ``(result, summary)`` pair. ``summary`` is the spoken text the
            assistant must use for this step; it is always grounded in what the
            backend actually did.
        """
        args = dict(arguments or {})
        handlers: dict[str, Callable[[dict[str, Any]], tuple[dict[str, Any], str]]] = {
            "book_appointment": self.book_appointment,
            "request_prescription": self.request_prescription,
            "lookup_appointment": self.lookup_appointment,
            "get_clinic_info": self.get_clinic_info,
            "record_call_summary": self.record_call_summary,
        }
        handler = handlers.get(name)
        if handler is None:
            logger.warning(f"[Tools] Refusing unknown tool {name!r}")
            return (
                {
                    "success": False,
                    "error": "UNKNOWN_TOOL",
                    "message": "That action is not available.",
                },
                "That action is not available.",
            )
        return handler(args)

    def book_appointment(self, args: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """Validate and store an appointment."""
        raw_date = str(args.get("appointment_date", "")).strip()
        raw_time = str(args.get("appointment_time", "")).strip()

        if not raw_date or not raw_time:
            parsed_date, parsed_time = parse_appointment_request(
                f"{raw_date} {raw_time}".strip(), datetime.now()
            )
            raw_date = raw_date or (parsed_date.isoformat() if parsed_date else "")
            raw_time = raw_time or (parsed_time.strftime("%H:%M") if parsed_time else "")

        if not raw_date or not raw_time:
            return (
                {
                    "success": False,
                    "error": "MISSING_DATE_OR_TIME",
                    "message": "I need both a day and a time to book the appointment.",
                },
                "I need both a day and a time to book the appointment.",
            )

        if self.budget is not None:
            self.budget.reserve_request()

        result = book_appointment(
            first_name=self.caller.first_name or "Caller",
            last_name=self.caller.last_name,
            email=self.caller.email,
            mobile=self.caller.mobile,
            reason=str(args.get("reason", "")) or self.caller.reason,
            doctor=str(args.get("doctor", "")),
            appointment_date=raw_date,
            appointment_time=raw_time,
            schedule=self.schedule,
            ledger_path=self.appointment_ledger,
        )

        if result.get("success") and self.record_calls:
            record_call_to_excel(
                first_name=self.caller.first_name,
                last_name=self.caller.last_name,
                email=self.caller.email,
                mobile=self.caller.mobile,
                intake_reason="Appointment Booking",
                request_label="Appointment Booking",
                appointment_date=str(result.get("appointment_date", "")),
                appointment_time=str(result.get("appointment_time", "")),
                call_summary=self._record_summary(
                    str(result.get("message", "")), "Appointment Booking"
                ),
                status="Confirmed",
                file_path=self._excel_path(),
            )
            self._recorded_labels.append("Appointment Booking")

        return result, str(result.get("message", ""))

    def request_prescription(self, args: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """Verify a patient ID and dispatch the prescription email."""
        patient_id = str(args.get("patient_id", "")).strip()
        medication = str(args.get("medication", "")).strip() or "Amoxicillin 500mg Oral Capsule"
        target_email = str(args.get("email", "")).strip() or self.caller.email

        if not patient_id:
            return (
                {
                    "success": False,
                    "error": "MISSING_PATIENT_ID",
                    "message": "Could you tell me your patient ID so I can verify you?",
                },
                "Could you tell me your patient ID so I can verify you?",
            )

        if self.budget is not None:
            self.budget.reserve_request()

        result = handle_prescription_request(
            patient_id_input=patient_id,
            to_email=target_email,
            patient_name=self.caller.full_name,
            medication=medication,
        )

        if self.record_calls:
            record_call_to_excel(
                first_name=self.caller.first_name,
                last_name=self.caller.last_name,
                email=target_email,
                mobile=self.caller.mobile,
                intake_reason="Prescription Request",
                request_label="Prescription Request",
                patient_id=str(patient_id) if result.get("verified") else "",
                call_summary=self._record_summary(
                    str(result.get("message", "")), "Prescription Request"
                ),
                status="Prescription Sent" if result.get("verified") else "Unverified ID",
                file_path=self._excel_path(),
            )
            self._recorded_labels.append("Prescription Request")

        if not result.get("verified") and not result.get("success"):
            result.setdefault(
                "message",
                (
                    "I could not verify that patient ID, so I have not sent a prescription. "
                    "The practice will need to confirm your identity."
                ),
            )
        return result, str(result.get("message", ""))

    def lookup_appointment(self, args: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """Read this caller's stored appointments instead of guessing."""
        reference = str(args.get("reference", "")).strip().upper()
        entries = find_appointment(
            reference=reference,
            first_name="" if reference else self.caller.first_name,
            ledger_path=self.appointment_ledger,
        )
        if reference:
            entries = [entry for entry in entries if entry.get("reference") == reference]
        else:
            entries = [
                entry
                for entry in entries
                if str(entry.get("last_name", "")).strip().lower()
                in (
                    "",
                    self.caller.last_name.strip().lower(),
                )
            ]

        if not entries:
            return (
                {
                    "success": True,
                    "found": False,
                    "message": (
                        "I don't have an appointment under your name at the moment. "
                        "Would you like me to book one?"
                    ),
                },
                "I don't have an appointment under your name at the moment. "
                "Would you like me to book one?",
            )

        entry = sorted(entries, key=lambda item: str(item.get("appointment_date", "")))[0]
        spoken = (
            f"You have an appointment on {_spoken_date(entry.get('appointment_date', ''))} "
            f"at {_spoken_appointment_time(entry.get('appointment_time', ''))} with "
            f"{entry.get('doctor', 'the practice')}. Your reference is "
            f"{entry.get('reference', '')}."
        )
        return {"success": True, "found": True, "appointment": entry, "message": spoken}, spoken

    def get_clinic_info(self, args: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """Return verified practice facts."""
        topic = str(args.get("topic", "hours")).lower()
        schedule = self.schedule
        facts = {
            "hours": (f"We are open {schedule.describe_hours()}."),
            "address": f"We are at {schedule.address}.",
            "contact": (
                f"You can reach the practice on {schedule.phone}."
                if schedule.phone
                else "The practice reception can take your details and call you back."
            ),
            "services": (
                "We can help with appointments, repeat prescriptions and general "
                "practice questions. For anything clinical, a clinician will call you back."
            ),
        }
        text = facts.get(topic)
        if text is None:
            return (
                {
                    "success": False,
                    "error": "UNKNOWN_TOPIC",
                    "message": "I do not have that detail.",
                },
                "I do not have that detail.",
            )
        return (
            {"success": True, "topic": topic, "answer": text, "clinic": schedule.name},
            text,
        )

    def record_call_summary(self, args: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """Store the outcome of a call in the clinic ledger."""
        summary = str(args.get("summary", "")).strip()
        label = str(args.get("request_label", "")).strip() or "General Inquiry"
        if not summary:
            return (
                {"success": False, "error": "MISSING_SUMMARY", "message": "Nothing to record."},
                "Nothing to record.",
            )
        if self.record_calls:
            record_call_to_excel(
                first_name=self.caller.first_name,
                last_name=self.caller.last_name,
                email=self.caller.email,
                mobile=self.caller.mobile,
                intake_reason=label,
                request_label=label,
                call_summary=self._record_summary(summary, label),
                status="Completed",
                file_path=self._excel_path(),
            )
            self._recorded_labels.append(label)
        return {"success": True, "recorded": True, "request_label": label}, "Noted."

    # -- helpers ----------------------------------------------------------

    def _record_summary(self, message: str, request_label: str) -> str:
        """Keep the intake note in the summary, not in the request column.

        The browser keeps the intake form between calls, so its reason can be
        stale ("appointment" left over from the previous call). The record's
        Intake Reason describes what the call actually handled; the form text
        is preserved here for audit instead of contradicting the action.
        """
        intake = self.caller.reason.strip()
        if intake and intake.lower() not in request_label.lower():
            return f"{message} (Intake form note: {intake})".strip()
        return message

    def _excel_path(self) -> Path | None:
        """Normalise the optional ledger override to a path."""
        if not self.excel_path:
            return None
        return self.excel_path if isinstance(self.excel_path, Path) else Path(self.excel_path)

    def has_recorded(self) -> bool:
        """Whether a tool already wrote a call record for this session."""
        return bool(self._recorded_labels)

    def record_fallback_summary(self, summary: str) -> None:
        """Write one general record when a call ends with no tool outcome."""
        if self.record_calls and not self.has_recorded():
            record_call_to_excel(
                first_name=self.caller.first_name,
                last_name=self.caller.last_name,
                email=self.caller.email,
                mobile=self.caller.mobile,
                intake_reason="General Inquiry",
                request_label="General Inquiry",
                call_summary=self._record_summary(summary, "General Inquiry"),
                status="Completed",
                file_path=self._excel_path(),
            )
            self._recorded_labels.append("General Inquiry")


def _spoken_date(value: str) -> str:
    """Render an ISO date as a spoken date."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%A %d %B")
    except (TypeError, ValueError):
        return value or "the booked day"


def _spoken_appointment_time(value: str) -> str:
    """Render a stored ``HH:MM`` time as a spoken time."""
    try:
        return datetime.strptime(value, "%H:%M").strftime("%I:%M %p").lstrip("0")
    except (TypeError, ValueError):
        return value


def build_system_instruction(
    caller: CallerProfile,
    schedule: ClinicSchedule | None = None,
    now: datetime | None = None,
) -> str:
    """Compose the grounded receptionist instruction.

    Args:
        caller: Intake-form profile for this call.
        schedule: Practice facts; defaults to the environment configuration.
        now: Reference time used to resolve spoken dates.

    Returns:
        The system instruction sent with every model request.
    """
    schedule = schedule or load_clinic_schedule()
    now = now or datetime.now()
    return (
        "You are EMMA, the administrative receptionist for "
        f"{schedule.name}. You are on a live voice call, so speak in short, warm, "
        "natural sentences. No markdown, no bullet points, no lists.\n\n"
        f"Today is {now.strftime('%A %d %B %Y')} and the local time is {now.strftime('%H:%M')}.\n"
        f"Caller: {caller.full_name or 'the caller'}. "
        f"Email on file: {caller.email or 'not provided'}. "
        f"Mobile on file: {caller.mobile or 'not provided'}. "
        f"Reason given at intake (may be out of date; what the caller says on the "
        f"call takes precedence): {caller.reason or 'not provided'}.\n\n"
        f"Practice address: {schedule.address}. Opening hours: {schedule.describe_hours()}.\n"
        f"Practitioners: {', '.join(schedule.doctors)}.\n\n"
        "How you must work:\n"
        "1. APPOINTMENTS. Ask for the day, the time and the reason if they are missing. "
        "Convert relative dates using today's date, then call book_appointment. "
        "Only say an appointment is confirmed after the tool returns success, and always "
        "read back the reference it returns. If the tool reports a conflict or closed "
        "hours, apologize briefly and offer the alternatives it returns. Never invent a "
        "practitioner, a slot or a confirmation.\n"
        "2. PRESCRIPTIONS. Ask for the caller's patient ID. When they give it, call "
        "request_prescription with that ID. Only say the prescription was sent if the "
        "tool confirms verification. The authorised demo patient ID is "
        f"{VALID_PATIENT_ID}; never claim another ID was verified.\n"
        "3. LOOKUPS. If the caller asks whether they already have an appointment, call "
        "lookup_appointment. Never guess whether a booking exists.\n"
        "4. QUESTIONS. Use get_clinic_info for hours, address, contact details and "
        "services. Do not invent practice facts, prices or clinical advice. For anything "
        "clinical, say a clinician will call them back.\n"
        "5. RECORDING. When a call ends without an appointment or prescription, call "
        "record_call_summary so the front desk keeps a record.\n"
        "6. SAFETY. You are not a clinician: never diagnose, never advise on medication "
        "dosage, and give emergency instructions only if the caller describes an emergency, "
        "directing them to call 999.\n"
        "7. BREVITY. One or two sentences per turn. Ask one question at a time."
    )


# ---------------------------------------------------------------------------
# Transcript hygiene
# ---------------------------------------------------------------------------

_ECHO_STOPWORDS = frozenset(
    {"the", "a", "an", "is", "are", "to", "of", "and", "i", "you", "we", "it", "for", "on", "in"}
)


def normalize_text(text: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace.

    Digit/letter boundaries are split so "3PM" and "3 PM" compare equal — the
    two STT paths format times differently and must still be recognised as the
    same caller turn.
    """
    lowered = text.lower()
    lowered = re.sub(r"(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)", " ", lowered)
    return re.sub(r"[^a-z0-9\s]", " ", lowered).strip()


def token_set(text: str) -> set[str]:
    """Content words of a phrase, used for similarity checks."""
    return {word for word in normalize_text(text).split() if word not in _ECHO_STOPWORDS}


def similarity(left: str, right: str) -> float:
    """Jaccard similarity of two phrases' content words (0..1)."""
    a, b = token_set(left), token_set(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def is_echo(transcript: str, recent_assistant_text: str, threshold: float = 0.6) -> bool:
    """Whether a transcript is the assistant hearing its own speech.

    Microphones pick up the laptop speakers even with echo cancellation, and a
    transcription of the assistant's own sentence would otherwise be treated as
    a caller turn — which is how a receptionist ends up answering its own
    greeting on a loop.

    Args:
        transcript: New caller transcript.
        recent_assistant_text: What the assistant last said.
        threshold: Similarity above which the transcript is discarded.

    Returns:
        True when the transcript should be ignored.
    """
    if not transcript.strip() or not recent_assistant_text.strip():
        return False

    spoken = normalize_text(transcript)
    said = normalize_text(recent_assistant_text)
    words = spoken.split()
    if len(words) < 3:
        return False

    if similarity(transcript, recent_assistant_text) >= threshold:
        return True

    # Word spacing differs between the two paths ("QuantumLoopAI" arrives as
    # "Quantum Loop AI"), so compare with all spaces removed: a fragment that
    # is a contiguous piece of the assistant's sentence is echo, not a caller
    # turn. This is deliberately the *only* bag-of-words rule: a looser overlap
    # test rejects real replies such as "Can you help me book an appointment?"
    # simply because EMMA's greeting also said "help" and "today".
    compact_spoken = spoken.replace(" ", "")
    compact_said = said.replace(" ", "")
    if len(compact_spoken) >= 10 and compact_spoken in compact_said:
        return True
    if len(compact_spoken) >= 10 and compact_said in compact_spoken:
        return True
    return False


def is_duplicate_transcript(
    transcript: str,
    recent_user_texts: list[str],
    threshold: float = 0.7,
) -> bool:
    """Whether the same caller turn already arrived over the other STT path.

    Args:
        transcript: New caller transcript.
        recent_user_texts: Recently processed caller turns.
        threshold: Similarity above which the transcript is a duplicate.

    Returns:
        True when the transcript repeats a recent caller turn.
    """
    return any(similarity(transcript, previous) >= threshold for previous in recent_user_texts)

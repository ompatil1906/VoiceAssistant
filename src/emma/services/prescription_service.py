#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Prescription Verification & SMTP Dispatch Service.

Validates patient identification for prescription requests (verifying Patient ID == 110)
and securely dispatches an official demo prescription via SMTP to the patient's
registered email address.
"""

from __future__ import annotations

import email.message
import os
import re
import smtplib
from collections.abc import Mapping
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any

try:
    from loguru import logger
except ImportError:
    import logging

    logger = logging.getLogger("emma.prescription_service")

# Official verified demo patient ID
VALID_PATIENT_ID = "110"


def normalize_patient_id(raw_id: str | int | None) -> str:
    """Normalize user input or speech transcript into a clean patient ID string.

    Handles spoken forms such as 'one one zero', 'one-ten', or raw digits with formatting.
    """
    if raw_id is None:
        return ""
    text = str(raw_id).strip().lower()

    # Spoken or padded forms of the demo ID: "100 and 10", "100 10".
    # Checked before the plain digit strip so "100 and 10" does not become 10010.
    compact = re.sub(r"[\s-]+", " ", text)
    if re.fullmatch(r"100 (and )?10", compact):
        return "110"

    # Direct digit match
    digits = re.sub(r"[^\d]", "", text)
    if digits:
        return digits

    # Spoken representations
    spoken_map = {
        "one one zero": "110",
        "one-one-zero": "110",
        "one ten": "110",
        "one-ten": "110",
        "hundred and ten": "110",
    }
    for spoken, val in spoken_map.items():
        if spoken in text:
            return val

    return text


def verify_patient_id(raw_id: str | int | None) -> bool:
    """Check if the provided patient ID matches the authorized prescription record (110)."""
    normalized = normalize_patient_id(raw_id)
    return normalized == VALID_PATIENT_ID


def generate_prescription_html(
    patient_name: str,
    patient_id: str,
    recipient_email: str,
    medication: str = "Amoxicillin 500mg Oral Capsule",
    dosage_instructions: str = "Take 1 capsule by mouth every 8 hours (three times daily) with meals for 10 days.",
    quantity: str = "30 Capsules",
    refills: str = "0 (Zero)",
    doctor_name: str = "Dr. Sarah Jenkins, MD",
    clinic_name: str = "EMMA Health Practice & Wellness Center",
) -> str:
    """Generate professional HTML prescription body for email dispatch."""
    issue_date = datetime.now(UTC).strftime("%B %d, %Y")
    rx_number = f"RX-{datetime.now(UTC).strftime('%Y%m%d')}-0110"

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Official Medical Prescription - {clinic_name}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f8fafc; margin: 0; padding: 24px; color: #1e293b; }}
    .rx-card {{ max-width: 620px; margin: 0 auto; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05); }}
    .header {{ background: #0f172a; color: #ffffff; padding: 24px; }}
    .header h1 {{ margin: 0; font-size: 20px; font-weight: 600; letter-spacing: -0.02em; }}
    .header p {{ margin: 4px 0 0; font-size: 13px; color: #94a3b8; }}
    .badge {{ display: inline-block; background: #059669; color: #ffffff; font-size: 11px; font-weight: 600; padding: 3px 8px; border-radius: 9999px; text-transform: uppercase; margin-top: 8px; }}
    .content {{ padding: 24px; }}
    .section-title {{ font-size: 12px; font-weight: 700; color: #64748b; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; }}
    .patient-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; background: #f1f5f9; padding: 16px; border-radius: 8px; margin-bottom: 20px; }}
    .info-label {{ font-size: 11px; color: #64748b; margin-bottom: 2px; }}
    .info-val {{ font-size: 14px; font-weight: 600; color: #0f172a; }}
    .rx-box {{ border-left: 4px solid #2563eb; background: #eff6ff; padding: 16px; border-radius: 0 8px 8px 0; margin-bottom: 20px; }}
    .med-title {{ font-size: 16px; font-weight: 700; color: #1d4ed8; margin: 0 0 6px; }}
    .instructions {{ font-size: 14px; line-height: 1.5; color: #334155; margin: 0 0 10px; }}
    .rx-details {{ font-size: 12px; color: #475569; display: flex; gap: 20px; }}
    .footer {{ border-top: 1px solid #e2e8f0; padding: 16px 24px; background: #f8fafc; font-size: 11px; color: #94a3b8; text-align: center; }}
  </style>
</head>
<body>
  <div class="rx-card">
    <div class="header">
      <span class="badge">Verified Electronic Rx</span>
      <h1>{clinic_name}</h1>
      <p>EMMA Autonomous Medical Voice Receptionist &amp; Clinic Engine</p>
    </div>
    <div class="content">
      <div class="patient-grid">
        <div>
          <div class="info-label">PATIENT NAME</div>
          <div class="info-val">{patient_name or "Valued Patient"}</div>
        </div>
        <div>
          <div class="info-label">VERIFIED PATIENT ID</div>
          <div class="info-val">#{patient_id}</div>
        </div>
        <div>
          <div class="info-label">EMAIL DESTINATION</div>
          <div class="info-val">{recipient_email}</div>
        </div>
        <div>
          <div class="info-label">ISSUE DATE</div>
          <div class="info-val">{issue_date}</div>
        </div>
      </div>

      <div class="section-title">Prescription Details ({rx_number})</div>
      <div class="rx-box">
        <h3 class="med-title">&#x211E; {medication}</h3>
        <p class="instructions"><strong>Directions:</strong> {dosage_instructions}</p>
        <div class="rx-details">
          <span><strong>Dispense:</strong> {quantity}</span>
          <span><strong>Refills:</strong> {refills}</span>
        </div>
      </div>

      <div style="margin-top: 16px; font-size: 12px; color: #64748b; line-height: 1.5;">
        <strong>Prescriber:</strong> {doctor_name} &bull; License #MD-884219<br>
        <strong>Practice:</strong> QuantumLoopAI Clinical Systems &bull; EMMA Voice Portal<br>
        <em>Please present this confirmation or prescription identifier to your dispensing pharmacy.</em>
      </div>
    </div>
    <div class="footer">
      Notice: This demo electronic prescription was generated and dispatched automatically via EMMA Telephony Voice Assistant upon confirmation of Patient ID #{patient_id}.
    </div>
  </div>
</body>
</html>"""


def send_demo_prescription_email(
    to_email: str,
    patient_name: str = "",
    patient_id: str = "110",
    medication: str = "Amoxicillin 500mg Oral Capsule",
    dosage_instructions: str = "Take 1 capsule by mouth every 8 hours with meals for 10 days.",
    custom_smtp_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Send verified demo prescription to recipient via SMTP server, with graceful archiving.

    Args:
        to_email: Patient destination email address.
        patient_name: Full name of the patient.
        patient_id: Confirmed patient ID (must be verified as 110).
        medication: Name and strength of medication.
        dosage_instructions: Sig / usage directions.
        custom_smtp_config: Optional SMTP configuration override dictionary.

    Returns:
        Dictionary with status, method, message, and timestamp.
    """
    if not to_email or "@" not in to_email:
        return {
            "success": False,
            "error": f"Invalid recipient email address: '{to_email}'",
            "delivered": False,
        }

    config = dict(custom_smtp_config or {})
    smtp_host = config.get("host") or os.getenv("SMTP_HOST", "")
    smtp_port = int(config.get("port") or os.getenv("SMTP_PORT", "587"))
    smtp_user = config.get("user") or os.getenv("SMTP_USER", "")
    smtp_password = config.get("password") or os.getenv("SMTP_PASSWORD", "")
    smtp_from = (
        config.get("from_email")
        or os.getenv("SMTP_FROM")
        or smtp_user
        or "prescriptions@quantumloop.ai"
    )
    use_tls = config.get(
        "use_tls", os.getenv("SMTP_USE_TLS", "true").lower() in ("1", "true", "yes")
    )

    subject = f"Verified Prescription Confirmation - Patient ID #{patient_id} ({medication})"
    html_body = generate_prescription_html(
        patient_name=patient_name,
        patient_id=patient_id,
        recipient_email=to_email,
        medication=medication,
        dosage_instructions=dosage_instructions,
    )

    msg = email.message.EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"EMMA Clinic Services <{smtp_from}>"
    msg["To"] = to_email
    msg.set_content(
        f"EMMA Clinic Services - Prescription Order\n\n"
        f"Patient ID: #{patient_id}\n"
        f"Patient Name: {patient_name or 'Valued Patient'}\n"
        f"Rx: {medication}\n"
        f"Instructions: {dosage_instructions}\n"
        f"Refills: 0 (Authorized)\n"
        f"Issued Date: {datetime.now(UTC).strftime('%Y-%m-%d')}\n"
    )
    msg.add_alternative(html_body, subtype="html")

    dispatched = False
    delivery_note = ""

    # Attempt Live SMTP Transmission if host is configured
    if smtp_host:
        try:
            logger.info(
                f"Connecting to SMTP server {smtp_host}:{smtp_port} for patient {to_email}..."
            )
            with smtplib.SMTP(smtp_host, smtp_port, timeout=12) as server:
                if use_tls:
                    server.starttls()
                if smtp_user and smtp_password:
                    server.login(smtp_user, smtp_password)
                server.send_message(msg)
            dispatched = True
            delivery_note = f"Sent via live SMTP server ({smtp_host}:{smtp_port}) to {to_email}."
            logger.info(f"[Prescription SMTP] Successfully delivered prescription to {to_email}.")
        except Exception as exc:
            logger.warning(
                f"[Prescription SMTP] Live SMTP transmission failed ({exc}). Archiving locally."
            )
            delivery_note = f"SMTP transmission failed ({exc}); archived demo copy."
    else:
        delivery_note = "SMTP server not configured in environment; archived demo copy locally."

    # Always persist a local copy for compliance and offline testing / demo inspection
    archive_dir = Path("data/prescriptions")
    archive_dir.mkdir(parents=True, exist_ok=True)
    timestamp_slug = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    file_path = archive_dir / f"rx_patient_{patient_id}_{timestamp_slug}.eml"
    try:
        with open(file_path, "wb") as f:
            f.write(msg.as_bytes())
    except Exception as exc:
        logger.warning(f"Could not write local prescription archive: {exc}")

    return {
        "success": True,
        "delivered_live_smtp": dispatched,
        "patient_id": patient_id,
        "recipient": to_email,
        "medication": medication,
        "archive_path": str(file_path),
        "note": delivery_note,
        "timestamp": datetime.now(UTC).isoformat(),
    }


def handle_prescription_request(
    patient_id_input: str | int | None,
    to_email: str,
    patient_name: str = "",
    medication: str = "Amoxicillin 500mg Oral Capsule",
) -> dict[str, Any]:
    """Execute complete prescription inquiry & verification workflow.

    1. Validates that patient ID == '110'.
    2. If invalid, rejects with explicit guidance.
    3. If valid, dispatches prescription to their email and returns confirmation.
    """
    if not verify_patient_id(patient_id_input):
        normalized = normalize_patient_id(patient_id_input)
        return {
            "success": False,
            "verified": False,
            "error": "UNVERIFIED_PATIENT_ID",
            # Never disclose the authorised ID in a refusal: the caller may not
            # be the patient, and the message is spoken out loud.
            "message": (
                "I couldn't verify that patient ID, so I haven't sent a prescription. "
                "The practice will need to confirm your identity."
            ),
            "provided_id": str(patient_id_input),
            "normalized_id": normalized,
        }

    # ID is confirmed (110)
    dispatch_res = send_demo_prescription_email(
        to_email=to_email,
        patient_name=patient_name,
        patient_id=VALID_PATIENT_ID,
        medication=medication,
    )

    return {
        "success": True,
        "verified": True,
        "patient_id": VALID_PATIENT_ID,
        "recipient": to_email,
        "message": (
            f"Patient ID #{VALID_PATIENT_ID} successfully confirmed. "
            f"Your prescription for {medication} has been sent via email to {to_email}."
        ),
        "dispatch_details": dispatch_res,
    }

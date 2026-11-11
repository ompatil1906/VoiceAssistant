#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Excel Call Records & Intake Form Storage.

Stores caller filled form details, conversation summary, request label,
and appointment date & time directly into an Excel workbook (call_records.xlsx)
and a synchronized CSV mirror.

Uses zero external dependencies: constructs valid ISO/IEC 29500 OpenXML (.xlsx)
workbooks using Python's standard zipfile and XML generation, with optional
openpyxl acceleration if available.
"""

from __future__ import annotations

import csv
import datetime
import io
import os
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

try:
    from loguru import logger
except ImportError:
    import logging
    logger = logging.getLogger("emma.storage")

EXCEL_RECORDS_DIR = Path(os.environ.get("CALL_RECORDS_DIR", "data"))
EXCEL_RECORDS_PATH = EXCEL_RECORDS_DIR / "call_records.xlsx"
CSV_RECORDS_PATH = EXCEL_RECORDS_DIR / "call_records.csv"

COLUMNS = [
    "Timestamp",
    "First Name",
    "Last Name",
    "Email",
    "Mobile Number",
    "Intake Reason",
    "Request Label",
    "Appointment Date",
    "Appointment Time",
    "Patient ID",
    "Call Summary",
    "Status",
]


@dataclass
class CallRecord:
    """A single call record with intake form and appointment details."""

    first_name: str
    last_name: str
    email: str
    mobile: str
    intake_reason: str
    call_summary: str
    request_label: str  # e.g., 'Appointment', 'Prescription', 'General Inquiry'
    appointment_date: str = ""
    appointment_time: str = ""
    patient_id: str = ""
    status: str = "Completed"
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def to_row(self) -> list[str]:
        return [
            self.timestamp,
            self.first_name,
            self.last_name,
            self.email,
            self.mobile,
            self.intake_reason,
            self.request_label,
            self.appointment_date,
            self.appointment_time,
            self.patient_id,
            self.call_summary,
            self.status,
        ]


CallIntakeRecord = CallRecord


def _build_openxml_xlsx(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> bytes:
    """Build a valid .xlsx file in memory using standard library zipfile."""
    sheet_xml: list[str] = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        "<sheetData>",
    ]

    # Header Row
    sheet_xml.append('<row r="1">')
    for col_idx, h in enumerate(headers, 1):
        col_letter = chr(64 + col_idx) if col_idx <= 26 else f"A{chr(64 + col_idx - 26)}"
        clean_val = str(h).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        sheet_xml.append(f'<c r="{col_letter}1" t="inlineStr"><is><t>{clean_val}</t></is></c>')
    sheet_xml.append("</row>")

    # Data Rows
    for row_idx, row in enumerate(rows, 2):
        sheet_xml.append(f'<row r="{row_idx}">')
        for col_idx, val in enumerate(row, 1):
            col_letter = chr(64 + col_idx) if col_idx <= 26 else f"A{chr(64 + col_idx - 26)}"
            clean_val = str(val or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            sheet_xml.append(f'<c r="{col_letter}{row_idx}" t="inlineStr"><is><t>{clean_val}</t></is></c>')
        sheet_xml.append("</row>")

    sheet_xml.append("</sheetData></worksheet>")

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        '  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        '  <Default Extension="xml" ContentType="application/xml"/>\n'
        '  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>\n'
        '  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>\n'
        "</Types>"
    )

    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        '  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>\n'
        "</Relationships>"
    )

    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        '  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>\n'
        "</Relationships>"
    )

    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">\n'
        "  <sheets>\n"
        '    <sheet name="Call Records" sheetId="1" r:id="rId1"/>\n'
        "  </sheets>\n"
        "</workbook>"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/worksheets/sheet1.xml", "".join(sheet_xml))

    return buf.getvalue()


def record_call_to_excel(
    *,
    first_name: str,
    last_name: str = "",
    email: str = "",
    mobile: str = "",
    mobile_number: str = "",
    intake_reason: str = "",
    call_summary: str = "",
    request_label: str = "General Inquiry",
    appointment_date: str = "",
    appointment_time: str = "",
    patient_id: str = "",
    status: str = "Completed",
    file_path: Path | None = None,
) -> Path:
    """Record a caller interaction and intake form details into an Excel form.

    Args:
        first_name: Caller first name from intake form.
        last_name: Caller last name from intake form.
        email: Caller email address.
        mobile: Caller phone number.
        intake_reason: Reason stated in intake form.
        call_summary: Generated or conversation summary.
        request_label: Request type (e.g. Appointment, Prescription, Refill).
        appointment_date: Date string if appointment was booked (e.g. '2026-11-15').
        appointment_time: Time string if appointment was booked (e.g. '10:30 AM').
        patient_id: Confirmed patient ID (e.g. '110').
        status: Call and action status.
        file_path: Override path to .xlsx file.

    Returns:
        Path to the updated .xlsx file.
    """
    target_xlsx = file_path or EXCEL_RECORDS_PATH
    target_dir = target_xlsx.parent
    target_dir.mkdir(parents=True, exist_ok=True)

    resolved_mobile = mobile_number or mobile
    record = CallRecord(
        first_name=first_name,
        last_name=last_name,
        email=email,
        mobile=resolved_mobile,
        intake_reason=intake_reason,
        call_summary=call_summary,
        request_label=request_label,
        appointment_date=appointment_date,
        appointment_time=appointment_time,
        patient_id=patient_id,
        status=status,
    )

    # 1. Update CSV mirror
    csv_path = target_dir / (target_xlsx.stem + ".csv")
    existing_rows: list[list[str]] = []

    if csv_path.exists():
        try:
            with csv_path.open("r", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                for r in reader:
                    if r:
                        existing_rows.append(r)
        except Exception as e:
            logger.warning(f"Failed reading existing CSV records: {e}")

    existing_rows.append(record.to_row())

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(COLUMNS)
        writer.writerows(existing_rows)

    # 2. Update XLSX binary
    xlsx_bytes = _build_openxml_xlsx(COLUMNS, existing_rows)
    target_xlsx.write_bytes(xlsx_bytes)

    logger.info(
        f"📊 Saved call record to Excel: {target_xlsx} [Total records: {len(existing_rows)}] "
        f"(Caller: {first_name} {last_name}, Label: {request_label}, Appt: {appointment_date} {appointment_time})"
    )
    return target_xlsx

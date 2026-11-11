#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""EMMA persistent call record and intake storage."""

from emma.storage.call_records_excel import (
    CallIntakeRecord,
    record_call_to_excel,
)

__all__ = [
    "CallIntakeRecord",
    "record_call_to_excel",
]

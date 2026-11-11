#
# Copyright (c) 2024-2026, Daily
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
# Inherited from an upstream BSD 2-Clause licensed voice framework and
# modified for EMMA Receptionist by QuantumLoopAI.
#


"""Local (in-process) bus implementations."""

from emma.runtime.bus.local.async_queue import AsyncQueueBus

__all__ = ["AsyncQueueBus"]

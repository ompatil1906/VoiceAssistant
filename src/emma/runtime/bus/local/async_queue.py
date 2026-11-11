#
# Copyright (c) 2024-2026, Daily
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
# Inherited from an upstream BSD 2-Clause licensed voice framework and
# modified for EMMA Receptionist by QuantumLoopAI.
#


"""In-process worker bus backed by asyncio queues."""

from loguru import logger

from emma.runtime.bus.bus import WorkerBus
from emma.runtime.bus.messages import BusMessage


class AsyncQueueBus(WorkerBus):
    """In-process bus that delivers messages via priority queues."""

    async def publish(self, message: BusMessage) -> None:
        """Deliver a message to all local subscriber queues.

        Args:
            message: The bus message to deliver.
        """
        logger.trace(f"{self}: sending {message}")
        self.on_message_received(message)

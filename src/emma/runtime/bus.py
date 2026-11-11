#
# Copyright (c) 2024-2026, Daily
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
# Inherited from an upstream BSD 2-Clause licensed voice framework and
# modified for EMMA Receptionist by QuantumLoopAI.
#


"""Worker bus package -- pub/sub messaging between workers and the runner.

Provides the pub/sub infrastructure that connects workers to each other and to
the runner. Key components:

- `WorkerBus` -- abstract base class defining the send/receive interface.
- `AsyncQueueBus` -- in-process implementation backed by ``asyncio.Queue``.
- `BusBridgeProcessor` -- bidirectional mid-pipeline bridge for
  transport/session workers that exchanges frames with other workers
  through the bus.
- `BusMessage` and its subclasses -- the typed message hierarchy used for
  worker lifecycle events (activation, cancellation, shutdown), job
  coordination, and frame transport.
"""

from emma.runtime.bus.bridge_processor import BusBridgeProcessor
from emma.runtime.bus.bus import WorkerBus
from emma.runtime.bus.local import AsyncQueueBus
from emma.runtime.bus.messages import BusActivateWorkerMessage, BusAddWorkerMessage, BusCancelMessage, BusCancelWorkerMessage, BusDataMessage, BusDeactivateWorkerMessage, BusEndMessage, BusEndWorkerMessage, BusFlushProgressMessage, BusFrameMessage, BusJobCancelMessage, BusJobRequestMessage, BusJobResponseMessage, BusJobResponseUrgentMessage, BusJobStreamDataMessage, BusJobStreamEndMessage, BusJobStreamStartMessage, BusJobUpdateMessage, BusJobUpdateRequestMessage, BusJobUpdateUrgentMessage, BusLocalMessage, BusMessage, BusSystemMessage, BusTTSSpeakMessage, BusWorkerErrorMessage, BusWorkerLocalErrorMessage, BusWorkerReadyMessage, BusWorkerRegistryMessage
    BusActivateWorkerMessage,
    BusAddWorkerMessage,
    BusCancelMessage,
    BusCancelWorkerMessage,
    BusDataMessage,
    BusDeactivateWorkerMessage,
    BusEndMessage,
    BusEndWorkerMessage,
    BusFlushProgressMessage,
    BusFrameMessage,
    BusJobCancelMessage,
    BusJobRequestMessage,
    BusJobResponseMessage,
    BusJobResponseUrgentMessage,
    BusJobStreamDataMessage,
    BusJobStreamEndMessage,
    BusJobStreamStartMessage,
    BusJobUpdateMessage,
    BusJobUpdateRequestMessage,
    BusJobUpdateUrgentMessage,
    BusLocalMessage,
    BusMessage,
    BusSystemMessage,
    BusTTSSpeakMessage,
    BusWorkerErrorMessage,
    BusWorkerLocalErrorMessage,
    BusWorkerReadyMessage,
    BusWorkerRegistryMessage,
)
from emma.runtime.bus.subscriber import BusSubscriber
from emma.runtime.registry.types import WorkerRegistryEntry

__all__ = [
    "WorkerBus",
    "AsyncQueueBus",
    "BusActivateWorkerMessage",
    "BusAddWorkerMessage",
    "BusWorkerErrorMessage",
    "BusWorkerLocalErrorMessage",
    "WorkerRegistryEntry",
    "BusWorkerReadyMessage",
    "BusWorkerRegistryMessage",
    "BusBridgeProcessor",
    "BusCancelWorkerMessage",
    "BusCancelMessage",
    "BusDeactivateWorkerMessage",
    "BusEndWorkerMessage",
    "BusEndMessage",
    "BusFlushProgressMessage",
    "BusFrameMessage",
    "BusDataMessage",
    "BusLocalMessage",
    "BusMessage",
    "BusSubscriber",
    "BusSystemMessage",
    "BusTTSSpeakMessage",
    "BusJobCancelMessage",
    "BusJobRequestMessage",
    "BusJobResponseMessage",
    "BusJobResponseUrgentMessage",
    "BusJobStreamDataMessage",
    "BusJobStreamEndMessage",
    "BusJobStreamStartMessage",
    "BusJobUpdateMessage",
    "BusJobUpdateRequestMessage",
    "BusJobUpdateUrgentMessage",
]

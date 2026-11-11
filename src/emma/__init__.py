#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""EMMA Receptionist runtime — QuantumLoopAI's first-party voice agent engine.

The runtime is a frame-based pipeline: :class:`emma.runtime.processor.FrameProcessor`
instances are chained by :class:`emma.runtime.pipeline.pipeline.Pipeline`, and a
single :class:`emma.runtime.worker.PipelineWorker` owns one conversation, started
by :class:`emma.runtime.runner.WorkerRunner`.

Building blocks:

- ``emma.runtime`` — engine, pipeline, lifecycle.
- ``emma.audio`` — resampling, filtering, mixing, buffering.
- ``emma.speech`` — VAD, turn detection, speech-to-text, text-to-speech.
- ``emma.intelligence`` — LLM services, conversation context, tool calling.
- ``emma.transports`` — LiveKit and WebSocket transports, RTVI protocol.
- ``emma.cost`` — provider cost classes, budget enforcement, usage ledger.
- ``emma.security`` — data-policy gate and healthcare log redaction.
- ``emma.observability`` — frame metrics and latency observers.

Parts of this package are inherited from an open-source real-time voice
framework, copyright Daily, distributed under the BSD 2-Clause License. See
``NOTICE`` and ``THIRD_PARTY_NOTICES/`` for the attribution record.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version

import asyncio
import sys

_DISTRIBUTION = "emma-receptionist"
_FALLBACK_VERSION = "0.1.0.dev0"


def _detect_version() -> str:
    """Return the installed distribution version, or the development fallback."""
    try:
        return _distribution_version(_DISTRIBUTION)
    except PackageNotFoundError:
        return _FALLBACK_VERSION


__version__ = _detect_version()


__all__ = [
    "version",
    "RealCallSession",
    "build_real_call_pipeline",
]


def version() -> str:
    """Return the EMMA runtime version."""
    return __version__


def __getattr__(name: str):
    if name in ("RealCallSession", "build_real_call_pipeline"):
        from emma.runtime.real_call import RealCallSession, build_real_call_pipeline
        return locals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Python 3.12 reimplemented ``asyncio.wait_for`` on top of ``asyncio.timeout`` and
# fixed several cancellation bugs that were never backported. On 3.11 the
# ``wait_for2`` package provides that implementation; without it, cancelling a
# processor can leave a task waiting on a timeout that never fires.
if sys.version_info < (3, 12):  # pragma: no cover - version dependent
    try:
        import wait_for2

        asyncio.wait_for = wait_for2.wait_for
    except ImportError:
        pass

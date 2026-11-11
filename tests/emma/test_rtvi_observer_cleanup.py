 
# Copyright (c) 2026, QuantumLoopAI
# Modified for EMMA Receptionist by QuantumLoopAI.

"""Tests for RTVIObserver cleanup."""

import unittest

from loguru import logger

from emma.transports.rtvi.observer import RTVIObserver, RTVIObserverParams


class TestRTVIObserverCleanup(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_can_run_twice_with_system_logs(self):
        # An observer removed from a worker and added back is cleaned up on
        # removal and again at shutdown.
        observer = RTVIObserver(params=RTVIObserverParams(system_logs_enabled=True))
        sink_id = observer._system_logger_id

        await observer.cleanup()
        await observer.cleanup()

        with self.assertRaises(ValueError):
            logger.remove(sink_id)
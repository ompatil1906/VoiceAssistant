 
# Copyright (c) 2026, QuantumLoopAI
# Modified for EMMA Receptionist by QuantumLoopAI.

"""User turn start strategy based on VAD events."""

from emma.runtime.frames import Frame, VADUserStartedSpeakingFrame
from emma.turns.user_start.base_user_turn_start_strategy import BaseUserTurnStartStrategy


class VADUserTurnStartStrategy(BaseUserTurnStartStrategy):
    """User turn start strategy based on VAD (Voice Activity Detection).

    This strategy assumes the user turn starts as soon as a VAD frame indicates
    that the user has started speaking.

    """

    async def process_frame(self, frame: Frame):
        """Process an incoming frame to detect user turn start.

        Args:
            frame: The frame to be analyzed.
        """
        if isinstance(frame, VADUserStartedSpeakingFrame):
            await self.report_user_turn_started()
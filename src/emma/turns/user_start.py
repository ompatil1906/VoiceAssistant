 
# Copyright (c) 2026, QuantumLoopAI
# Modified for EMMA Receptionist by QuantumLoopAI.

from emma.turns.user_start.base_user_turn_start_strategy import BaseUserTurnStartStrategy, UserTurnStartedParams
from emma.turns.user_start.external_user_turn_start_strategy import ExternalUserTurnStartStrategy
from emma.turns.user_start.min_words_user_turn_start_strategy import MinWordsUserTurnStartStrategy
from emma.turns.user_start.transcription_user_turn_start_strategy import TranscriptionUserTurnStartStrategy
from emma.turns.user_start.vad_user_turn_start_strategy import VADUserTurnStartStrategy
from emma.turns.user_start.wake_phrase_user_turn_start_strategy import WakePhraseUserTurnStartStrategy

try:
    from emma.turns.user_start.krisp_viva_ip_user_turn_start_strategy import KrispVivaIPUserTurnStartStrategy
except ImportError:
    KrispVivaIPUserTurnStartStrategy = None  # krisp_audio not installed

__all__ = [
    "BaseUserTurnStartStrategy",
    "ExternalUserTurnStartStrategy",
    "KrispVivaIPUserTurnStartStrategy",
    "MinWordsUserTurnStartStrategy",
    "TranscriptionUserTurnStartStrategy",
    "UserTurnStartedParams",
    "VADUserTurnStartStrategy",
    "WakePhraseUserTurnStartStrategy",
]
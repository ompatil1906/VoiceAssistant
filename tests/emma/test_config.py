#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Configuration tests: the default configuration cannot bill and cannot leak."""

import pytest

from emma.config import ConfigError, ProviderProfile, RuntimeConfig
from emma.cost.policy import CostClass, ProviderDenied
from emma.security.data_policy import DataMode, DataPolicyViolation


def test_defaults_are_local_and_synthetic(monkeypatch):
    for name in list(
        {
            "EMMA_PROVIDER_PROFILE",
            "EMMA_PROVIDERS",
            "EMMA_DATA_MODE",
            "EMMA_ALLOW_PAID",
            "EMMA_ALLOW_TRIAL",
            "EMMA_LLM_PROVIDER",
            "EMMA_STT_PROVIDER",
            "EMMA_TTS_PROVIDER",
        }
    ):
        monkeypatch.delenv(name, raising=False)
    config = RuntimeConfig.from_env({})
    assert config.profile is ProviderProfile.LOCAL
    assert config.data_mode is DataMode.SYNTHETIC
    assert config.allow_paid is False
    assert config.limits.max_usd == 0.0
    assert CostClass.PAID not in config.cost_policy().allowed


def test_local_profile_resolves_without_credentials():
    plan = RuntimeConfig.from_env({}).resolve_plan()
    assert plan.llm == "ollama"
    assert plan.stt == "whisper"
    assert plan.tts == "kokoro"
    assert plan.vad == "silero_vad"
    # No LiveKit credentials, so the plan falls back to the transport that needs
    # no external service.
    assert plan.transport == "websocket"


def test_free_profile_in_synthetic_mode_is_refused():
    env = {"EMMA_PROVIDER_PROFILE": "free", "GEMINI_API_KEY": "test-key"}
    config = RuntimeConfig.from_env(env)
    assert config.profile is ProviderProfile.FREE
    with pytest.raises(DataPolicyViolation):
        config.resolve_plan()


def test_free_profile_with_approval_resolves_to_gemini():
    env = {
        "EMMA_PROVIDER_PROFILE": "free",
        "EMMA_DATA_MODE": "approved",
        "GEMINI_API_KEY": "test-key",
        "LIVEKIT_URL": "ws://localhost:7880",
        "LIVEKIT_API_KEY": "key",
    }
    plan = RuntimeConfig.from_env(env).resolve_plan()
    assert plan.llm == "gemini"
    assert plan.stt == "gemini"


def test_missing_gemini_key_is_a_config_error():
    env = {"EMMA_PROVIDER_PROFILE": "free", "EMMA_DATA_MODE": "approved"}
    with pytest.raises(ConfigError) as excinfo:
        RuntimeConfig.from_env(env).resolve_plan()
    assert "GEMINI_API_KEY" in str(excinfo.value)


def test_unknown_profile_is_a_config_error():
    with pytest.raises(ConfigError):
        RuntimeConfig.from_env({"EMMA_PROVIDER_PROFILE": "yolo"})


def test_custom_profile_requires_a_provider_list():
    with pytest.raises(ConfigError):
        RuntimeConfig.from_env({"EMMA_PROVIDER_PROFILE": "custom"})


def test_provider_outside_profile_is_refused():
    env = {"EMMA_PROVIDER_PROFILE": "local", "EMMA_LLM_PROVIDER": "gemini"}
    with pytest.raises(ConfigError):
        RuntimeConfig.from_env(env).resolve_plan()


def test_paid_provider_is_refused_even_when_selected():
    env = {
        "EMMA_PROVIDER_PROFILE": "custom",
        "EMMA_PROVIDERS": "kokoro,elevenlabs",
        "EMMA_LLM_PROVIDER": "kokoro",
        "EMMA_STT_PROVIDER": "kokoro",
        "EMMA_TTS_PROVIDER": "elevenlabs",
    }
    with pytest.raises(ProviderDenied) as excinfo:
        RuntimeConfig.from_env(env).resolve_plan()
    assert "paid" in str(excinfo.value)


def test_describe_never_contains_secret_values():
    env = {
        "GEMINI_API_KEY": "super-secret-value",
        "LIVEKIT_API_KEY": "lk-secret",
        "LIVEKIT_URL": "ws://localhost:7880",
    }
    described = RuntimeConfig.from_env(env).describe()
    rendered = repr(described)
    assert "super-secret-value" not in rendered
    assert "lk-secret" not in rendered
    assert described["credentials_present"] == [
        "GEMINI_API_KEY",
        "LIVEKIT_API_KEY",
        "LIVEKIT_URL",
    ]


def test_websocket_transport_needs_no_livekit_credentials():
    env = {"EMMA_TRANSPORT": "websocket"}
    assert RuntimeConfig.from_env(env).resolve_plan().transport == "websocket"


def test_livekit_is_selected_when_credentials_exist():
    env = {"LIVEKIT_URL": "ws://localhost:7880", "LIVEKIT_API_KEY": "key"}
    assert RuntimeConfig.from_env(env).resolve_plan().transport == "livekit"


def test_explicit_livekit_without_credentials_is_refused():
    env = {"EMMA_TRANSPORT": "livekit"}
    with pytest.raises(ConfigError):
        RuntimeConfig.from_env(env).resolve_plan()

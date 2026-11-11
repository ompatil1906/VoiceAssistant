#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Data-policy tests: patient data does not reach providers that train on inputs."""

import pytest

from emma.security.data_policy import DataMode, DataPolicy, DataPolicyViolation


def test_synthetic_mode_refuses_providers_that_train_on_input():
    policy = DataPolicy(DataMode.SYNTHETIC)
    with pytest.raises(DataPolicyViolation) as excinfo:
        policy.assert_allowed("gemini")
    assert "EMMA_DATA_MODE=synthetic" in str(excinfo.value)


def test_synthetic_mode_allows_local_providers():
    policy = DataPolicy(DataMode.SYNTHETIC)
    for provider in ("kokoro", "whisper", "ollama", "silero_vad"):
        policy.assert_allowed(provider)


def test_approved_mode_allows_a_signed_off_provider():
    policy = DataPolicy(DataMode.APPROVED)
    policy.assert_allowed("gemini")
    assert policy.is_allowed("gemini")


def test_unknown_provider_is_treated_as_permissive():
    policy = DataPolicy(DataMode.SYNTHETIC)
    assert not policy.is_allowed("mystery-asr")
    with pytest.raises(DataPolicyViolation):
        policy.assert_allowed("mystery-asr")


def test_synthetic_mode_is_the_default():
    assert DataPolicy().mode is DataMode.SYNTHETIC

#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Cost policy tests: unknown providers are billable, paid is denied by default."""

import pytest

from emma.cost.policy import (
    DEFAULT_ALLOWED,
    CostClass,
    CostPolicy,
    ProviderDenied,
)


def test_local_providers_are_allowed_by_default():
    policy = CostPolicy()
    assert policy.assert_allowed("kokoro").cost_class is CostClass.LOCAL
    assert policy.assert_allowed("ollama").cost_class is CostClass.LOCAL


def test_default_policy_includes_free_tier_but_not_paid():
    assert DEFAULT_ALLOWED == frozenset({CostClass.LOCAL, CostClass.FREE_TIER})
    assert CostPolicy().is_allowed("gemini")
    assert not CostPolicy().is_allowed("elevenlabs")


def test_unknown_provider_is_treated_as_paid():
    policy = CostPolicy()
    description = policy.describe("some-new-vendor")
    assert description.cost_class is CostClass.PAID
    assert description.trains_on_input is True
    with pytest.raises(ProviderDenied):
        policy.assert_allowed("some-new-vendor")


def test_paid_is_denied_unless_explicitly_enabled():
    with pytest.raises(ProviderDenied) as excinfo:
        CostPolicy().assert_allowed("elevenlabs")
    assert "paid" in str(excinfo.value)

    allowed = CostPolicy(allowed=frozenset({CostClass.LOCAL, CostClass.PAID}))
    assert allowed.assert_allowed("elevenlabs").cost_class is CostClass.PAID


def test_trial_providers_need_explicit_opt_in():
    with pytest.raises(ProviderDenied):
        CostPolicy().assert_allowed("deepgram")
    with_trial = CostPolicy(allowed=DEFAULT_ALLOWED | {CostClass.TRIAL})
    assert with_trial.assert_allowed("deepgram").cost_class is CostClass.TRIAL


def test_local_only_policy_denies_every_remote_provider():
    policy = CostPolicy(allowed=frozenset({CostClass.LOCAL}))
    for provider in ("gemini", "deepgram", "elevenlabs"):
        with pytest.raises(ProviderDenied):
            policy.assert_allowed(provider)

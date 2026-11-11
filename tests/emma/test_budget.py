#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Session budget tests: ceilings stop work instead of overspending."""

import pytest

from emma.cost.budget import LOCAL_ONLY_BUDGET_LIMITS, BudgetLimits, SessionBudget
from emma.cost.policy import QuotaExhausted


def test_request_allowance_is_enforced():
    budget = SessionBudget(BudgetLimits(max_requests=2))
    budget.reserve_request()
    budget.reserve_request()
    with pytest.raises(QuotaExhausted):
        budget.reserve_request()
    assert budget.usage["requests"] == 2


def test_zero_request_allowance_denies_the_first_call():
    budget = SessionBudget(LOCAL_ONLY_BUDGET_LIMITS)
    with pytest.raises(QuotaExhausted):
        budget.reserve_request()


def test_token_allowance_is_enforced():
    budget = SessionBudget(BudgetLimits(max_tokens=100))
    budget.record_tokens(60)
    with pytest.raises(QuotaExhausted):
        budget.record_tokens(60)
    assert budget.usage["tokens"] == 120


def test_spend_ceiling_defaults_to_zero():
    budget = SessionBudget(BudgetLimits())
    with pytest.raises(QuotaExhausted):
        budget.record_spend(0.01, unit_price_known=True)


def test_spend_requires_a_known_unit_price():
    budget = SessionBudget(BudgetLimits(max_usd=10.0))
    with pytest.raises(ValueError):
        budget.record_spend(0.5, unit_price_known=False)


def test_unlimited_allowance_never_raises():
    budget = SessionBudget(BudgetLimits(max_requests=None, max_tokens=None))
    for _ in range(50):
        budget.reserve_request()
    budget.record_tokens(10_000_000)
    assert budget.usage["requests"] == 50


def test_usage_snapshot_is_independent():
    budget = SessionBudget(BudgetLimits(max_requests=5))
    budget.reserve_request()
    snapshot = budget.usage
    budget.reserve_request()
    assert snapshot["requests"] == 1
    assert budget.usage["requests"] == 2

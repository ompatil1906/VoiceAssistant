#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Cost control: provider classification, budget ceilings and the usage ledger.

EMMA's operating target is zero spend. This package is what makes that a
property of the code rather than a promise: providers are classified, unknown
providers are treated as billable, and a session stops at its configured
ceiling instead of continuing to a paid call.
"""

from emma.cost.budget import LOCAL_ONLY_BUDGET_LIMITS, BudgetLimits, SessionBudget
from emma.cost.ledger import UsageLedger, UsageRecord
from emma.cost.policy import (
    DEFAULT_ALLOWED,
    PROVIDER_CATALOGUE,
    BudgetExceeded,
    CostClass,
    CostPolicy,
    ProviderCost,
    ProviderDenied,
    QuotaExhausted,
)

__all__ = [
    "DEFAULT_ALLOWED",
    "LOCAL_ONLY_BUDGET_LIMITS",
    "PROVIDER_CATALOGUE",
    "BudgetExceeded",
    "BudgetLimits",
    "CostClass",
    "CostPolicy",
    "ProviderCost",
    "ProviderDenied",
    "QuotaExhausted",
    "SessionBudget",
    "UsageLedger",
    "UsageRecord",
]

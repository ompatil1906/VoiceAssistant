#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Provider cost classification and access control.

Every provider the runtime can reach is described by a :class:`ProviderCost`.
The pipeline asks :meth:`CostPolicy.assert_allowed` before a provider is built
and before each billed request batch, so an unexpected charge becomes a raised
exception instead of an invoice.

The governing rule: **a provider whose pricing is unknown is treated as paid**,
and paid providers are denied unless the deployment explicitly opts in.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Iterable, Mapping


class CostClass(str, Enum):
    """How a provider charges for use.

    Attributes:
        LOCAL: Runs on the deployment's own hardware. Cannot generate an invoice.
        FREE_TIER: Remote, but within a published free allowance. Generates no
            invoice while the allowance lasts and fails when it is exhausted.
        TRIAL: Remote promotional credit. Free until the credit runs out, then
            billable.
        PAID: Bills per request, token, character or second. Never selected by
            default.
    """

    LOCAL = "local"
    FREE_TIER = "free_tier"
    TRIAL = "trial"
    PAID = "paid"


#: Cost classes enabled when a deployment says nothing about spending.
DEFAULT_ALLOWED = frozenset({CostClass.LOCAL, CostClass.FREE_TIER})


class BudgetExceeded(RuntimeError):
    """Raised when a provider or request cannot be used under the cost policy."""


class ProviderDenied(BudgetExceeded):
    """Raised when a provider's cost class is not enabled for this deployment."""


class QuotaExhausted(BudgetExceeded):
    """Raised when the session's configured allowance is used up."""


@dataclass(frozen=True)
class ProviderCost:
    """Cost description for one provider.

    Parameters:
        provider: Provider identifier used in configuration, e.g. ``"gemini"``.
        cost_class: How the provider bills.
        trains_on_input: Whether the provider's terms permit it to use submitted
            content to improve its products. Checked by
            :mod:`emma.security.data_policy`; unverified providers are treated as
            permissive.
        notes: Why this classification was chosen, for auditors.
    """

    provider: str
    cost_class: CostClass
    trains_on_input: bool = False
    notes: str = ""


#: Providers the runtime knows about. Deliberately small: anything not listed is
#: treated as paid, and therefore denied by default.
PROVIDER_CATALOGUE: Mapping[str, ProviderCost] = MappingProxyType(
    {
        "kokoro": ProviderCost(
            provider="kokoro",
            cost_class=CostClass.LOCAL,
            notes="Kokoro-82M ONNX (Apache-2.0 weights, MIT runtime), CPU inference.",
        ),
        "whisper": ProviderCost(
            provider="whisper",
            cost_class=CostClass.LOCAL,
            notes="faster-whisper (MIT), local CPU/GPU inference.",
        ),
        "silero_vad": ProviderCost(
            provider="silero_vad",
            cost_class=CostClass.LOCAL,
            notes="Silero VAD ONNX, local inference.",
        ),
        "ollama": ProviderCost(
            provider="ollama",
            cost_class=CostClass.LOCAL,
            notes="Local model server; no per-token billing.",
        ),
        "gemini": ProviderCost(
            provider="gemini",
            cost_class=CostClass.FREE_TIER,
            trains_on_input=True,
            notes=(
                "Gemini API free tier is rate-limited (RPM/RPD), not unlimited. "
                "Google's API terms permit use of submitted content to improve "
                "products, so the data policy refuses to route to it while "
                "EMMA_DATA_MODE=synthetic."
            ),
        ),
        "deepgram": ProviderCost(
            provider="deepgram",
            cost_class=CostClass.TRIAL,
            trains_on_input=True,
            notes=(
                "Trial credit only, billable afterwards, and its current pricing "
                "and data terms were not verified: treated as permissive and "
                "disabled by default."
            ),
        ),
        "cartesia": ProviderCost(
            provider="cartesia",
            cost_class=CostClass.PAID,
            trains_on_input=False,
            notes="Cartesia Sonic ultra-low latency streaming voice API with emotion control. Billed per character.",
        ),
        "elevenlabs": ProviderCost(
            provider="elevenlabs",
            cost_class=CostClass.PAID,
            trains_on_input=False,
            notes="ElevenLabs conversational voice synthesis and cloning. Billed per character.",
        ),
    }
)


class CostPolicy:
    """Decides whether a provider or a request may be used.

    Example::

        policy = CostPolicy(allowed=frozenset({CostClass.LOCAL}))
        policy.assert_allowed("gemini")   # raises ProviderDenied
        policy.assert_allowed("kokoro")   # returns
    """

    def __init__(
        self,
        allowed: Iterable[CostClass] = DEFAULT_ALLOWED,
        catalogue: Mapping[str, ProviderCost] = PROVIDER_CATALOGUE,
    ) -> None:
        """Initialize the policy.

        Args:
            allowed: Cost classes this deployment may reach. Everything else is
                denied. Pass :data:`DEFAULT_ALLOWED` variants only when the owner
                of the deployment has accepted the spend.
            catalogue: Provider cost catalogue; defaults to
                :data:`PROVIDER_CATALOGUE`.
        """
        self._allowed = frozenset(allowed)
        self._catalogue = catalogue

    @property
    def allowed(self) -> frozenset[CostClass]:
        """Cost classes currently permitted."""
        return self._allowed

    def describe(self, provider: str) -> ProviderCost:
        """Return the catalogue entry for ``provider``.

        An unknown provider is described as paid, so it is denied by default
        rather than silently trusted.
        """
        known = self._catalogue.get(provider)
        if known is not None:
            return known
        return ProviderCost(
            provider=provider,
            cost_class=CostClass.PAID,
            trains_on_input=True,
            notes="Provider is not in the cost catalogue; treated as billable.",
        )

    def is_allowed(self, provider: str) -> bool:
        """Whether ``provider`` may be used under this policy."""
        return self.describe(provider).cost_class in self._allowed

    def assert_allowed(self, provider: str) -> ProviderCost:
        """Assert that ``provider`` may be used.

        Args:
            provider: Provider identifier, e.g. ``"gemini"``.

        Returns:
            The provider's cost description.

        Raises:
            ProviderDenied: If the provider's cost class is not enabled.
        """
        description = self.describe(provider)
        if description.cost_class not in self._allowed:
            raise ProviderDenied(
                f"provider '{provider}' is {description.cost_class.value} and this "
                f"deployment allows only "
                f"{', '.join(sorted(c.value for c in self._allowed))}"
            )
        return description

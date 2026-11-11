#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Data-policy gate for patient information.

EMMA is an administrative receptionist for NHS GP practices. Caller audio and
transcripts are patient data. Some providers' terms allow them to use submitted
content to improve their products, which is incompatible with that data leaving
the practice without a signed agreement.

The gate is a construction-time check: a provider that may train on its inputs
cannot be built while the runtime is in :attr:`DataMode.SYNTHETIC`, so a
misconfiguration fails at startup rather than after a real call.
"""

from __future__ import annotations

from enum import Enum

from emma.cost.policy import PROVIDER_CATALOGUE, CostClass, ProviderCost


class DataMode(str, Enum):
    """What kind of data this deployment may send to remote providers.

    Attributes:
        SYNTHETIC: Development and demonstration. Only synthetic or fully
            de-identified content may be processed, so providers that train on
            submitted content are refused.
        APPROVED: Production, after privacy, contractual and security sign-off
            for each provider in use. The deployment accepts the provider's
            data terms explicitly.
    """

    SYNTHETIC = "synthetic"
    APPROVED = "approved"


class DataPolicyViolation(RuntimeError):
    """Raised when a provider may not receive this deployment's data."""


class DataPolicy:
    """Decides which providers may receive caller data.

    Example::

        policy = DataPolicy(DataMode.SYNTHETIC)
        policy.assert_allowed("gemini")   # raises: free tier trains on input
        policy.assert_allowed("kokoro")   # returns: local
    """

    def __init__(
        self,
        mode: DataMode = DataMode.SYNTHETIC,
        catalogue=PROVIDER_CATALOGUE,
    ) -> None:
        """Initialize the policy.

        Args:
            mode: The deployment's data mode.
            catalogue: Provider catalogue used to look up data terms.
        """
        self._mode = mode
        self._catalogue = catalogue

    @property
    def mode(self) -> DataMode:
        """The active data mode."""
        return self._mode

    def describe(self, provider: str) -> ProviderCost:
        """Return the catalogue entry for ``provider``, defaulting to permissive."""
        known = self._catalogue.get(provider)
        if known is not None:
            return known
        return ProviderCost(
            provider=provider,
            cost_class=CostClass.PAID,
            trains_on_input=True,
            notes="Unknown provider; data terms unverified, so treated as permissive.",
        )

    def is_allowed(self, provider: str) -> bool:
        """Whether ``provider`` may receive data under the active mode."""
        if self._mode is DataMode.APPROVED:
            return True
        return not self.describe(provider).trains_on_input

    def assert_allowed(self, provider: str) -> None:
        """Assert that ``provider`` may receive data.

        Args:
            provider: Provider identifier, e.g. ``"gemini"``.

        Raises:
            DataPolicyViolation: If the provider's data terms are incompatible
                with the active mode.
        """
        if self.is_allowed(provider):
            return
        raise DataPolicyViolation(
            f"provider '{provider}' may use submitted content to improve its "
            f"products, which EMMA_DATA_MODE={self._mode.value} does not permit. "
            "Route this workload to a local provider, or switch the deployment to "
            "EMMA_DATA_MODE=approved once the provider's data terms have been "
            "signed off."
        )

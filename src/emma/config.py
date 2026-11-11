#
# Copyright (c) 2026, QuantumLoopAI
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Runtime configuration, loaded from the environment with safe defaults.

Two rules shape this module:

1. **Defaults cannot cost money.** With no environment variables set, the
   runtime selects local providers only and its spend ceiling is zero.
2. **Secrets are never rendered.** :meth:`RuntimeConfig.describe` reports which
   credentials are present, never their values.

Example::

    config = RuntimeConfig.from_env()
    plan = config.resolve_plan()   # raises if the selection would bill
    plan.llm, plan.stt, plan.tts   # -> "ollama", "whisper", "kokoro"
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from emma.cost.budget import BudgetLimits
from emma.cost.policy import CostClass, CostPolicy
from emma.security.data_policy import DataMode, DataPolicy

LOCAL_PROVIDERS = ("kokoro", "whisper", "silero_vad", "ollama")
FREE_TIER_PROVIDERS = ("gemini",)


class ProviderProfile(str, Enum):
    """Which provider set a deployment runs.

    Attributes:
        LOCAL: Local models only. Zero cost, nothing leaves the machine.
        FREE: Local models plus free-tier remote providers, used for LLM and
            speech where their allowance covers the load.
        CUSTOM: The exact provider list in ``EMMA_PROVIDERS``.
    """

    LOCAL = "local"
    FREE = "free"
    CUSTOM = "custom"


class ConfigError(RuntimeError):
    """Raised when the environment describes a configuration that is not allowed."""


def _env_bool(environ: Mapping[str, str], name: str, default: bool) -> bool:
    raw = environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(environ: Mapping[str, str], name: str, default: int | None) -> int | None:
    raw = environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    if raw.strip().lower() in ("none", "unlimited"):
        return None
    return int(raw)


def _env_float(environ: Mapping[str, str], name: str, default: float | None) -> float | None:
    raw = environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    if raw.strip().lower() in ("none", "unlimited"):
        return None
    return float(raw)


@dataclass(frozen=True)
class Plan:
    """The provider selection a session will actually use.

    Parameters:
        llm: Selected language-model provider.
        stt: Selected speech-to-text provider.
        tts: Selected text-to-speech provider.
        vad: Selected voice-activity detector.
        transport: Selected realtime transport.
    """

    llm: str
    stt: str
    tts: str
    vad: str = "silero_vad"
    transport: str = "livekit"

    def providers(self) -> tuple[str, ...]:
        """Every provider the plan touches, for cost and data checks."""
        return (self.llm, self.stt, self.tts, self.vad)


@dataclass(frozen=True)
class RuntimeConfig:
    """Typed runtime configuration.

    Parameters:
        profile: Provider profile to resolve.
        providers: Explicit provider list; only meaningful for
            :attr:`ProviderProfile.CUSTOM`.
        data_mode: Data mode for the deployment.
        allow_paid: Whether billable providers may be used at all.
        allow_trial: Whether trial-credit providers may be used.
        limits: Session budget ceilings.
        ledger_path: Where the usage ledger is written.
        llm_provider: Override for the language-model provider.
        stt_provider: Override for the speech-to-text provider.
        tts_provider: Override for the text-to-speech provider.
        transport: Realtime transport to build.
        gemini_model: Gemini model identifier.
        ollama_base_url: Ollama OpenAI-compatible endpoint.
        ollama_model: Ollama model identifier.
        whisper_model: faster-whisper model size or path.
        kokoro_voice: Kokoro voice name.
        credentials: Credential values, kept out of ``repr``.
    """

    profile: ProviderProfile = ProviderProfile.LOCAL
    providers: tuple[str, ...] = LOCAL_PROVIDERS
    data_mode: DataMode = DataMode.SYNTHETIC
    allow_paid: bool = False
    allow_trial: bool = False
    limits: BudgetLimits = field(default_factory=lambda: BudgetLimits(max_usd=0.0))
    ledger_path: str | None = "logs/usage.jsonl"
    llm_provider: str | None = None
    stt_provider: str | None = None
    tts_provider: str | None = None
    transport: str = "livekit"
    gemini_model: str = "gemini-2.5-flash"
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_model: str = "gemma3:4b"
    whisper_model: str = "base.en"
    kokoro_voice: str = "af_heart"
    cartesia_voice_id: str = "79a125e8-cd45-4c13-8a67-188112f4dd22"
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"
    deepgram_model: str = "nova-2"
    credentials: Mapping[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "RuntimeConfig":
        """Build a configuration from environment variables.

        Args:
            environ: Environment mapping; defaults to :data:`os.environ`.

        Returns:
            The parsed configuration.

        Raises:
            ConfigError: If a value cannot be parsed.
        """
        env = os.environ if environ is None else environ
        raw_profile = env.get("EMMA_PROVIDER_PROFILE", "local").strip().lower()
        try:
            profile = ProviderProfile(raw_profile)
        except ValueError as exc:
            raise ConfigError(
                f"EMMA_PROVIDER_PROFILE={raw_profile!r} is not one of "
                f"{', '.join(p.value for p in ProviderProfile)}"
            ) from exc

        if profile is ProviderProfile.FREE:
            providers = LOCAL_PROVIDERS + FREE_TIER_PROVIDERS
        elif profile is ProviderProfile.CUSTOM:
            listed = tuple(
                name.strip() for name in env.get("EMMA_PROVIDERS", "").split(",") if name.strip()
            )
            if not listed:
                raise ConfigError("EMMA_PROVIDER_PROFILE=custom requires EMMA_PROVIDERS")
            providers = listed
        else:
            providers = LOCAL_PROVIDERS

        raw_mode = env.get("EMMA_DATA_MODE", "synthetic").strip().lower()
        try:
            data_mode = DataMode(raw_mode)
        except ValueError as exc:
            raise ConfigError(
                f"EMMA_DATA_MODE={raw_mode!r} is not one of "
                f"{', '.join(m.value for m in DataMode)}"
            ) from exc

        limits = BudgetLimits(
            max_requests=_env_int(env, "EMMA_MAX_SESSION_REQUESTS", 200),
            max_tokens=_env_int(env, "EMMA_MAX_SESSION_TOKENS", 100_000),
            max_characters=_env_int(env, "EMMA_MAX_SESSION_CHARACTERS", 200_000),
            max_audio_seconds=_env_float(env, "EMMA_MAX_SESSION_AUDIO_SECONDS", 1_800.0),
            max_usd=_env_float(env, "EMMA_MAX_SESSION_USD", 0.0),
            max_minutes=_env_float(env, "EMMA_MAX_SESSION_MINUTES", 15.0),
        )

        credential_names = (
            "GEMINI_API_KEY",
            "DEEPGRAM_API_KEY",
            "CARTESIA_API_KEY",
            "ELEVENLABS_API_KEY",
            "LIVEKIT_URL",
            "LIVEKIT_API_KEY",
            "LIVEKIT_API_SECRET",
        )
        credentials = {name: env[name] for name in credential_names if env.get(name)}

        # Transport follows what the deployment has: LiveKit when its credentials
        # are present, otherwise the WebSocket transport, which needs no external
        # service. An explicit EMMA_TRANSPORT always wins.
        livekit_ready = bool(credentials.get("LIVEKIT_URL") and credentials.get("LIVEKIT_API_KEY"))
        transport = env.get("EMMA_TRANSPORT") or ("livekit" if livekit_ready else "websocket")

        return cls(
            profile=profile,
            providers=providers,
            data_mode=data_mode,
            allow_paid=_env_bool(env, "EMMA_ALLOW_PAID", False),
            allow_trial=_env_bool(env, "EMMA_ALLOW_TRIAL", False),
            limits=limits,
            ledger_path=env.get("EMMA_LEDGER_PATH", "logs/usage.jsonl") or None,
            llm_provider=env.get("EMMA_LLM_PROVIDER") or None,
            stt_provider=env.get("EMMA_STT_PROVIDER") or None,
            tts_provider=env.get("EMMA_TTS_PROVIDER") or None,
            transport=transport,
            gemini_model=env.get("EMMA_GEMINI_MODEL", "gemini-2.5-flash"),
            ollama_base_url=env.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
            ollama_model=env.get("EMMA_OLLAMA_MODEL", "gemma3:4b"),
            whisper_model=env.get("EMMA_WHISPER_MODEL", "base.en"),
            kokoro_voice=env.get("EMMA_KOKORO_VOICE", "af_heart"),
            cartesia_voice_id=env.get("EMMA_CARTESIA_VOICE_ID", "79a125e8-cd45-4c13-8a67-188112f4dd22"),
            elevenlabs_voice_id=env.get("EMMA_ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"),
            deepgram_model=env.get("EMMA_DEEPGRAM_MODEL", "nova-2"),
            credentials=credentials,
        )

    def cost_policy(self) -> CostPolicy:
        """Build the cost policy implied by this configuration."""
        allowed = {CostClass.LOCAL, CostClass.FREE_TIER}
        if self.allow_trial:
            allowed.add(CostClass.TRIAL)
        if self.allow_paid:
            allowed.add(CostClass.PAID)
        return CostPolicy(allowed=frozenset(allowed))

    def data_policy(self) -> DataPolicy:
        """Build the data policy implied by this configuration."""
        return DataPolicy(self.data_mode)

    def has_credential(self, name: str) -> bool:
        """Whether a credential is configured, without revealing it."""
        return bool(self.credentials.get(name))

    def resolve_plan(self) -> Plan:
        """Select providers and verify the selection against both policies.

        Returns:
            The concrete provider plan.

        Raises:
            ConfigError: If a selected provider is not permitted by the cost
                policy, the data policy, or the configured profile.
            DataPolicyViolation: If the provider's data terms conflict with the
                data mode.
        """
        llm = self.llm_provider or ("gemini" if "gemini" in self.providers else "ollama")
        stt = self.stt_provider or ("gemini" if "gemini" in self.providers else "whisper")
        tts = self.tts_provider or "kokoro"

        plan = Plan(llm=llm, stt=stt, tts=tts, transport=self.transport)
        policy = self.cost_policy()
        data = self.data_policy()

        for provider in plan.providers():
            if provider not in self.providers:
                raise ConfigError(
                    f"provider '{provider}' is not in the profile "
                    f"(EMMA_PROVIDER_PROFILE={self.profile.value})"
                )
            policy.assert_allowed(provider)
            data.assert_allowed(provider)

        if llm == "gemini" and not self.has_credential("GEMINI_API_KEY"):
            raise ConfigError(
                "llm provider 'gemini' is selected but GEMINI_API_KEY is not set; "
                "set the key or use EMMA_PROVIDER_PROFILE=local"
            )
        if self.transport == "livekit" and not (
            self.has_credential("LIVEKIT_URL") and self.has_credential("LIVEKIT_API_KEY")
        ):
            raise ConfigError(
                "transport 'livekit' is selected but LIVEKIT_URL/LIVEKIT_API_KEY are "
                "not set; set them or use EMMA_TRANSPORT=websocket"
            )
        return plan

    def describe(self) -> dict[str, object]:
        """Return a loggable summary that reveals which credentials exist, never their values."""
        return {
            "profile": self.profile.value,
            "providers": list(self.providers),
            "data_mode": self.data_mode.value,
            "allow_paid": self.allow_paid,
            "allow_trial": self.allow_trial,
            "transport": self.transport,
            "llm_provider": self.llm_provider,
            "stt_provider": self.stt_provider,
            "tts_provider": self.tts_provider,
            "session_limits": {
                "requests": self.limits.max_requests,
                "tokens": self.limits.max_tokens,
                "characters": self.limits.max_characters,
                "audio_seconds": self.limits.max_audio_seconds,
                "usd": self.limits.max_usd,
                "minutes": self.limits.max_minutes,
            },
            "credentials_present": sorted(self.credentials),
        }

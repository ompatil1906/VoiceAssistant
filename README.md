# EMMA Receptionist

**by QuantumLoopAI**

A real-time voice runtime for EMMA, an administrative receptionist for NHS GP
practices. EMMA answers practice questions, captures appointment requests,
supports routine appointment and prescription workflows, collects structured
caller information, escalates to a human receptionist, and gives approved
emergency instructions — without attempting clinical assessment.

This repository contains the **runtime**: the pipeline engine, audio path,
voice activity detection, turn-taking, speech and language integrations, and
transports. The EMMA application (FastAPI backend, LiveKit token issuing, browser
client, practice knowledge base, authentication) lives in its own repository and
imports this one.

---

## Status

The runtime is being extracted from its upstream framework into a first-party
package. Current state is tracked in
[`docs/architecture/INITIAL_AUDIT.md`](docs/architecture/INITIAL_AUDIT.md) and the
migration plan in
[`docs/architecture/MIGRATION_PLAN.md`](docs/architecture/MIGRATION_PLAN.md).
Anything that has not been measured or executed is labelled as such in
[`docs/reports/TEST_REPORT.md`](docs/reports/TEST_REPORT.md) — no capability is
claimed on the strength of an import succeeding.

---

## Design principles

1. **Zero spend by default.** Local models handle every path. Remote providers
   are rate-limited free tiers, disabled unless a deployment opts in, and metered
   by an in-process budget guard that stops a session at its ceiling.
2. **Patient data stays put unless a human decided otherwise.** The default data
   mode refuses to route audio or transcripts to any provider whose terms allow
   it to train on submitted content.
3. **Interruption is a first-class signal.** Barge-in cancels generation and
   synthesis, flushes stale audio, and keeps the conversation coherent.
4. **Small, auditable surface.** Only the providers, transports and modules EMMA
   actually runs are present. The provider set is a static fact, not a plugin
   search.

## Architecture at a glance

```
Browser (AudioWorklet, 16 kHz PCM16)
   │  WebRTC via self-hosted LiveKit  ── or ──  WebSocket
   ▼
transports ──► runtime (frames · processors · pipeline · worker · runner)
                    │
                    ├── speech: VAD · turn detection · STT · TTS
                    ├── intelligence: LLM · context · tools
                    └── audio: resampling · mixing · buffering
                          ▲
             cost (budget guard) · security (data policy, redaction)
```

Full description, including the barge-in path and the cost/privacy gates:
[`docs/architecture/ARCHITECTURE.md`](docs/architecture/ARCHITECTURE.md).

## Technology, at zero cost

| Layer | Default | Why |
| --- | --- | --- |
| Transport | LiveKit server, self-hosted (Apache-2.0) | WebRTC without a managed bill |
| VAD | Silero VAD, local ONNX | No network hop on the interruption path |
| STT | faster-whisper (MIT), local | Zero cost; segment-based, not partial streaming |
| STT (optional) | Gemini streaming, free tier | Lower latency, rate limited, requires approved data mode |
| LLM | Ollama / Gemma, local | Zero cost per token |
| LLM (optional) | Gemini Flash, free tier | Fast first token, tool calling, rate limited |
| TTS | Kokoro-82M via `kokoro-onnx` (Apache-2.0 weights) | Natural voice, CPU inference, no per-character bill |

Piper is deliberately absent: the `piper-tts` distribution the upstream extra
pins is GPL-3.0-or-later. See
[`docs/architecture/TECH_SELECTION.md`](docs/architecture/TECH_SELECTION.md),
which records what was verified, from where, and on what date.

## Quickstart

```bash
# 1. Environment (Python ≥3.11)
uv sync --extra livekit --extra kokoro --extra whisper

# 2. Configuration — safe defaults, no credentials required
cp .env.example .env

# 3. Local model server for the LLM
ollama pull gemma3:4b && ollama serve

# 4. Run the tests
uv run pytest tests/emma
```

Bringing up LiveKit, the EMMA backend and the browser client is covered in
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md). Development workflow, including the
extraction tooling, is in [`docs/DEVELOPER_SETUP.md`](docs/DEVELOPER_SETUP.md).

## Not a medical device

EMMA is an administrative receptionist. It does not diagnose, triage or give
clinical advice, and it must not be deployed as if it did. Emergency handling is
limited to delivering the practice's approved wording and escalating.

## Licensing and attribution

- This repository is distributed under the MIT License (`LICENSE`).
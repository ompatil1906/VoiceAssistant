# AGENTS.md

Guidance for AI coding agents working in this repository. Project overview and
quickstart live in `README.md`; architecture in
`docs/architecture/ARCHITECTURE.md`.

## What this repository is

The **EMMA Receptionist runtime** by QuantumLoopAI: the voice engine that the
EMMA application (FastAPI backend, browser client, practice knowledge base — a
separate repository) imports to run a conversation. It is not the upstream
framework, and it
not a general-purpose voice framework: only the providers, transports and modules
EMMA actually runs are present.

## Current migration state

The runtime is generated from the upstream tree by `scripts/emma/vendor.py`. Read
`docs/architecture/INITIAL_AUDIT.md` and `docs/reports/TEST_REPORT.md` before
changing anything: they state what exists, what was verified and what is blocked.
Do not assume a module under `emma.` exists because the architecture document
names it — check the tree.

## Commands

```bash
# Verify first-party modules with no virtualenv required
PYTHONPATH=src python3 scripts/emma/selfcheck.py

# Full test suite (needs an installed environment)
uv run pytest tests/emma

# Lint
uv run ruff check

# Import closure of the modules EMMA runs
python3 scripts/emma/module_graph.py --closure pipeline.worker ...

# What the extractor would generate (never writes)
python3 scripts/emma/vendor.py --check

# Generate src/emma from the upstream tree
python3 scripts/emma/vendor.py
```

## Rules that are not negotiable

1. **Provenance.** A file derived from upstream keeps its original copyright and
   `SPDX-License-Identifier` header and carries the QuantumLoopAI modification
   notice. `NOTICE` and `THIRD_PARTY_NOTICES/` must survive every refactor.
2. **Never add a billable path by default.** A new provider goes into
   `emma/cost/policy.py:PROVIDER_CATALOGUE` with an explicit cost class. Unknown
   pricing means `PAID`, which means disabled. Nothing may bypass
   `CostPolicy.assert_allowed`.
3. **Patient data.** Any provider that may train on submitted content is flagged
   `trains_on_input=True`, keeping it unreachable in `synthetic` data mode. Log
   output passes through `emma.security.redaction`.
4. **No copyleft in the base install.** `piper-tts` is GPL-3.0-or-later and was
   removed for that reason. A copyleft library may only appear behind an optional
   extra and must be recorded in `THIRD_PARTY_NOTICES/python-dependencies.md`.
5. **No upstream product identity.** No upstream logo, badge, link, CLI name or
   packaging metadata. Attribution in `THIRD_PARTY_NOTICES/` is required; branding
   is not permitted.
6. **Do not claim untested behaviour.** Reports use VERIFIED / FAILED / NOT TESTED
   / BLOCKED. Browser audio, LiveKit media flow and latency numbers require a real
   run; an import succeeding proves nothing.

## Engineering conventions

- Frames are the public interface. Prefer pushing a frame over adding a method
  that duplicates one.
- `process_frame` must stay fast: hand slow work to `self.create_task()` and push
  the result when ready. Never block the event loop.
- Queues are bounded. Always.
- Use `self.create_task(coroutine, name)` so the task manager owns the task, and
  `await self.cancel_task(task)` to stop it.
- Interruptions broadcast both directions; cancelled generations never append
  partial output to the conversation context.
- Docstrings are Google style. Line length 100.
- Keep the runtime dependency-light: a new dependency needs a licence entry and a
  reason.

## Repository map

| Path | Contents |
| --- | --- |
| `src/emma/runtime/` | frames, processor, pipeline, worker, runner |
| `src/emma/audio/` | resampling, filters, mixing, buffering |
| `src/emma/speech/` | VAD, turn detection, STT, TTS, expression |
| `src/emma/intelligence/` | LLM services, context, adapters |
| `src/emma/transports/` | LiveKit, WebSocket, RTVI |
| `src/emma/turns/` | user start/stop strategies |
| `src/emma/cost/` | budget guard, cost classes, usage ledger (**first-party**) |
| `src/emma/security/` | data policy, redaction (**first-party**) |
| `src/emma/support/` | inherited utilities |
| `tests/emma/` | first-party tests + ported framework tests |
| `scripts/emma/` | closure analysis, extraction generator, test runner |
| `docs/` | architecture, reports, developer and deployment guides |
| `THIRD_PARTY_NOTICES/` | attribution and licence inventory |

# Developer Setup

## Prerequisites

| Requirement | Version | Notes |
| --- | --- | --- |
| Python | ≥3.11 | 3.13 is what the migration was audited on |
| `uv` | current | dependency and environment management |
| Ollama | current | only for the local-LLM path |
| LiveKit server | current | only for the WebRTC path; the WebSocket transport needs nothing |

No cloud account is required to develop, test or run the default configuration.

## Install

```bash
uv sync --extra livekit --extra kokoro --extra whisper --group dev
cp .env.example .env
```

Extras deliberately **not** installed by default: everything billable. Adding an
extra is a decision, and `EMMA_ALLOW_PAID=false` keeps it unreachable even then.

## Running the code

```bash
# Local models for the LLM and speech
ollama pull gemma3:4b
ollama serve

# The runtime's own tests
uv run pytest tests/emma

# Lint and format
uv run ruff check
uv run ruff format --check
```

If you do not have a virtualenv yet (or `uv` is unavailable), the first-party
modules can still be verified with the dependency-free runner:

```bash
PYTHONPATH=src python3 scripts/emma/selfcheck.py
```

## Repository layout

| Path | Contents |
| --- | --- |
| `src/emma/` | the runtime package |
| `src/emma/cost/` | budget guard, cost classes, usage ledger (first-party) |
| `src/emma/security/` | data-policy gate, healthcare redaction (first-party) |
| `tests/emma/` | first-party tests plus ported framework tests |
| `scripts/emma/` | extraction tooling (see below) |
| `docs/architecture/` | audit, architecture, technology selection, migration plan |
| `docs/reports/` | test, cleanup and validation reports |
| `THIRD_PARTY_NOTICES/` | attribution and licence inventory |

## Extraction tooling

The runtime is generated from the upstream framework, not hand-copied. Three
scripts do the work:

```bash
# Transitive import closure of the modules EMMA runs
python3 scripts/emma/module_graph.py --closure pipeline.worker ...

# What would be generated, without writing anything
python3 scripts/emma/vendor.py --check

# Generate src/emma from the upstream tree
python3 scripts/emma/vendor.py

# Run first-party tests without a virtualenv
PYTHONPATH=src python3 scripts/emma/selfcheck.py
```

`scripts/emma/vendor.py` holds two tables — `EXACT` and `PREFIX` — that define the
module mapping. Changing where a module lands means editing that table, not
moving files by hand. See
[`docs/architecture/MIGRATION_PLAN.md`](architecture/MIGRATION_PLAN.md).

## Rules for changes

1. **Provenance.** A file derived from upstream keeps its original copyright and
   SPDX header and adds the QuantumLoopAI modification notice. Never strip an
   inherited header.
2. **Cost.** Any new provider entry goes in `emma/cost/policy.py:PROVIDER_CATALOGUE`
   with an explicit cost class. A provider whose pricing you cannot establish is
   `PAID` — which means disabled.
3. **Privacy.** Any provider that may train on submitted content is flagged
   `trains_on_input=True`, which keeps it unreachable in `synthetic` data mode.
4. **Dependencies.** No new base dependency with an unknown licence, and no new
   copyleft dependency in the base install (see
   `THIRD_PARTY_NOTICES/python-dependencies.md`).
5. **Queues.** Bounded, always. A growth path that has no ceiling is a bug.
6. **Blocking work.** Never inside `process_frame` or any other frame-handling
   coroutine; hand it to a task and push the result when it is ready.
7. **Claims.** A capability is documented as working only after a test that
   exercises it ran here. Otherwise it is marked NOT TESTED or BLOCKED.

## Testing tiers

| Tier | What it proves | How |
| --- | --- | --- |
| First-party unit tests | cost policy, budgets, data policy, redaction, configuration | `pytest tests/emma` or `selfcheck.py` |
| Ported framework tests | the inherited engine still behaves after the move | `pytest tests/emma` |
| Offline latency harness | pipeline timings with mocked providers | `scripts/bench/` (see `docs/performance/`) |
| Live session | a real model stack end to end | needs Ollama + a transport |

Browser microphone and acoustic behaviour cannot be verified without a running
LiveKit server and a real client; do not claim otherwise in a report.

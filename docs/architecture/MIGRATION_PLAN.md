# Extraction and Migration Plan

How the upstream tree becomes the EMMA runtime, in an order where every step is
verifiable and reversible.

## 1. Principle

The extraction is **generated, not hand-copied**. `scripts/emma/vendor.py` reads
the upstream package, computes the transitive import closure of the modules EMMA
runs, and writes the first-party package. Consequences:

- The set of vendored files is a fact, not a memory.
- Import rewriting, branding scrub and provenance headers are applied uniformly.
- Re-running after an upstream rebase reproduces the same move.
- Reviewing a migration means reviewing a mapping table, not 180 diffs.

The upstream package is **discovered**, not named: it is the one package directory
under `src/` that is not `emma`. Module mapping is expressed in *relative* paths,
so the tooling carries no upstream product identity and re-pointing it at a future
upstream is a single flag.

## 2. Mapping

Module names map through exact rules first, then prefix rules (longest prefix
wins). The tables live in `scripts/emma/vendor.py:EXACT_REL` and `:PREFIX_REL`, and
are written relative to the upstream root — `frames.frames` maps to
`emma.runtime.frames`, and so on:

| Upstream area (relative) | EMMA module |
| --- | --- |
| `frames.frames` | `emma.runtime.frames` |
| `processors.frame_processor` | `emma.runtime.processor` |
| `pipeline.*` | `emma.runtime.pipeline.*` |
| `workers.*` | `emma.runtime.workers.*` |
| `clocks`, `bus`, `registry` | `emma.runtime.{clocks,bus,registry}` |
| `audio.vad` | `emma.speech.vad` |
| `audio.turn` | `emma.speech.turn_detection` |
| `audio` | `emma.audio` |
| `services.stt_service` | `emma.speech.stt.base` |
| `services.google.stt` | `emma.speech.stt.gemini` |
| `services.tts_service` | `emma.speech.tts.base` |
| `services.kokoro.tts` | `emma.speech.tts.kokoro` |
| `services.llm_service` | `emma.intelligence.llm.base` |
| `services.google.llm` | `emma.intelligence.llm.gemini` |
| `processors.aggregators.*` | `emma.intelligence.aggregators.*` |
| `adapters.llm.*` | `emma.intelligence.adapters.*` |
| `transports.*` | `emma.transports.*` |
| `processors.frameworks.rtvi.*` | `emma.transports.rtvi.*` |
| `turns.*` | `emma.turns.*` |
| `observers.*`, `metrics.*` | `emma.observability.*` |
| `utils.*` | `emma.support.*` |
| `tests.utils` | `emma.testing.utils` |
| package root | `emma` |

Rewriting rules:

- Import statements are rewritten from the AST, including relative imports
  (resolved to absolute, then mapped) — so a file may move directories safely.
- String literals that name a whole module are rewritten. Prose and URLs are not
  touched by the import pass.
- A separate branding pass then replaces residual upstream product naming in
  comments, docstrings, log identifiers and URLs. The only place the upstream name
  exists in this repository is the single constant driving that pass, in the
  extraction tool — never in the product, the packaging or the documentation.
- Every file keeps its original copyright and `SPDX-License-Identifier` header and
  gains `Copyright (c) 2026, QuantumLoopAI` plus a modification notice.

## 3. Order of work

Each step has a gate. A step does not start until the previous gate passes.

| # | Step | Gate |
| --- | --- | --- |
| 1 | Audit (`docs/architecture/INITIAL_AUDIT.md`) | numbers reproduce from the tree |
| 2 | Module graph + closure (`scripts/emma/module_graph.py`) | closure size stable, seeds documented |
| 3 | Generate `src/emma` (`scripts/emma/vendor.py`) | every vendored module imports |
| 4 | Port covered tests into `tests/emma/` | ported suite runs green |
| 5 | First-party identity (`pyproject.toml`, `emma/__init__`) | `pip install -e .` succeeds; no upstream import remains |
| 6 | Cost guard + data policy (`emma/cost`, `emma/security`) | unit tests: unknown price disabled, budget raises, redaction masks |
| 7 | Delete upstream tree (`src/` upstream package, unported tests, examples, …) | full suite green with the upstream tree absent |
| 8 | Name audit (`scripts/emma/nameaudit.py`) | zero upstream-name hits outside Git history |
| 9 | Benchmarks | numbers recorded with sample counts |
| 10 | Cleanup pass | suite re-run green; no dead modules |

Step 7 is the irreversible one, so it happens **after** the runtime is proven to
stand on its own.

## 4. Deliberate keeps

| Kept | Why removing it now is the wrong trade |
| --- | --- |
| `bus/`, `registry/`, job RPC | `BaseWorker` lifecycle is written against them; the local bus is in-process asyncio with no latency cost. Removing them is a ~3.5 kLOC refactor whose only benefit is fewer files. |
| `expressive/` | `frames.py` imports it; inert unless a service declares capabilities. |
| `turns/` (including provider-specific strategies) | The turn engine is the barge-in engine. Unused strategies are unreachable without their providers, which are not installed. |
| `openai` base LLM classes | Ollama's service is implemented on top of them. |
| `docs/architecture/word-trecking/` | Inherited design notes for word tracking, text-segment mapping and RTVI — modules EMMA keeps. Refreshed to EMMA naming. |

## 5. Deliberate drops beyond the plain closure

| Dropped | Trigger |
| --- | --- |
| `piper` service | `piper-tts` ≥1.3.0 is GPL-3.0-or-later (see `TECH_SELECTION.md` §6) |
| `bus/network` (pgmq, redis) | no distributed workers |
| `runner/` prebuilt app | EMMA has its own FastAPI backend |
| `evals/`, `cli/`, `flows/` | not runtime |
| telephony serializers | PSTN out of scope |
| 58 provider integrations | not used by EMMA |
| upstream logo and badges | trademark, not licence |

## 6. Reversibility

- Upstream history is untouched; the migration is ordinary commits on a branch.
- Until step 7, the upstream tree remains intact, so any regression can be
  compared against the original implementation directly.
- `scripts/emma/vendor.py` can be re-run at any time to regenerate `src/emma` from
  the upstream tree.

## 7. After the migration

Upstream fixes worth taking are taken by re-running the generator against a
rebased upstream, then reviewing the closure diff. That is intentionally more work
than a dependency bump — it is the cost of owning the runtime, and it is why the
mapping lives in one file.

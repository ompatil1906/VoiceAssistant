# Initial Repository Audit

**Project:** EMMA Receptionist by QuantumLoopAI
**Audit date:** 2026-02-24
**Auditor:** QuantumLoopAI engineering
**Status of this document:** factual record of the repository *before* any extraction work.

Everything marked **VERIFIED** was produced by inspecting this working tree or by
running a command in it. Anything that could not be established is marked
**BLOCKED** or **NOT TESTED** — never assumed.

---

## 1. Repository identity

| Property | Value | Status |
| --- | --- | --- |
| Working tree | `<repo>` (the checkout root) | VERIFIED |
| Upstream | `git remote origin` of the checkout above | VERIFIED |
| Branch | `main` | VERIFIED |
| HEAD commit | `cbdeae0cb31a067a197163b01f2e2b56ce9b7c09` | VERIFIED |
| `git describe` | `v1.15.0-465-gcbdeae0cb` | VERIFIED |
| Distribution name | the upstream distribution | VERIFIED (original `pyproject.toml`) |
| Import name | the upstream import root (see `module_graph.py:upstream_root`) | VERIFIED |
| License | BSD-2-Clause, `Copyright (c) 2024–2026, Daily` | VERIFIED (`LICENSE`) |
| Requires Python | `>=3.11` | VERIFIED (`pyproject.toml`) |
| Local Python | 3.13.5 | VERIFIED |
| Local modifications at audit time | none (`git status` clean, no stashes) | VERIFIED |

### What is *not* in this repository

The EMMA application — FastAPI backend, LiveKit token endpoint, browser client,
practice knowledge base, authentication, session management — **is not present
in this checkout**. `emma/`, `backend/`, `app/` do not exist. The parent
directory `~/Projects/QuantumLoopAI` is outside the authorized inspection scope
and was not read.

Consequence: Milestone 5 ("preserve existing EMMA functionality") and the
regression suite for the EMMA API contracts are **BLOCKED** until the
application code is made available. Everything in this audit and in the
migration work is therefore scoped to the **runtime layer**, which is what the
EMMA application consumes.

---

## 2. Size and shape of the upstream package

| Metric | Value | Status |
| --- | --- | --- |
| Python modules in the upstream package | 758 | VERIFIED |
| Lines of Python in the upstream package | 224,628 | VERIFIED |
| Provider integrations (`services/*/`) | 63 | VERIFIED |
| Transport integrations (`transports/*/`) | 11 | VERIFIED |
| Optional-dependency extras | 98 | VERIFIED |
| Base runtime dependencies | 23 | VERIFIED |
| Test files | 421 (7.4 MB) | VERIFIED |
| `examples/` | 25 projects, 18 MB | VERIFIED |
| `evals/` (upstream) | 11,877 lines | VERIFIED |
| `cli/` (upstream) | 7,755 lines | VERIFIED |
| Files carrying a `Copyright (c)` header | 614 | VERIFIED |

Largest subpackages by lines of code:

```
services/     96,196     transports/  20,528     processors/  12,957
utils/        12,790     evals/       11,877     adapters/     8,322
cli/           7,755     turns/        6,660     workers/      6,352
pipeline/      6,021     audio/        5,879     runner/       4,294
```

---

## 3. Existing architecture (as found)

Frame-based pipeline. A `Frame` is the unit of data and control; a
`FrameProcessor` handles one frame at a time; a `Pipeline` chains processors; a
worker owns the pipeline lifecycle.

| Layer | Location | Notes |
| --- | --- | --- |
| Frames | `frames/frames.py` (3,057 lines) | data + control signals, downstream/upstream |
| Processor base | `processors/frame_processor.py` (1,398) | queue, task manager, interruption handling |
| Pipeline | `pipeline/pipeline.py`, `pipeline/base_pipeline.py` | linear + parallel composition |
| Worker | `pipeline/worker.py` (1,858) | `PipelineWorker`, the runnable unit |
| Worker base | `workers/base_worker.py` (1,622) | activation, bus subscription, job RPC |
| Runner | `workers/runner.py` (577) | signal handling, shared bus, registry |
| Bus | `bus/` (2,574) | in-process and network (pgmq/redis) message bus |
| Registry | `registry/` (206) | worker discovery |
| Jobs | `pipeline/job_context.py`, `job_decorator.py` | cross-worker RPC |
| Audio | `audio/` (5,879) | resamplers, filters, mixers, VAD, turn analyzers |
| Turns | `turns/` (6,660) | user start/stop strategies, turn controller |
| Speech/AI services | `services/` (96,196) | 63 providers |
| Context | `processors/aggregators/llm_response_universal.py` (2,918) | `LLMContext` + user/assistant aggregators |
| Transports | `transports/` (20,528) | LiveKit, WebSocket, Daily, local, … |
| RTVI | `processors/frameworks/rtvi/` | client protocol bridging |
| Observability | `observers/` (3,906), `metrics/` (247) | frame observers, TTFB/usage metrics |

Beyond the single-bot case the framework carries a **multi-agent** model:
`BaseWorker` + `WorkerBus` + `WorkerRegistry` + `@job` RPC + bus-bridged
pipelines. That model exists because the upstream framework supports agents that hand off, fan
out and run across processes.

---

## 4. Execution path of a working voice session

The chain below was confirmed by reading the source (file:line references are to
HEAD `cbdeae0`).

1. **Construction** — application builds a `Pipeline` of processors and a
   transport, wraps them in `PipelineWorker` (`pipeline/worker.py`), and
   registers it with `WorkerRunner` (`workers/runner.py`).
2. **Activation** — `BaseWorker.activate()` (`workers/base_worker.py`) starts the
   pipeline; `PipelineWorker` pushes `StartFrame` first, which every processor
   uses to learn sample rates and to mark itself ready.
3. **Ingress** — `BaseInputTransport` (`transports/base_input.py`) is where
   `StartFrame` initialises audio in; media arrives from the transport, is
   resampled to the pipeline rate, and pushed downstream as `InputAudioRawFrame`.
   A `VADProcessor` downstream of the input converts audio into
   `VADUserStartedSpeakingFrame` / `VADUserStoppedSpeakingFrame`.
4. **Turn-taking** — user turn start/stop strategies (`turns/user_start/`,
   `turns/user_stop/`) are driven by the `LLMUserAggregator`; strategies emit
   `UserStartedSpeakingFrame` / `UserStoppedSpeakingFrame` and, on a confirmed
   start during bot speech, an interruption.
5. **Recognition** — `SegmentedSTTService` (`services/stt_service.py`)
   accumulates the user's speech segment and transcribes it **in a background
   task**, pushing `TranscriptionFrame` downstream.
6. **Aggregation** — `LLMUserAggregator` collects transcriptions into
   `LLMContext` and, when the turn ends, emits `LLMContextFrame`.
7. **Generation** — `LLMService` (`services/llm_service.py`) streams
   `LLMTextFrame` downstream token by token; tool calls go out as
   `FunctionCallsStartedFrame` and results come back as
   `FunctionCallResultFrame`.
8. **Synthesis** — `TTSService` (`services/tts_service.py`) aggregates text into
   sentences and streams `TTSAudioRawFrame` downstream as audio becomes
   available, tagging frames with a context id so they can be cancelled.
9. **Egress** — `BaseOutputTransport` (`transports/base_output.py`) buffers and
   mixes audio frames, applies the transport's output sample rate, and writes
   them out in real time.
10. **Interruption** — any processor can call `broadcast_interruption()`, which
    pushes `InterruptionFrame` both ways; downstream services flush queued audio,
    upstream aggregation discards the partial turn.
11. **Teardown** — `EndFrame` propagates, processors release tasks and
    resources, the worker ends, and (with `auto_end=True`) the runner exits.

**Observation for EMMA:** steps 1–11 need the pipeline, frames, processor,
worker, transports, VAD, turn strategies, aggregators and one STT/LLM/TTS
triple. They do **not** need the bus, the registry, job RPC, `flows`, the CLI,
`evals`, the runner's prebuilt app, any of the 63 providers, or the example
projects.

---

## 5. Essential vs. removable (evidence, not opinion)

The extraction is driven by the transitive import closure of the modules a real
session touches, computed with `scripts/emma/module_graph.py` (an AST walk that
also follows `import`-in-string dynamic imports).

Seeds used for the measurement:

```
pipeline.worker, workers.runner, transports.livekit.transport,
transports.websocket.fastapi, services.google.{stt,llm,tts},
services.kokoro.tts, services.piper.tts, services.whisper.stt,
services.ollama.llm, audio.vad.silero,
processors.aggregators.{llm_context,llm_response_universal}
```

Result:

| Metric | Value | Status |
| --- | --- | --- |
| Modules in closure | 182 | VERIFIED |
| Lines in closure | 67,558 | VERIFIED (30.1 % of upstream) |
| Data files travelling with code | 39 | VERIFIED (`*.onnx`, `*.proto`, `*.wav`, `*.json`) |
| Upstream tests fully covered by closure | 143 of 421 | VERIFIED |

Closure by subpackage:

```
utils 43 · turns 32 · audio 21 · services 19 · processors 12 · bus 9
pipeline 8 · adapters 8 · transports 6 · expressive 6 · observers 3
registry 3 · clocks 2 · workers 2 · frames 1 · metrics 1 · serializers 1
transcriptions 1 · classifiers 1 · package root 1
```

### Kept

Everything in the closure, because the closure *is* the evidence of need.

### Removed

| Removed | Lines | Justification |
| --- | --- | --- |
| `services/` minus closure | ~90,000 | 58 of 63 providers unused by EMMA |
| `transports/` minus closure | ~20,000 | Daily, Tavus, HeyGen, MoQ, Vonage, WhatsApp, SmallWebRTC, LemonSlice unused |
| `evals/` | 11,877 | release-eval harness, not runtime |
| `cli/` | 7,755 | project scaffolding/monitoring, not runtime |
| `flows/` | 2,943 | structured conversation DSL, unused by EMMA |
| `runner/` | 4,294 | prebuilt multi-transport bot host, unused |
| `serializers/` minus base | ~3,100 | telephony codecs (Twilio, Plivo, Telnyx, …) — PSTN explicitly out of scope |
| `examples/` | 18 MB | demo projects |
| `scripts/cli/` | — | scaffolding for the removed CLI |
| `docs/` (upstream site) | 264 KB | replaced by EMMA documentation |

### Kept deliberately even where the closure is loose

- **`bus/` (9 modules, ~2.5 kLOC).** `BaseWorker`'s activation lifecycle, job RPC
  and `PipelineWorker`'s bridged mode are written against it. It is pure
  in-process `asyncio` for the local implementation and adds no measurable
  latency. Removing it means rewriting ~3.5 kLOC of lifecycle code — a deep
  refactor with real regression risk and no runtime benefit for a single-bot
  receptionist. The **network** implementations (pgmq, redis) are removed.
- **`turns/` (32 modules).** This is the turn-taking engine; EMMA's barge-in and
  end-of-turn behaviour depend on it. Individual strategies that require
  provider-specific signals (Krisp, wake-phrase) remain but are unreachable
  without those providers, which are not installed.
- **`expressive/` (6 modules).** `frames.py` imports expression capabilities;
  the vendored Kokoro/TTS path can perform delivery tags. It is inert when a
  service declares no capabilities.

---

## 6. Dependency inventory

Base runtime dependencies (23), with the reason each is present:

| Package | Why it is in the base install | EMMA need |
| --- | --- | --- |
| `aiofiles` | async file I/O | keep |
| `aiohttp` | HTTP client for services | keep |
| `audioop-lts` | audio ops on Python ≥3.13 | keep |
| `docstring_parser` | parses tool docstrings into function schemas | keep |
| `loguru` | logging | keep |
| `loudness` | audio level normalisation | keep |
| `Markdown` | Markdown handling in text filters | keep |
| `sentencex` | sentence segmentation for streaming TTS | keep |
| `numpy` | audio buffers | keep |
| `Pillow` | image frames (vision) | **candidate for removal** — EMMA has no vision |
| `protobuf` | frame serialisation | keep |
| `pydantic` | configuration models | keep |
| `python-dotenv` | `.env` loading | keep |
| the upstream resampler package | audio resampling extension | **license + necessity to verify**; may be replaceable with `scipy.signal.resample_poly` |
| `soundfile` | WAV I/O | keep |
| `openai` | OpenAI-compatible client, also used by the Ollama path | keep (Ollama uses it) |
| `typing_extensions` | typing backports | keep |
| `wait_for2` | fixes `asyncio.wait_for` on Python <3.12 | keep |
| `websockets` | WebSocket transport/client | keep |
| `pyyaml` | configuration | keep |
| `num2words` | text normalisation for TTS | keep |
| `onnxruntime` | Silero VAD and smart-turn inference | keep |

Extras that EMMA will actually install: `livekit`, `livekit-api`, `google`
(Gemini), `kokoro`, `piper`, `whisper`, `websocket`, `silero` (no extra deps),
`ollama` (no extra deps). The remaining 89 extras are not installed.

Observed dependency-friction point in upstream `pyproject.toml`: the `google`
extra carries a conditional pair of `google-genai` pins (`>=2.25` except on
3.13/Linux, where a `pydantic<2.12` conflict with `vonage-video-connector` is
worked around). Since EMMA does not install the Vonage transport, that
conditional can be collapsed to a single pin.

---

## 7. License inventory

### First-party-visible licenses in the tree

| Item | License | Status |
| --- | --- | --- |
| `LICENSE` (root) | BSD-2-Clause, Daily 2024–2026 | VERIFIED |
| Source headers | 614 files with `Copyright` | VERIFIED |
| — Daily (all variants) | 610 files | VERIFIED |
| — NVIDIA Corporation | 6 files | VERIFIED |
| — HuggingFace / librosa / torchaudio | 2 files (whisper feature extraction) | VERIFIED |
| upstream logo image | upstream branding — **must not ship** | VERIFIED (present at audit time) |

The inherited tree is BSD-2-Clause throughout; BSD-2-Clause requires retaining
the copyright notice, the conditions and the disclaimer in source and binary
redistributions. Nothing in the inherited code is copyleft.

### Third-party assets that need separate review

| Asset | Location | Concern |
| --- | --- | --- |
| Silero VAD ONNX weights | `audio/vad/data/*.onnx` | model weights are licensed separately from the MIT runtime code — must be recorded explicitly |
| Smart-turn ONNX weights | `audio/turn/smart_turn/data/*.onnx` | as above |
| Kokoro weights + voices | downloaded at runtime by `kokoro-onnx` | weights Apache-2.0 (verified from the model card); **individual voice packs must be checked separately** |
| Piper voices | downloaded at runtime by `piper-tts` | voice-dependent; must be checked per voice |
| Whisper weights | downloaded at runtime by `faster-whisper` | MIT per OpenAI release; redistributions must be re-verified |
| `frames.proto` | `frames/frames.proto` | generated artifact, inherited BSD header |

Python dependency licenses could **not** be enumerated from the tree alone:
no virtual environment exists and the upstream runtime is not installed, so `pip-licenses`
cannot run. Marked **BLOCKED**; see §9.

### Branding

`README.md`, the upstream logo image, `pyproject.toml` URLs, the package `__init__`
version banner, CLI strings, log identifiers and `COMMUNITY_INTEGRATIONS.md` all
assert the upstream product identity. All of these are application-facing and must be
replaced. The BSD licence permits this; trademark rights are separate, so the
logo and any claim of upstream affiliation must be dropped.

---

## 8. Performance: static observations

No measurements exist yet — the package is not installed and no benchmark has
been run, so **all numbers below are structural observations, not results.**

1. **Resampling happens on both edges.** `BaseInputTransport` resamples transport
   audio to the pipeline rate and `BaseOutputTransport` resamples again on the
   way out (`audio/resamplers/sinc_resampler.py`). A 48 kHz browser stream into a
   16 kHz pipeline is two conversions. The default `sinc_resampler` is a windowed
   function resampler in pure Python/NumPy — this is a plausible CPU hot spot and
   the first candidate for measurement.
2. **Frame-per-frame Python overhead.** Every `InputAudioRawFrame` (20 ms at
   16 kHz = 640 bytes) crosses the queue of every processor. A five-processor
   chain therefore does ~250 queue handoffs per second of audio.
3. **`process_frame` is serial per processor.** Anything slow inside it stalls
   the whole chain; the upstream code correctly pushes work into tasks
   (`SegmentedSTTService` transcribes in the background) but third-party service
   code may not.
4. **Turn strategies run per frame.** Default VAD-based start plus a stop
   strategy per aggregator; cost is small but non-zero and scales with strategy
   count.
5. **No vendor code here manages cost.** There is no budget guard anywhere in the
   tree — billing safety must be built first-party.

### Measurement plan

Before/after numbers will be produced by `scripts/bench/bench_latency.py`
(offline replay with mocked providers) and recorded in
`docs/performance/BENCHMARKS.md` with sample counts and p50/p95/p99. Nothing is
claimed as an improvement without that record.

---

## 9. Test baseline

| Item | Status | Detail |
| --- | --- | --- |
| `uv run pytest` on upstream suite | **BLOCKED** | no `.venv`, upstream runtime not importable, `uv sync --all-extras` needs network access to 98 extras |
| Static test inventory | VERIFIED | 421 files, 7.4 MB, `tests/conftest.py` global config |
| Tests reachable from the EMMA runtime closure | VERIFIED | 143 files, ported by `scripts/emma/vendor.py` |

Baseline numbers must be filled in by the first environment that can install
dependencies. This document will be updated rather than guessed.

---

## 10. Functionality that must be preserved

From the mission brief, the EMMA application (when available) expects:

1. FastAPI backend and its existing HTTP contracts.
2. LiveKit token generation endpoint.
3. Browser-based LiveKit transport with PCM Web Audio playback.
4. Gemini LLM integration, including tool/function calling.
5. Practice-specific retrieval and knowledge base.
6. Authentication, session management, configuration.
7. Existing conversational workflows.

Items 1–7 live in the EMMA application, not here. What the runtime must
therefore guarantee for them is: a **drop-in pipeline construction surface**
(`Pipeline` + `PipelineWorker` + `WorkerRunner` + LiveKit transport + RTVI),
unchanged frame semantics, and stable service constructor signatures for Gemini,
Kokoro, Piper, Whisper and Ollama — so the application's agent code changes only
its import root.

---

## 11. Decisions taken from this audit

1. **Vendor by closure, not by hand.** `scripts/emma/vendor.py` is the single
   source of truth for what the EMMA runtime contains; it is re-runnable.
2. **Keep the worker lifecycle intact.** Bus, registry and job RPC stay; removing
   them is a separate, riskier project with no latency payoff.
3. **Drop multi-agent, flows, CLI, evals, telephony and 58 providers.**
4. **No upstream-authored Python dependency.** The upstream resampler package and
   the prebuilt runner package must go; the former needs a licence check and a
   replacement decision.
5. **Cost and privacy controls are first-party** — the upstream tree has none.
6. **EMMA application integration stays BLOCKED** until its code is available;
   the runtime work proceeds independently.

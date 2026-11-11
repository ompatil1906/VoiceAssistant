# EMMA Receptionist — Runtime Architecture

**Product:** EMMA Receptionist by QuantumLoopAI
**Scope:** the voice runtime that the EMMA application builds on.

## 1. Shape

EMMA keeps the frame-based pipeline model because it is the right model for
real-time voice: data and control flow in one direction through a chain of
processors, every frame can be cancelled, and interruption is a first-class
signal rather than an exception. What it does **not** keep is the upstream
framework's breadth:
63 providers, 11 transports, a multi-agent bus, a CLI, a scraper of example
projects and an eval harness are gone.

```
Browser (AudioWorklet, 16 kHz PCM16)
        │  WebRTC (LiveKit, self-hosted)  ── or ──  WebSocket + FastAPI
        ▼
┌──────────────────────────── emma/transports ────────────────────────────┐
│  BaseInputTransport:  resample → InputAudioRawFrame                      │
│  BaseOutputTransport: mix → resample → paced write                       │
│  RTVI: client events, transcripts, function-call echo                    │
└─────────────────────────────────┬───────────────────────────────────────┘
                                  │ frames downstream
        ┌─────────────────────────▼───────────────────────────┐
        │                  emma/runtime                        │
        │  frames · processor · pipeline · worker · runner      │
        │  clocks · bus (in-process) · registry                │
        └───┬──────────────┬───────────────┬──────────────┬────┘
            │              │               │              │
     emma/speech      emma/turns      emma/intelligence  emma/audio
     VAD              start/stop      LLM + adapters     resamplers
     STT              strategies      context + tools    filters
     TTS              end-of-turn     aggregators        mixers
     expression                                          speech buffers
            │              │               │              │
            └──────────────┴───────┬───────┴──────────────┘
                                   │
        ┌──────────────────────────▼──────────────────────────┐
        │  cross-cutting, first-party                          │
        │  emma/cost      budget guard, usage accounting       │
        │  emma/security  data-policy gate, log redaction      │
        │  emma/observability  metrics, latency, log observers │
        └──────────────────────────────────────────────────────┘
```

## 2. Package layout

| Path | Responsibility |
| --- | --- |
| `emma/runtime/frames.py` | Frame types: data, control, system |
| `emma/runtime/processor.py` | `FrameProcessor` base: queue, task manager, interruption |
| `emma/runtime/pipeline/` | `Pipeline`, `ParallelPipeline`, source/sink |
| `emma/runtime/worker.py` | `PipelineWorker` + `BaseWorker` lifecycle |
| `emma/runtime/runner.py` | `WorkerRunner`: signals, registration, shutdown |
| `emma/runtime/clocks/` | Clock abstraction for paced output |
| `emma/runtime/bus/` | In-process `AsyncQueueBus` used by the worker lifecycle |
| `emma/runtime/registry/` | Worker registry consumed by the lifecycle |
| `emma/audio/` | Resamplers, filters, mixers, speech segment buffering, DTMF |
| `emma/speech/vad/` | Silero VAD analyzer and VAD processor |
| `emma/speech/turn_detection/` | Smart-turn analyzers (optional, local) |
| `emma/speech/stt/` | `base` (STT service contract), `gemini`, `whisper`, `latency` |
| `emma/speech/tts/` | `base` (TTS service contract), `kokoro`, `gemini` |
| `emma/speech/adapters/` | Expression adapters (delivery/sound markup per provider) |
| `emma/speech/expression/` | Expression capabilities and formatting |
| `emma/turns/` | User start/stop strategies, turn controller, mute strategies |
| `emma/intelligence/llm/` | `base`, `gemini`, `ollama`, `openai_compat` |
| `emma/intelligence/context/` | `LLMContext`, aggregators, summarisation |
| `emma/intelligence/adapters/` | Tool/schema conversion per provider |
| `emma/transports/` | `serializer`, `livekit`, `websocket`, `rtvi` |
| `emma/observability/` | Observers, frame metrics, TTFB/usage metrics |
| `emma/support/` | Cross-cutting helpers inherited from upstream |
| `emma/providers/` | Empty by default; the only place a new vendor may be added |
| `emma/testing/utils.py` | `run_test()` and frame-level test helpers |
| `emma/cost/` | **First-party.** Budget guard, provider cost classes, usage ledger |
| `emma/security/` | **First-party.** Data-policy gate, healthcare redaction rules |
| `emma/config.py` | **First-party.** Typed settings loaded from the environment |

## 3. The session lifecycle

1. Application builds processors and a transport, wraps them in
   `PipelineWorker`, registers it with `WorkerRunner`. The application never
   touches the bus, the registry or the job layer directly.
2. `runner.run()` blocks until the worker ends.
3. `StartFrame` propagates; every processor learns the audio rates and
   announces readiness.
4. Transport pulls audio in, resamples once, pushes `InputAudioRawFrame`.
5. VAD turns audio into speaking/not-speaking frames.
6. Turn strategies (`emma/turns/user_start`, `user_stop`) decide when a turn
   begins and ends; a start while the bot speaks raises an interruption.
7. `LLMUserAggregator` collects the user turn into `LLMContext` and emits
   `LLMContextFrame`.
8. `LLMService` streams text tokens; tool calls are dispatched and their results
   re-enter the context.
9. `TTSService` aggregates sentences and streams audio with a context id.
10. Output transport mixes, resamples once, and paces audio to real time.
11. `EndFrame` unwinds: tasks cancelled, buffers flushed, worker ends.

## 4. Barge-in

The interruption path is the one that matters most in a receptionist and gets
the tightest budget:

```
user starts speaking (VAD) → UserStartedSpeakingFrame
    → turn-start strategy confirms the interruption
        → broadcast_interruption()
            ├─ downstream: TTS flushes queued audio for the context id,
            │               output transport drops the pending buffer
            ├─ upstream:   assistant aggregator discards the partial turn
            └─ LLM service: generation task cancelled
```

Rules the implementation holds to:

- The interruption is broadcast **both ways**, so nothing keeps speaking.
- Audio already written to the transport cannot be un-sent; the contract is
  "no *further* audio and no stale buffer", which is what the user perceives.
- Cancelled generations never append to context: a half-sentence that was
  already spoken is kept, a half-sentence that was never heard is discarded.
- Resumption listens for a new turn; it does not re-run the interrupted one.

Measured by `docs/performance/BENCHMARKS.md` (barge-in detection latency,
cancellation-to-silence latency), not asserted.

## 5. Cost and privacy are structural

`emma/cost` and `emma/security` are not optional observers; they sit **inside**
the service construction path.

- Every provider module declares a cost class: `local`, `free_tier`, `trial`,
  `paid`. Unknown pricing is `paid`.
- `budget.assert_allowed(provider, model)` runs before a session is built and
  before each request batch; it raises `BudgetExceeded`, which the pipeline
  turns into a fallback (Gemini → Ollama, Gemini TTS → Kokoro) rather than a
  bill.
- Usage (requests, tokens, characters, audio seconds) is accumulated per session
  and written to a redacted ledger.
- `EMMA_DATA_MODE` is `synthetic` by default. A provider flagged
  `trains_on_input` cannot be constructed in that mode. Only an explicit
  `approved` deployment, after privacy sign-off, may route real patient audio to
  it.
- Log records pass through a redaction filter that masks NHS numbers, phone
  numbers, dates of birth, postcodes and email addresses before they reach any
  sink.

## 6. What is intentionally simple

- **One bot per process.** A worker is a single pipeline; no handoff, no
  fan-out. Multi-agent machinery is removed, not merely unused.
- **Bounded queues everywhere.** Backpressure is an error the application sees,
  not memory growth it discovers.
- **No plugin discovery.** Providers are imported explicitly, so the set of
  reachable billable code paths is a static, auditable fact.
- **No hidden threads.** Everything is `asyncio`; blocking work lives in
  explicit executors with monitored queue depth.

## 7. Known limits

- The EMMA application's contracts are not pinned by tests in this repository
  yet, because the application code is not present (see `INITIAL_AUDIT.md` §1).
- Browser acoustic behaviour (echo, real microphone) has not been verified here;
  it requires a running LiveKit server and a browser session.

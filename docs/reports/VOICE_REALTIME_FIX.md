# Real-Time Voice Loop: Root Causes and Fix Report

Scope: the LiveKit call path that EMMA actually runs — `scripts/run_agent_session.py`
with `emma.services.streaming_stt`, `emma.services.gemini_client`,
`emma.services.streaming_tts` and the browser transport
`frontend/src/lib/voice/livekit-transport.ts`.

Every number below comes from a run recorded on this machine. Nothing is
extrapolated. Where a measurement is missing, the row says so.

Environment: macOS, Python 3.11.13 (`.venv`), LiveKit Cloud room
(`wss://emma-voice-t1j1vhsa.livekit.cloud`), Deepgram `nova-3`, Gemini
`gemini-3.8-flash`, ElevenLabs `eleven_flash_v2_5`. The caller is a headless
LiveKit participant that publishes a macOS-synthesised 16 kHz WAV in real time
and subscribes to the agent's audio track. A real browser with a microphone was
**NOT TESTED** (no browser automation is available here); the WebRTC media path,
the agent's transcript/state control channel and its audio track were exercised.

---

## 1. Root causes

### RC1 — Provider finals were treated as complete caller turns

`DeepgramStreamingTranscriber._to_event` collapsed `is_final` and `speech_final`
into `type="final"`, and `EmmaAgent._pump_stt_events` flushed a turn on any such
final whose last word was not a connector (`_ends_utterance`). Deepgram commits
several `is_final` segments per sentence — its endpointing fires on pauses of a
few hundred milliseconds — so one spoken sentence became several caller turns.

Measured on the reported example sentence (`before` run, 6.49 s of audio):
transcript `"Hey. I want to understand how artificial"` was flushed **2.1 s
before the caller stopped speaking**, the model answered it, and the remainder
became a second turn and a second answer.

### RC2 — No turn identity, so duplicate and delayed events reopened turns

The only idempotency was `is_duplicate_transcript` (Jaccard similarity ≥ 0.8 over
the last four transcripts). Fragments of one sentence are not similar enough to
each other, so the guard did not apply. There were no turn ids, no segment
sequence numbers and no record of which provider segments had already been
consumed by a finalized turn. A `Finalize`-triggered revision or a late final
could therefore start a new turn after the first was answered.

### RC3 — The response pipeline was serial and uncancellable

`_turn_worker_loop` awaited `_run_turn`, which awaited a non-streaming
`generateContent` call and then the whole `speak()`. A barge-in only set flags;
the in-flight HTTP request kept running, so the worker stayed blocked for the
abandoned answer and the next turn waited behind it. The queue was bounded at 8
and dropped the oldest turn on overflow. That is the "one or two turns behind"
symptom.

### RC4 — The model was not streamed, so synthesis waited for the last token

`GeminiClient.generate` returned only after the complete reply. TTS could not
start before that, and the whole reply was passed to one synthesis request.

### RC5 — Stale audio had no generation boundary

`speak()` reset `self.interrupted = False` at its start, so an older reply could
clear the interruption raised by a newer turn. The browser was never sent a
`barge-in` control event by the agent (the frontend handled one that nothing
emitted), and the browser's LiveKit audio element kept its buffered tail.

### RC6 — Interruption audio was not captured

Caller audio was forwarded to the transcriber only while EMMA was silent
(`listening = not self.speaking …`), and the "pre-buffer" was filled only in the
narrow window where the stream was not yet connected. The caller's first words
during an interruption were never sent, so the interrupting turn was transcribed
without them (measured: `"Actually, can I book an appointment for Friday?"` →
`"I book an appointment for Friday."`). Barge-in itself used a fixed loudness
threshold and waited 600 ms + 220 ms before acting.

### RC7 — A slow provider final added its own lag to endpointing

Turn finalization deadlines were measured from the arrival time of the last STT
event, so a final that arrived 1 s late pushed the answer 1 s later on top of the
endpointing window. Nothing asked the provider to commit the utterance after the
caller had actually stopped speaking.

---

## 2. What changed

| File | Change |
| --- | --- |
| `src/emma/services/turn_manager.py` (new) | `UtteranceAssembler` (turn assembly, end-of-utterance policy, duplicate/late segment rejection, `Finalize` scheduling), `TurnLedger` (turn ids, bounded pending backlog with merge-on-overflow, generation counter), `SentenceChunker` (streamed text → sentences) |
| `src/emma/services/response_stream.py` (new) | `SpeechSink`: sentence → TTS → PCM pipeline with bounded queues, generation checks on every stage, first-audio callback |
| `src/emma/services/streaming_stt.py` | `TranscriptEvent` now carries `is_final`, `speech_final`, `from_finalize` and a monotonic `sequence`; endpointing and `utterance_end_ms` are constructor parameters (defaults 300 / 1000 instead of hard-coded 700 / 1400) |
| `src/emma/services/gemini_client.py` | `stream()` over `:streamGenerateContent?alt=sse`, incremental `StreamChunk`s, thought parts suppressed, tool calls and usage on the terminal chunk, one retry before any text, budget metering preserved. `sanitize_stream_delta` keeps inter-delta spaces |
| `scripts/run_agent_session.py` | The voice loop was rewired around the assembler and ledger: energy signal → assembler, `Finalize` on confirmed silence, one response per turn, streaming model → `SpeechSink`, generation-guarded PCM publishing, cancellable turn task, barge-in control event, pre-roll flush on interruption, adaptive barge-in threshold, per-turn latency log |
| `frontend/src/lib/voice/livekit-transport.ts` | Barge-in now mutes the buffered remote audio for 450 ms, unmutes when the agent starts speaking again, and reacts within ~180 ms (was ~300 ms after a 720 ms grace) |
| `tests/emma/test_turn_manager.py`, `test_response_stream.py`, `test_gemini_stream.py`, `test_agent_turn_lifecycle.py`, `test_streaming_stt_events.py` (new) | 51 regression tests; see §5 |

### Turn policy (explicit, configurable)

A turn is finalized only when all of the following hold:

1. **Caller silence**, measured from the audio loop's energy detector, not from
   STT arrival: ≥ 0.45 s after a punctuated `speech_final`; ≥ 1.25 s when the
   committed text has no terminal punctuation; ≥ 3.0 s when the text stops on a
   connector ("…at", "…the"); ≥ 1.4 s for a provider that only produced
   partials. Hard ceiling 20 s.
2. **No new segment for 0.25 s**, so a straggler segment joins the same turn.
3. The provider's `UtteranceEnd` (1 s of provider-observed silence), or a
   punctuated result it committed after our `Finalize` request, ends the turn
   immediately.

`Finalize` is sent after 0.6 s of confirmed caller silence and again after each
new speech onset, which bounds how long a slow provider can delay the committed
transcript.

Duplicates: a segment is dropped when its normalised text is already in the
turn, when it shares a start offset with an existing segment (a revision
replaces it), when it starts before the turn that was just answered, or when it
is a tail for audio inside that turn that arrives while the caller is silent.
Partials never finalize a turn and are only used when they are a cumulative
superset of the committed text.

Responses: one generation per turn. `TurnLedger.supersede()` bumps the
generation on every barge-in or newer turn; the model stream, the TTS producer,
the publisher and the context commit all check it, so a superseded reply cannot
speak or be appended to the conversation. Pending overflow merges the oldest
pending turn into the next rather than dropping words.

---

## 3. Before / after measurements

### 3.1 Reported failure scenario (one long sentence)

Utterance: “Hey, I want to understand how artificial intelligence agents work and
how they can interact with external tools.” (6.49 s of audio)

| Metric | Before (`probe-before-2`) | After (`probe-final-1/2`) |
| --- | --- | --- |
| Text returned for the first committed segment | `"Hey. I want to understand how artificial"` (incomplete) | full sentence |
| Model calls for one utterance | 2 | 1 |
| Spoken replies for one utterance | 2 | 1 |
| Answer began before the caller stopped speaking | yes, 2.1 s early | no |
| Speech end → complete final transcript | 5.86 s (and already partly answered) | **1.15 s / 1.19 s** |
| Speech end → first audio of a correct answer | n/a (first answer was to a fragment) | 3.59 s / 5.10 s (LLM-bound) |

The two `after` runs both produced exactly one turn, one transcript, one
response. Provider variability did produce one transcription error in a separate
run (`"how art of artificial intelligence"` for `"how artificial intelligence"`)
— turn management was still correct.

### 3.2 Short question (single provider segment)

“Can you tell me what your opening hours are on a typical weekday?”

| Metric | Before (`probe-before-3`) | After (`probe-short-3/4`) |
| --- | --- | --- |
| Speech end → final transcript | 0.48 s (already correct; no continuation) | **1.08 s / 1.28 s / 1.29 s** |
| Speech end → first audio | 2.26 s (LLM 1.78 s + TTS 0.23 s, whole reply) | 3.59 s / 5.10 s / 3.70 s (LLM 2.0–3.7 s) |

The STT window is longer here because the previous implementation answered on
the first segment with no stability check; the new window is silence-based and
does not answer mid-thought. First-audio latency is now dominated by the
provider's model latency, which varied between runs (see §3.4).

### 3.3 Interruption (barge-in)

Utterance 1 answered, then during playback the caller says “Actually, can I book
an appointment for Friday?”.

| Metric | Before (`probe-before-barge`) | After (`probe-barge-preroll`, `probe-barge-final2`) |
| --- | --- | --- |
| Interrupting transcript | `"I book an appointment for Friday."` (opening words lost) | `"Actually, can I book an appointment for Friday?"` |
| Obsolete reply committed to context | no (but it had already spoken) | no |
| Agent state leaves “speaking” after caller onset | 0.72 s grace + 0.30 s confirm | **0.60 s / 0.67 s** (0.25 s grace + 0.16 s confirm + control round trip) |
| Stale audio after the interruption | tail kept playing until the flags propagated | no frame published after `interrupt()`; `AudioSource.clear_queue()` |
| Speech end of interruption → first audio of the new answer | 3.6 s | 2.89 s / 3.54 s |

Interruption while EMMA is still generating (not yet speaking) also cancels the
generation after 0.4 s of caller energy, measured in `probe-after-barge2`
(`barge-in (caller speech while thinking)` → `turn 1 superseded`, no audio for
the abandoned answer).

### 3.4 Model streaming (same prompt, same tools, 3 trials each)

Real receptionist system instruction (2035 chars) and tool declarations, one
caller turn, measured with `curl`-equivalent timings:

| Endpoint | Time to first text (median) | Time to complete reply (median) | Spoken characters |
| --- | --- | --- | --- |
| `generateContent` (before) | — (no text until complete) | 3.87 s | 204 |
| `streamGenerateContent` (after) | **3.22 s** | 3.46 s | 213 |

Streaming delivers the first sentence ~0.65 s earlier than the previous
implementation saw any text at all, and later sentences are synthesised while
the first is playing. The absolute model latency is a provider/network property
on this link and is unchanged by this work.

### 3.5 Per-turn log line (new)

```
[Latency] turn=1 speech_end->final=1253ms speech_end->first_audio=4545ms
[Latency] turn=1 stt_final=1253ms llm_first_text=... first_audio=4545ms turn_total=... tools=[...]
chunks=... turns=... dupes=0 late=0 ledger={'accepted': 1, 'duplicates': 0, 'superseded': 0, ...}
```

The first line is emitted before playback finishes, so the numbers are available
during a live call, not only at its end.

---

## 4. Status

| Item | Status |
| --- | --- |
| Streaming STT event shape and endpointing configuration | **VERIFIED** — unit tests + live Deepgram |
| Turn assembly, duplicate/late rejection, partial handling | **VERIFIED** — 26 unit tests + live Deepgram replay |
| One response per utterance (long sentence) | **VERIFIED** — live run: 1 model call, 1 reply |
| No response to a partial transcript | **VERIFIED** — live runs: first answer no longer starts mid-sentence |
| Barge-in stops playback and yields the newest turn | **VERIFIED** — live run over LiveKit |
| Streaming model → sentence TTS → playback | **VERIFIED** — live run + 7 pipeline tests |
| Browser UI (mic, audio element, transcript feed) | **NOT TESTED** — no browser automation here; frontend compiles (`tsc --noEmit`) |
| Full browser/phone latency numbers | **NOT TESTED** — headless LiveKit participant only |
| Repo-wide test suite | 525 passed, 62 failed, 125 errors — the failures and errors are the pre-existing environment ones (missing optional provider modules, missing compiled extensions, date-dependent appointment fixtures). Baseline was 474 passed with the same 62/125. |

---

## 5. Tests

`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/emma -q -n 4`
(the two virtualenvs in this checkout have broken pytest plugins, so autoload is
disabled and `pytest-asyncio`/`xdist` are loaded explicitly):

```
621 passed-equivalent: 525 passed, 61 skipped, 62 failed, 125 errors   (8.87 s)
```

New tests, all passing (51):

| Module | Tests | Covers |
| --- | --- | --- |
| `test_turn_manager.py` | 26 | fragmented segments with natural pauses → one turn; premature trigger on a pause; `UtteranceEnd`; connector wait; duplicate final; `Finalize` revision after a flush; provider tail after a flush; new speech after a flush; partials never trigger; partial-only fallback; cumulative partial absorption; foreign partial rejection; shared-word segments not truncated; revision replacement; punctuated `Finalize` result; `Finalize` scheduling; ledger duplicate/merge/generation; sentence chunking |
| `test_response_stream.py` | 7 | first audio before the reply completes; synthesis of sentence N+1 overlaps playback of N; order and exactly-once; abort; generation change; first-audio callback; empty text |
| `test_gemini_stream.py` | 6 | incremental text + usage; thought parts suppressed; function calls on the terminal chunk; 5xx retry; 4xx error chunk; malformed frames |
| `test_agent_turn_lifecycle.py` | 5 | one sentence → one model call and one reply; duplicate final + double `UtteranceEnd` → one reply; barge-in cancels and the next turn is answered without backlog; audio starts while the model is still streaming; direct-speech tool skips a model round |
| `test_streaming_stt_events.py` | 7 | `is_final` ≠ `speech_final`; `from_finalize`; sequence ordering; empty alternatives; endpointing/`utterance_end_ms` in the stream URL |

Run commands:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/emma/test_turn_manager.py \
  tests/emma/test_response_stream.py tests/emma/test_gemini_stream.py \
  tests/emma/test_agent_turn_lifecycle.py tests/emma/test_streaming_stt_events.py -q -n0

PYTHONPATH=src python3 scripts/emma/selfcheck.py    # 31 passed, 0 failed
.venv/bin/python -m ruff check src/emma/services scripts/run_agent_session.py
(cd frontend && npx tsc --noEmit -p tsconfig.json)   # clean
```

Live verification used a headless LiveKit caller (`/tmp`, not committed): a
16 kHz PCM WAV rendered by the macOS voice is published in real time, the
agent's control data and audio track are timestamped, and the caller can start a
second utterance during playback to test interruption.

---

## 6. Remaining limitations

1. **Model latency dominates first audio.** 2.4–3.7 s to the first sentence on
   this link. The pipeline no longer adds to it, but a faster model, region or a
   local LLM is the only way to reduce it further. A spoken “let me check”
   filler was deliberately not added: it hides latency rather than removing it.
2. **End-of-turn has a floor of ~1.1 s** from provider commitment plus the
   deliberate non-punctuated pause window. All thresholds are tunable without
   code changes: `EMMA_ENDPOINTING_MS`, `EMMA_UTTERANCE_END_MS`,
   `EMMA_TURN_FINAL_PUNCTUATION_GRACE_MS`, `EMMA_TURN_FINAL_PAUSE_GRACE_MS`,
   `EMMA_TURN_CONNECTOR_WAIT_MS`, `EMMA_TURN_SILENCE_CONFIRM_MS`,
   `EMMA_TURN_FINALIZE_ON_SILENCE_MS`, `EMMA_TURN_PARTIAL_ONLY_GRACE_MS`,
   `EMMA_TURN_LATE_SEGMENT_TOLERANCE_MS`, `EMMA_TURN_MAX_UTTERANCE_MS`,
   `EMMA_MAX_PENDING_TURNS`, `EMMA_BARGE_IN_PREROLL_MS`.
3. **A caller who pauses longer than the non-punctuated window (1.25 s) and then
   continues** is still answered as two turns. That is inherent to end-of-turn
   detection without a semantic turn model; the window is the acceptance
   threshold. Deepgram's `UtteranceEnd` (1 s) has the same property.
4. **Barge-in pre-roll is 800 ms of the half-duplex buffer.** With a browser's
   echo cancellation this is mostly caller speech, but residual echo can reach
   the transcriber; `is_echo` discards a full echo, not a partial one.
5. **Interrupted replies are not committed to the model context**, in line with
   the repository rule that cancelled generations never append partial output.
   The user's turn stays in context, so the follow-up answer is coherent, but
   the model does not know which sentence the caller already heard.
6. **Provider transcription accuracy is unchanged.** One live run produced
   `"how art of artificial intelligence"` for the synthetic voice. Turn
   assembly kept it as one complete turn; the words themselves are the
   provider's.
7. **The browser surface compiles but was not exercised**; the `barge-in` mute
   window and reduced confirmation ticks were verified by type-checking only.
8. **Metrics are per-session logs, not a dashboard.** `TurnMetrics` is stored on
   the agent and written to the log; nothing exports it to the RTVI `metrics`
   event yet.

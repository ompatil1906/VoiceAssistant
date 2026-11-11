# Technology Selection — Zero-Cost First

**Verified on:** 2026-02-24. Every row states what was checked and where. A row
that no source could confirm says so; it is never assumed.

Governing rule: the default configuration must be unable to cost money. Anything
billable is an explicitly-enabled, separately-metered integration.

---

## 1. Selection summary

| Layer | Default (zero-cost) | Fallback | Optional, opt-in (billable or trial) |
| --- | --- | --- | --- |
| Transport | LiveKit server, self-hosted (Apache-2.0) | WebSocket + FastAPI | — |
| VAD | Silero VAD, local ONNX | — | — |
| Turn detection | VAD boundaries + transcript-based end-of-turn | smart-turn ONNX, local | — |
| STT | Gemini streaming (free tier) | faster-whisper, local | Deepgram (trial credit) |
| LLM | Gemini Flash (free tier) | Ollama / Gemma, local | OpenAI-compatible endpoints |
| TTS | Kokoro, local ONNX (Apache-2.0 weights) | Gemini TTS (free tier) | — |

Every provider above is metered by the first-party budget guard
(`emma/cost/`). Nothing is reachable that is not listed here.

---

## 2. Transport

**Chosen: LiveKit server, self-hosted.**

| Check | Result | Source |
| --- | --- | --- |
| Server licence | Apache-2.0 | `livekit/livekit` repository metadata |
| Self-hosting supported | Yes, single binary / Docker / Kubernetes; agents join rooms like any participant | LiveKit docs, *Self-hosting overview* |
| Cost | Zero when self-hosted; LiveKit Cloud is optional and not used | as above |
| Browser fit | WebRTC, native browser audio, no plugins | as above |

The inherited LiveKit transport (`emma/transports/livekit/`) is retained. The existing
EMMA deployment already uses self-hosted LiveKit, so this preserves the current
integration rather than replacing it.

WebSocket transport is retained as the second transport: it is dependency-light
(FastAPI/Starlette), it is what a browser can fall back to when WebRTC is
blocked by a network, and many Piper/TTS development flows are simpler over it.

Rejected: Daily and other managed transports (paid), SmallWebRTC (added
complexity with no benefit over LiveKit here), telephony transports (PSTN is
explicitly out of scope at this stage).

---

## 3. Voice activity detection

**Chosen: Silero VAD, local ONNX.**

| Check | Result |
| --- | --- |
| Runs locally | Yes — ONNX Runtime, CPU, no network |
| Licence of runtime code | MIT (silero-vad) |
| Licence of weights | Separate from the runtime; must be recorded in `THIRD_PARTY_NOTICES/` |
| Latency | Frame-level detection at the pipeline rate; no network hop |
| Cost | Zero |

VAD is the gate for barge-in: a false positive truncates EMMA mid-sentence, a
false negative makes barge-in feel dead. Silero's speech probability is used
with configurable start/stop thresholds (`audio/vad/vad_analyzer.py:VADParams`)
so the practice can tune sensitivity instead of arguing about defaults.

Rejected: WebRTC VAD (cheap but markedly more sensitive to background noise in
a reception area); any cloud VAD (adds a network hop to the interruption path,
which is the one place latency is most audible).

**Turn detection** stays first-party: the inherited strategy engine
(`emma/turns/`) combines VAD boundaries, partial transcripts and configurable
silence timeouts. `LocalSmartTurnAnalyzerV3` (ONNX, local) remains available as
an optional semantic end-of-turn detector; it costs CPU, not money.

---

## 4. Speech-to-text

| Candidate | Streaming | Local | Licence | Cost | Verdict |
| --- | --- | --- | --- | --- | --- |
| Gemini API (audio input) | Yes, native streaming | No | Provider terms | Free tier with RPM/RPD caps | **Default** |
| faster-whisper | Pseudo-streaming (segment-based) | Yes | MIT (SYSTRAN) | Zero | **Fallback** |
| Deepgram | Yes | No | Provider terms | Trial credit, then paid | Opt-in only |
| sherpa-onnx | Yes | Yes | Apache-2.0 | Zero | NOT TESTED — kept as future option |
| Browser Web Speech API | Vendor-dependent | No | Browser terms | Free | Rejected: not available in all browsers, no transcript timing control |

Verified: Gemini free-tier limits are rate limits, not unlimited use — published
figures reported for Gemini 2.5 Flash were 10 requests/minute and 250
requests/day, with Flash-Lite higher. **These are the numbers to re-check at
deployment**, because Google changes them; the budget guard reads them from
config rather than hardcoding them.

Honest statement about local STT: `faster-whisper` transcribes **segments**, not
a continuous stream. In this architecture it is driven by
`SegmentedSTTService`, which starts transcription when the user's turn ends, in
a background task. It is therefore *not* low-latency partial transcription, and
this document does not claim it is. It applies when the goal is zero spend or
data must not leave the machine, accepting a slower first response.

---

## 5. Large language model

**Chosen: Gemini Flash (remote, free tier) with Ollama (local) as fallback.**

| Check | Result |
| --- | --- |
| Streaming | Yes — token streaming via the Gemini streaming API |
| Function calling | Yes — mapped through `emma/intelligence/adapters/` |
| Structured output | Yes — JSON schema responses |
| Free tier | Yes, rate-limited (see §4 figures); **not** covered by a consumer Google AI Pro subscription |
| Local alternative | Ollama + Gemma; zero cost, CPU/GPU bound |

Two facts that shape the implementation, both verified:

1. A Google AI *consumer* subscription and Gemini *API* quota are separate
   products. The brief's warning is correct and the runtime never assumes
   otherwise; quota is whatever the API key is entitled to.
2. Google has announced that the API will stop accepting unrestricted API keys
   (reported date: 2026-06-19). Keys must therefore be created with
   restrictions, and the deployment guide says so explicitly.

**Privacy caveat that drives a hard rule:** free-tier API terms generally allow
submitted content to be used to improve the provider's products. Because EMMA
handles NHS patient data, the runtime defaults to
`EMMA_DATA_MODE=synthetic`: providers whose terms permit training on inputs are
flagged `trains_on_input` and the pipeline **refuses to route** to them unless
the deployment is explicitly switched to `approved` after privacy sign-off. That
is enforced in `emma/security/data_policy.py`, not in documentation alone.

Rejected: any provider whose pricing model could not be established from an
official source. "Unknown price" is treated as "paid", therefore
"disabled".

---

## 6. Text-to-speech

| Candidate | Licence | Local | Streaming | Verdict |
| --- | --- | --- | --- | --- |
| **Kokoro-82M via `kokoro-onnx`** | Model weights **Apache-2.0**; runtime **MIT** | Yes | Yes, sentence-chunked | **Default** |
| Gemini TTS | Provider terms | No | Yes | Optional (free tier) |
| Piper | Runtime `piper-tts` **GPL-3.0-or-later**; original MIT `rhasspy/piper` archived | Yes | Yes | **Rejected** |
| Other cloud TTS (ElevenLabs, Cartesia, …) | Provider terms | No | Yes | Rejected: paid |

The Piper decision deserves emphasis because it is a licensing trap rather than
a quality judgement. The `piper-tts` distribution that the upstream `piper` extra
pins (`>=1.3.0`) comes from `OHF-Voice/piper1-gpl` and is **GPL-3.0-or-later**.
Linking it into a commercially distributed, proprietary product would impose
GPL obligations on that product. The previously MIT `rhasspy/piper` repository
is archived ("development has moved"). Piper is therefore removed from the
runtime and from `pyproject.toml`.

Kokoro is chosen because its **weights are Apache-2.0 and explicitly welcome
commercial deployment**, and because it is an 82 M-parameter ONNX model that
runs on CPU — zero marginal cost per minute of speech.

**Voice assets are reviewed separately from the model.** The model card licence
does not automatically licence every voice pack; the deployment checklist
requires recording the licence of each voice actually shipped.

---

## 7. Audio processing

| Concern | Decision |
| --- | --- |
| Client capture | Browser `AudioWorklet`, 16 kHz mono PCM16 — already the EMMA browser path |
| Resampling | One conversion at the ingress boundary, one at egress; no intermediate rates |
| Resampler | Sinc resampler inherited from upstream; the separate upstream-namespaced resampler package is removed — see §8 |
| Buffering | Bounded queues only; output buffer size is derived from the transport frame size |
| Echo cancellation | Delegated to WebRTC (browser AEC) rather than reimplemented; LiveKit enables it on the client |
| Noise suppression | Optional local filters kept if they are licence-clean; no cloud denoiser |

---

## 8. Removing upstream-namespaced dependencies

Two dependencies would otherwise keep an upstream product identity in the
dependency tree:

| Package | Problem | Resolution |
| --- | --- | --- |
| the upstream resampler package | Named after the upstream product, used by the sinc resampler fast path | Replace with the pure-NumPy sinc resampler already in the tree (`emma/audio/resamplers/sinc_resampler.py`); measure the cost, keep the vendor package only if the replacement is unacceptably slow |
| an upstream-namespaced prebuilt runner package | Only used by the removed `runner` extra | Removed with the extra |

---

## 9. What was not verified

Recorded so nobody mistakes silence for approval:

- **Deepgram trial credit** — amount and expiry were not confirmed from an
  official pricing page in this session. Treated as paid and disabled.
- **sherpa-onnx** streaming quality on reception-quality audio — not benchmarked.
- **Kokoro voice-pack licences** for individual voices — the model licence is
  verified; each voice must be recorded before shipping.
- **Gemini rate limits at the time of deployment** — must be read from the
  console for the actual project, not from this document.
- **Silero and smart-turn weight licences** — recorded as "model weights,
  separate licence" in the inventory; the exact text must be included in
  `THIRD_PARTY_NOTICES/` before release.

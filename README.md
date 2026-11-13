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




Ran list root and scripts
Ran read frontend package.json
Analyzed run_agent_session.py:L1-60
Analyzed run_agent_session.py:L1170-1232
Analyzed route.ts:L1-100
Analyzed route.ts:L100-150
Ran check if livekit-server is installed
Ran search for FastAPI usage
Analyzed README.md:L1-100
Analyzed DEPLOYMENT.md:L1-100
Ran check other EMMA repo
Ran list QuantumLoopAI projects
Ran check env port and url vars
Ran check frontend env
Ran check scripts/emma
Ran check active ports

Here are the server start commands for running the EMMA voice system:

---

### 1. LiveKit Server (WebRTC SFU)
Runs the WebRTC media server required for voice streaming (`ws://127.0.0.1:7880`).

- **Using native binary:**
  ```bash
  livekit-server --config livekit.yaml
  ```
- **Or using Docker:**
  ```bash
  docker run --rm -p 7880:7880 -p 7881:7881 -p 7882:7882/udp \
    -v "$PWD/livekit.yaml:/livekit.yaml" livekit/livekit-server --config /livekit.yaml
  ```

---

### 2. Frontend & Token Server (Next.js)
Runs the voice console UI and the `/api/voice/token` endpoint at `http://localhost:3000`.

```bash
cd frontend
npm run dev
```

> **Note:** When you start a call from the frontend console, the `/api/voice/token` route automatically spawns the Python voice agent session (`scripts/run_agent_session.py`) in the background.

---

### 3. Voice Agent Session Runner (Manual / Standalone)
If you want to run or debug the voice agent directly for a specific room without the UI auto-spawning it:

```bash
.venv-313/bin/python scripts/run_agent_session.py --room <ROOM_NAME> --caller '{"firstName": "Sam"}'
```

---

### 4. Local LLM Server (Ollama — Optional)
Only needed if you are using the local offline profile (`EMMA_PROVIDER_PROFILE=local`):

```bash
ollama serve
```
*(Make sure the model is pulled: `ollama pull gemma3:4b`)*

---

### Standard Development Startup Workflow:
1. **Terminal 1:** `livekit-server --config livekit.yaml`
2. **Terminal 2:** `cd frontend && npm run dev`
3. Open `http://localhost:3000` in your browser and click to connect.

---

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
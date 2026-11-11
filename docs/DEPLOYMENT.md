# Deployment Guide

EMMA is deployed as two pieces: the **EMMA application** (FastAPI backend, browser
client, practice knowledge base, authentication) and this **runtime**, which the
application imports to build and run a conversation. This guide covers the
runtime's side of a production deployment.

## 1. Deployment shapes

| Shape | Transport | Requires | Cost |
| --- | --- | --- | --- |
| Local / air-gapped | WebSocket | nothing external | zero |
| Practice server | self-hosted LiveKit + WebSocket fallback | LiveKit server, TLS | zero (own hardware) |
| Managed | LiveKit Cloud | LiveKit account | **billable** — outside the zero-cost policy |

The zero-cost target is shape 1 or 2. Shape 3 is possible but must be an explicit
commercial decision; nothing in the default configuration reaches it.

## 2. LiveKit (self-hosted)

```bash
# Reference deployment; pin the version you validate.
docker run --rm -p 7880:7880 -p 7881:7881 -p 7882:7882/udp \
  -v "$PWD/livekit.yaml:/livekit.yaml" livekit/livekit-server --config /livekit.yaml
```

Requirements:

- TLS termination in front of the server; browsers will not grant microphone
  access on a plain-HTTP origin (localhost excepted).
- A TURN path for restrictive practice networks.
- Keys server-side only. The browser receives a short-lived join token minted by
  the EMMA backend; it never sees `LIVEKIT_API_SECRET`.

## 3. Configuration

```bash
EMMA_PROVIDER_PROFILE=local        # or free, after privacy sign-off
EMMA_DATA_MODE=approved            # production, signed off per provider
EMMA_ALLOW_PAID=false              # keep false
EMMA_MAX_SESSION_USD=0.00          # keep 0.00
EMMA_TRANSPORT=livekit             # auto-selected when LiveKit credentials exist
LIVEKIT_URL=wss://livekit.example.nhs.uk
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...
OLLAMA_BASE_URL=http://127.0.0.1:11434/v1
EMMA_OLLAMA_MODEL=gemma3:4b
EMMA_WHISPER_MODEL=base.en
EMMA_KOKORO_VOICE=af_heart
EMMA_LEDGER_PATH=/var/log/emma/usage.jsonl
```

`.env.example` documents every variable. Never commit `.env`.

## 4. Privacy sign-off checklist

Before setting `EMMA_DATA_MODE=approved`:

1. Every remote provider in the deployment has a privacy/DPIA entry naming it.
2. The provider's current data terms have been read (not remembered): does it
   train on submitted content, where is processing performed, what is retained,
   and for how long?
3. A data processing agreement is in place where the provider requires one.
4. Transcription and generation retention on the EMMA side matches the practice's
   records policy.
5. Log redaction is verified against a sample of real-shaped (synthetic) data:
   NHS numbers, phones, dates of birth, postcodes, emails.
6. The practice has approved the escalation wording EMMA reads out.

Without all six, run `synthetic` and treat the deployment as a demonstration.

## 5. Keys

- `GEMINI_API_KEY` — create it with an API restriction. Google has announced that
  the API will stop accepting unrestricted keys; an unrestricted key is both a
  billing risk and a security finding.
- Rotate `LIVEKIT_API_SECRET` on a schedule; it signs join tokens.
- Store all credentials in the platform's secret store, never in an image layer or
  a repository.

## 6. Budget guard in production

- Set `EMMA_MAX_SESSION_*` from observed session shapes, not guesses.
- `EMMA_ALLOW_PAID=false` makes every billable provider unreachable, whatever
  else is configured. Treat flipping it as a change requiring approval.
- `EMMA_LEDGER_PATH` should point at durable storage; the ledger is the audit
  trail for what was consumed and by which provider.
- Watch for `QuotaExhausted` in logs: it means a session hit a ceiling. That is
  the guard working, not a failure to suppress.

## 7. Process supervision

Run the worker as a supervised process (systemd unit, container, or the EMMA
backend's own supervisor), one conversation per worker. Give it:

- `Restart=on-failure` and a bounded restart rate;
- memory limits that account for the loaded model (Whisper `base.en` and Kokoro
  together are modest; a larger Whisper model is not);
- access to the model cache directory so models are not re-downloaded per start.

## 8. Observability

- `emma.observability` provides frame metrics and latency observers; enable the
  metrics logger in staging to see time-to-first-audio per stage.
- Logs must pass through `emma.security.redaction`. If a log sink bypasses it,
  treat that as a defect.
- Recommended alerts: p95 time-to-first-audio, barge-in detection failures,
  `QuotaExhausted` rate, provider error rate, session duration outliers.

## 9. Rollback

The runtime is a library: pin the EMMA backend to a runtime version and roll back
together. Model files are cached, so a rollback does not require re-downloading
unless the model identifier changed.

## 10. What is not covered here

- Issuing LiveKit tokens, authentication and session management — the EMMA
  application owns these.
- PSTN/telephony — out of scope at this stage; telephony serializers are not part
  of the runtime.
- Clinical decision support — EMMA is an administrative receptionist and must not
  be deployed to give clinical advice.

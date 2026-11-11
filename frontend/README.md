# EMMA voice console — frontend

Next.js 16 (App Router) + Tailwind CSS 4 + shadcn/ui, with the
[Orbkit](https://orbkit.zzzzshawn.cloud) `shdr-21` (Nimbus) WebGL orb as the
assistant's state avatar.

## Primary screen

The main page (`src/app/page.tsx` → `src/components/voice/voice-stage.tsx`) is a
single, full-height black stage with two phases:

1. **Intake.** The `QuantumLoopAI` wordmark sits top left and a phone mockup
   (`src/components/ui/phone-mockup.tsx`) holds the caller form — first name, last name, mobile
   number, reason — with a `Call now` button. Validation is inline; the form
   keeps what was typed across sessions in `localStorage`.
2. **Call.** On `Call now` the phone docks to the bottom right as the live call
   widget (state, timer, mute, end) while the `shdr-21` orb takes the centre in
   a warm-cream / dusty-rose palette, over a small state word (READY /
   CONNECTING / LISTENING / THINKING / SPEAKING / LINE INTERRUPTED) and the live
   exchange in a quiet feed (`SAMPLE` chips mark demo turns).

Everything is driven by the same transport state machine: the orb, the header
pill, the phone screen and the feed cannot disagree. Mute exists only where
microphone audio exists — it is disabled in the demo preview, which opens no
microphone. The docked phone keeps the caller's mobile and reason, hiding them
when the handset renders at phone-booth width (container query, mobile
breakpoints) so the controls stay reachable.

Only the first name is sent to the gateway, as the `caller-first-name` query
parameter — the one intake value `/ws/exotel` accepts. The rest stays in the
browser and on the call screen.

The demo starts from the `Preview demo` pill in the header. Live mic/TTS levels
are passed to the orb through `volumes`.

## Live transport

`Call now` joins a real LiveKit room by default (`NEXT_PUBLIC_VOICE_TRANSPORT=livekit`):
the browser posts the intake first name to `POST /api/voice/token`, joins the
minted room over WebRTC and publishes its microphone. The gateway's agent worker
(`python -m app.integrations.livekit.agent`) joins the same room, runs Deepgram →
controller → Gemini → ElevenLabs, and publishes assistant audio plus the same
transcript/state/barge-in control events the WebSocket path emits. Set
`NEXT_PUBLIC_VOICE_TRANSPORT=websocket` to fall back to the raw `/ws/exotel`
media-stream transport.

## Long-form page

The earlier marketing composition (hero, stat strip, capabilities, runtime,
safety, final CTA) lives at `/classic` (`src/app/classic/page.tsx`).

## Run

```bash
pnpm install
pnpm dev
```

Open <http://localhost:3000>.

## Live gateway

Set the gateway URL in the console's settings dialog, or via an environment
variable:

```bash
cp .env.example .env.local
```

```bash
NEXT_PUBLIC_VOICE_WS_URL=ws://localhost:8000/ws/exotel
```

The console then:

1. asks for microphone permission on **Start live session**;
2. captures 20 ms / 16 kHz PCM16 frames in an `AudioWorklet`;
3. connects to the EMMA gateway with `?sample-rate=16000&control-events=1`;
4. plays returned PCM audio and renders transcript, state and barge-in events.

Browsers cannot attach custom WebSocket headers, so this path assumes the
gateway has `WS_AUTH_TOKEN` unset (the local default). See
`src/lib/voice/gateway-transport.ts` for the protocol notes.

## Layout

| Path | Contents |
| --- | --- |
| `src/app/page.tsx` | Primary minimal stage |
| `src/app/classic/page.tsx` | Full marketing page |
| `src/components/voice/use-voice-session.ts` | Shared transport lifecycle + feed |
| `src/components/voice/voice-stage.tsx` | Intake phone → docked call phone + orb stage |
| `src/components/voice/phone-form-screen.tsx` | Caller intake form inside the phone |
| `src/components/voice/phone-call-screen.tsx` | In-call phone screen (state, timer, mute, end) |
| `src/components/voice/voice-console.tsx` | Full console (used by `/classic`) |
| `src/components/brand/quantumloop-wordmark.tsx` | QuantumLoopAI wordmark you supplied |
| `src/components/ui/` | shadcn components and the installed Orbkit runtime |
| `src/lib/voice/` | Existing EMMA browser audio + transport clients |

`shdr-21` and `orbkit-core` were installed with:

```bash
pnpm dlx shadcn@latest add zzzzshawn/orbkit/shdr-21
```

The orb is MIT-licensed (Orbkit). Its state is driven only by the transport
state machine — `idle`/`listening` → `idle`, `connecting`/`thinking` →
`thinking`, `speaking` → `speaking` — with real mic/TTS levels passed through
`volumes` when a live session is running.

## Checks

```bash
pnpm typecheck
pnpm build
```

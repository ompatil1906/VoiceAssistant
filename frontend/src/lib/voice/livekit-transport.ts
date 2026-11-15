"use client";

/* Live transport — real WebRTC call over LiveKit.

   The browser joins a LiveKit room as the caller; the EMMA agent
   (`scripts/run_agent_session.py`) joins the same room and owns the speech
   pipeline: Deepgram streaming STT → Gemini with clinic tools → ElevenLabs
   streaming TTS. Assistant audio arrives as a normal WebRTC track, and control
   events (transcripts / state / notices) arrive as reliable data messages.

   Two rules matter here:

   1. **One transcriber.** The caller's voice is transcribed by the agent, not
      by the browser. An optional browser transcriber exists only when
      `NEXT_PUBLIC_BROWSER_STT=1` is set explicitly, and it is suspended while
      EMMA speaks: Chrome's recogniser has no echo cancellation relative to the
      room audio, so leaving it running makes EMMA transcribe its own voice and
      answer its own greeting in a loop.
   2. **Audio playback is a user-gesture problem.** LiveKit can attach the
      remote track while the browser still refuses to play it. `room.startAudio()`
      runs after the caller's click and any later blocked state surfaces as a
      "tap to enable audio" notice instead of silent failure.

   Env:
     NEXT_PUBLIC_VOICE_TRANSPORT=<transport>
     NEXT_PUBLIC_VOICE_GATEWAY_HTTP=<token_endpoint>
     NEXT_PUBLIC_LIVEKIT_URL=<livekit_url>
     NEXT_PUBLIC_BROWSER_STT=1 */

import {
  ConnectionState,
  Room,
  RoomEvent,
  Track,
  type RemoteTrack,
  type RemoteTrackPublication,
  type RemoteParticipant,
} from "livekit-client";

import type {
  TranscriptKind,
  VoiceCaller,
  VoiceEvent,
  VoiceSessionContext,
  VoiceState,
  VoiceTransport,
} from "@/lib/voice/types";

/** Assistant states → the UI state machine. */
const ASSISTANT_STATES: Record<string, VoiceState> = {
  listening: "listening",
  thinking: "thinking",
  speaking: "speaking",
};

// Every stage of the join gets a deadline. A sleeping gateway or an unanswered
// microphone prompt must not leave the caller on an unexplained spinner.
const TOKEN_TIMEOUT_MS = 12_000;
// Retry budget: a hosted gateway that was asleep answers once it has booted,
// which takes far longer than the first attempt's fast-fail deadline.
const TOKEN_RETRY_TIMEOUT_MS = 45_000;
const ROOM_CONNECT_TIMEOUT_MS = 20_000;
const MIC_TIMEOUT_MS = 20_000;

const BARGE_IN_LEVEL = 0.5;
// React within ~180 ms of sustained caller energy, after a short grace period
// that lets echo cancellation settle on EMMA's first syllable.
const BARGE_IN_TICKS = 3;
const BARGE_IN_GRACE_TICKS = 6;
const MONITOR_MS = 60;
// A WebRTC audio element can still hold a few hundred milliseconds of the
// obsolete reply when the agent stops publishing. Muting briefly drops it.
const BARGE_IN_MUTE_MS = 450;

const BROWSER_STT_ENABLED =
  process.env.NEXT_PUBLIC_BROWSER_STT === "1";

interface ControlMessage {
  event?: string;
  kind?: string;
  id?: string;
  text?: string;
  state?: string;
}

export interface LiveKitTransportOptions {
  /** HTTP origin of the gateway (token endpoint lives here). */
  gatewayHttpUrl: string;
  /** Optional override; otherwise taken from the token response. */
  livekitUrl?: string;
  callerFirstName?: string;
  caller?: VoiceCaller;
  roomName?: string;
}

function randomId(prefix: string): string {
  const uuid =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : Math.random().toString(16).slice(2);
  return `${prefix}-${uuid}`;
}

function httpToWs(url: string): string {
  return url.replace(/^http/i, "ws").replace(/\/$/, "");
}

function trimUrl(url: string): string {
  return url.replace(/\/+$/, "");
}

/** Fail a join stage that never settles, so the UI cannot wait forever. */
function withDeadline<T>(work: Promise<T>, ms: number, label: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = window.setTimeout(() => {
      reject(new Error(`${label} did not complete within ${Math.round(ms / 1000)}s`));
    }, ms);
    work.then(
      (value) => {
        window.clearTimeout(timer);
        resolve(value);
      },
      (cause: unknown) => {
        window.clearTimeout(timer);
        reject(cause);
      },
    );
  });
}

interface BrowserRecognitionWindow extends Window {
  webkitSpeechRecognition?: new () => unknown;
  SpeechRecognition?: new () => unknown;
}

export function createLiveKitTransport(
  options: LiveKitTransportOptions,
): VoiceTransport {
  const { gatewayHttpUrl, callerFirstName } = options;
  const roomName =
    options.roomName ??
    `emma-ui-${Math.random().toString(36).slice(2, 10)}`;

  let room: Room | null = null;
  let ctx: VoiceSessionContext | null = null;
  let active = false;
  let muted = false;
  const muteHandlers = new Set<(muted: boolean) => void>();

  let analyserTimer: number | null = null;
  let audioContext: AudioContext | null = null;
  let analyser: AnalyserNode | null = null;
  let buffer: Uint8Array<ArrayBuffer> | null = null;
  let smoothLevel = 0;
  let speechTicks = 0;
  let agentSpeaking = false;
  let speakingTicks = 0;

  // Optional browser transcriber (disabled by default — see the header note).
  let recognition: { stop(): void; start(): void } | null = null;

  // A failed join also tears the room down; only a disconnect after a
  // successful join means the call ended normally.
  let joined = false;

  const audioElements = new Set<HTMLMediaElement>();
  // Assistant lines streamed word by word: line id → text received so far.
  const assistantDrafts = new Map<string, string>();
  let currentCallerLineId: string | null = null;
  let bargeInMuteTimer: number | null = null;
  let unwirePlaybackGesture: (() => void) | null = null;
  // A soft ringback tone fills the gateway/agent startup, which is the only
  // part of the join the caller cannot see. It stops the moment EMMA publishes.
  let ringbackContext: AudioContext | null = null;
  let ringbackSource: AudioBufferSourceNode | null = null;
  let ringbackTimer: number | null = null;

  /** Drop audio the browser has already buffered for an obsolete reply. */
  const muteBufferedPlayback = () => {
    for (const element of audioElements) element.muted = true;
    if (bargeInMuteTimer !== null) window.clearTimeout(bargeInMuteTimer);
    bargeInMuteTimer = window.setTimeout(() => {
      bargeInMuteTimer = null;
      for (const element of audioElements) element.muted = false;
    }, BARGE_IN_MUTE_MS);
  };

  const unmutePlayback = () => {
    if (bargeInMuteTimer !== null) {
      window.clearTimeout(bargeInMuteTimer);
      bargeInMuteTimer = null;
    }
    for (const element of audioElements) element.muted = false;
  };

  const emit = (event: VoiceEvent) => ctx?.onEvent(event);

  const setState = (state: VoiceState) => emit({ type: "state", state });

  const notice = (text: string) =>
    emit({
      type: "notice",
      line: { id: randomId("notice"), text, sample: false },
    });

  const closeSpeechRecognition = () => {
    if (recognition) {
      try {
        recognition.stop();
      } catch {
        /* already stopped */
      }
      recognition = null;
    }
  };

  const startSpeechRecognition = () => {
    if (!BROWSER_STT_ENABLED) return;
    closeSpeechRecognition();
    const win =
      typeof window !== "undefined"
        ? (window as unknown as BrowserRecognitionWindow)
        : null;
    const SpeechRec = win?.SpeechRecognition || win?.webkitSpeechRecognition;
    if (!SpeechRec) return;

    try {
      const recogniser = new SpeechRec() as {
        continuous: boolean;
        interimResults: boolean;
        lang: string;
        onresult: ((event: unknown) => void) | null;
        onerror: ((event: unknown) => void) | null;
        onend: (() => void) | null;
        start(): void;
        stop(): void;
      };
      recognition = recogniser;
      recogniser.continuous = true;
      recogniser.interimResults = true;
      recogniser.lang = "en-GB";

      recogniser.onresult = (raw: unknown) => {
        if (!active || muted || !room) return;
        // Never feed the browser's transcript while EMMA is speaking: it is
        // usually EMMA hearing its own audio through the speakers.
        if (agentSpeaking) return;
        const event = raw as {
          resultIndex: number;
          results: ArrayLike<ArrayLike<{ transcript: string }> & { isFinal: boolean }>;
        };
        for (let i = event.resultIndex; i < event.results.length; i += 1) {
          const result = event.results[i];
          const text = result[0]?.transcript?.trim();
          if (!text) continue;
          emit({
            type: "transcript",
            line: {
              id: randomId("u"),
              kind: result.isFinal ? "final" : "partial",
              text,
              sample: false,
            },
          });
          if (result.isFinal) {
            void room.localParticipant
              .publishData(
                new TextEncoder().encode(
                  JSON.stringify({ event: "user_input", text }),
                ),
                { reliable: true },
              )
              .catch(() => undefined);
          }
        }
      };

      recogniser.onerror = () => {
        /* Browser STT is a convenience; the agent transcribes as well. */
      };

      recogniser.onend = () => {
        if (active && recognition === recogniser && !agentSpeaking) {
          try {
            recogniser.start();
          } catch {
            /* recognition can only be restarted after a tick */
          }
        }
      };

      recogniser.start();
    } catch {
      /* no browser transcriber available */
    }
  };

  const pauseSpeechRecognition = () => {
    if (!recognition) return;
    try {
      recognition.stop();
    } catch {
      /* already stopped */
    }
  };

  const closeLevelMeter = () => {
    closeSpeechRecognition();
    if (analyserTimer !== null) {
      window.clearInterval(analyserTimer);
      analyserTimer = null;
    }
    analyser = null;
    buffer = null;
    smoothLevel = 0;
    speechTicks = 0;
    speakingTicks = 0;
    const context = audioContext;
    audioContext = null;
    if (context && context.state !== "closed") void context.close();
  };

  const localMicTrack = (): MediaStreamTrack | null => {
    const publication = room?.localParticipant.getTrackPublication(
      Track.Source.Microphone,
    );
    return publication?.track?.mediaStreamTrack ?? null;
  };

  /** RMS meter over the published microphone, mirrored by `level()`. */
  const startLevelMeter = () => {
    closeLevelMeter();
    const track = localMicTrack();
    if (!track) return;

    try {
      audioContext = new AudioContext();
      const source = audioContext.createMediaStreamSource(
        new MediaStream([track]),
      );
      analyser = audioContext.createAnalyser();
      analyser.fftSize = 512;
      source.connect(analyser);
      buffer = new Uint8Array(new ArrayBuffer(analyser.fftSize));
    } catch {
      return;
    }

    analyserTimer = window.setInterval(() => {
      if (!active || !analyser || !buffer) return;
      analyser.getByteTimeDomainData(buffer);
      let sum = 0;
      for (const value of buffer) {
        const centred = (value - 128) / 128;
        sum += centred * centred;
      }
      const rms = Math.sqrt(sum / buffer.length);
      smoothLevel = smoothLevel * 0.6 + Math.min(1, rms * 3.2) * 0.4;

      if (!agentSpeaking) {
        speechTicks = 0;
        speakingTicks = 0;
        if (BROWSER_STT_ENABLED && !recognition) startSpeechRecognition();
        return;
      }

      // Barge-in: sustained caller energy over EMMA's voice cancels local
      // playback immediately and tells the agent to stop generating.
      speakingTicks += 1;
      if (muted || speakingTicks <= BARGE_IN_GRACE_TICKS) return;
      if (smoothLevel >= BARGE_IN_LEVEL) {
        speechTicks += 1;
        if (speechTicks >= BARGE_IN_TICKS && room) {
          speechTicks = 0;
          // Stop reporting until the agent actually starts its next turn.
          agentSpeaking = false;
          pauseSpeechRecognition();
          void room.localParticipant
            .publishData(
              new TextEncoder().encode(JSON.stringify({ event: "barge-in" })),
              { reliable: true },
            )
            .catch(() => undefined);
        }
      } else {
        speechTicks = 0;
      }
    }, MONITOR_MS);
  };

  const handleControl = (payload: Uint8Array) => {
    let message: ControlMessage;
    try {
      message = JSON.parse(new TextDecoder().decode(payload)) as ControlMessage;
    } catch {
      return;
    }

    switch (message.event) {
      case "transcript": {
        if (typeof message.text !== "string" || !message.text.trim()) return;
        const kind: TranscriptKind =
          message.kind === "partial" ? "partial" : "final";
        // Partials for one utterance reuse an id so the line grows in place
        // instead of stacking a new paragraph per Deepgram result.
        const id =
          typeof message.id === "string" && message.id
            ? message.id
            : (currentCallerLineId ??= randomId("t"));
        if (kind === "final") currentCallerLineId = null;
        emit({
          type: "transcript",
          line: { id, kind, text: message.text, sample: false },
        });
        return;
      }
      case "assistant-start": {
        stopRingback();
        if (typeof message.id !== "string") return;
        assistantDrafts.set(message.id, "");
        return;
      }
      case "assistant-delta": {
        if (typeof message.id !== "string" || !message.text) return;
        const previous = assistantDrafts.get(message.id) ?? "";
        const text = previous ? `${previous} ${message.text}` : message.text;
        assistantDrafts.set(message.id, text);
        emit({
          type: "transcript",
          line: { id: message.id, kind: "agent", text, sample: false },
        });
        return;
      }
      case "assistant-end": {
        if (typeof message.id !== "string") return;
        const spoken = assistantDrafts.get(message.id) ?? "";
        assistantDrafts.delete(message.id);
        const text =
          typeof message.text === "string" && message.text.trim()
            ? message.text
            : spoken;
        if (!text.trim()) return;
        emit({
          type: "transcript",
          line: { id: message.id, kind: "agent", text, sample: false },
        });
        return;
      }
      case "assistant": {
        stopRingback();
        if (typeof message.text !== "string" || !message.text.trim()) return;
        emit({
          type: "transcript",
          line: {
            id: randomId("a"),
            kind: "agent",
            text: message.text,
            sample: false,
          },
        });
        return;
      }
      case "notice": {
        if (typeof message.text !== "string" || !message.text.trim()) return;
        notice(message.text);
        return;
      }
      case "state": {
        const mapped = message.state ? ASSISTANT_STATES[message.state] : undefined;
        if (!mapped) return;
        agentSpeaking = mapped === "speaking";
        if (agentSpeaking) {
          stopRingback();
          speakingTicks = 0;
          speechTicks = 0;
          unmutePlayback();
          // The browser transcriber must not hear EMMA's own voice.
          pauseSpeechRecognition();
        } else if (!muted) {
          startSpeechRecognition();
        }
        setState(mapped);
        return;
      }
      case "barge-in": {
        agentSpeaking = false;
        speechTicks = 0;
        muteBufferedPlayback();
        return;
      }
      default:
        return;
    }
  };

  /** Stop the waiting tone; safe to call at any stage, including never started. */
  const stopRingback = () => {
    if (ringbackTimer !== null) {
      window.clearTimeout(ringbackTimer);
      ringbackTimer = null;
    }
    try {
      ringbackSource?.stop();
    } catch {
      /* already stopped */
    }
    ringbackSource = null;
    const context = ringbackContext;
    ringbackContext = null;
    if (context && context.state !== "closed") void context.close();
  };

  /** Play a quiet phone ringback while the room and agent come up. */
  const startRingback = () => {
    if (ringbackContext) return;
    try {
      const Ctor =
        window.AudioContext ??
        (window as unknown as { webkitAudioContext?: typeof AudioContext })
          .webkitAudioContext;
      if (!Ctor) return;
      const context = new Ctor();
      void context.resume().catch(() => undefined);

      // One second of the classic 440 + 480 Hz ring, two seconds of silence.
      const sampleRate = context.sampleRate;
      const toneSeconds = 1;
      const buffer = context.createBuffer(1, Math.floor(sampleRate * 3), sampleRate);
      const samples = buffer.getChannelData(0);
      const toneSamples = Math.floor(sampleRate * toneSeconds);
      for (let index = 0; index < toneSamples; index += 1) {
        const t = index / sampleRate;
        // Short fades avoid a click at the start and end of each ring.
        const fade = Math.min(1, t / 0.04, (toneSeconds - t) / 0.05);
        samples[index] =
          fade *
          0.5 *
          (Math.sin(2 * Math.PI * 440 * t) + Math.sin(2 * Math.PI * 480 * t));
      }

      const source = context.createBufferSource();
      source.buffer = buffer;
      source.loop = true;
      const gain = context.createGain();
      // Quiet enough to sit under speech and never drive the microphone.
      gain.gain.value = 0.045;
      source.connect(gain).connect(context.destination);
      source.start();

      ringbackContext = context;
      ringbackSource = source;
      // Never ring forever if the join stalls without reporting an error.
      ringbackTimer = window.setTimeout(stopRingback, 90_000);
    } catch {
      /* audio output is optional; notices still explain the wait */
    }
  };

  /** Let the next tap unblock audio when autoplay policy refuses it. */
  const enablePlaybackOnGesture = () => {
    if (unwirePlaybackGesture) return;
    const handler = () => {
      if (room) void room.startAudio().catch(() => undefined);
    };
    document.addEventListener("pointerdown", handler, { once: true });
    unwirePlaybackGesture = () => document.removeEventListener("pointerdown", handler);
  };

  const clearPlaybackGesture = () => {
    unwirePlaybackGesture?.();
    unwirePlaybackGesture = null;
  };

  const attachRemoteAudio = (track: RemoteTrack) => {
    // The agent is in the room and about to speak: the waiting tone is over.
    stopRingback();
    const element = track.attach();
    element.autoplay = true;
    element.setAttribute("playsinline", "true");
    element.style.display = "none";
    document.body.appendChild(element);
    audioElements.add(element);
    void element.play?.().catch(() => {
      notice("Tap the page once to enable EMMA's voice output.");
      enablePlaybackOnGesture();
    });
  };

  const clearAudioElements = () => {
    if (bargeInMuteTimer !== null) {
      window.clearTimeout(bargeInMuteTimer);
      bargeInMuteTimer = null;
    }
    for (const element of audioElements) {
      try {
        element.pause();
        element.removeAttribute("src");
      } catch {
        /* detached already */
      }
      element.remove();
    }
    audioElements.clear();
  };

  /** Announce a stage only when it is slow, so a fast join stays quiet. */
  const slowStageNotice = (text: string, delayMs = 1_500) => {
    const timer = window.setTimeout(() => {
      if (active) notice(text);
    }, delayMs);
    return () => window.clearTimeout(timer);
  };

  const start = (session: VoiceSessionContext) => {
    ctx = session;
    if (active) return;
    active = true;
    joined = false;
    assistantDrafts.clear();
    setState("connecting");
    startRingback();

    void (async () => {
      try {
        // The token endpoint also launches the agent, so it is the first stage
        // that can stall: a sleeping hosted gateway answers only after its cold
        // start. Abort and retry once, with the same room name, so the caller
        // sees progress instead of an indefinite "connecting".
        const tokenUrl = `${trimUrl(gatewayHttpUrl)}/api/voice/token`;
        const tokenBody = JSON.stringify({
          room_name: roomName,
          caller:
            options.caller ??
            (callerFirstName ? { firstName: callerFirstName } : undefined),
        });

        let token: { token?: string; livekit_url?: string } | null = null;
        let lastCause: unknown = null;
        const budgets = [TOKEN_TIMEOUT_MS, TOKEN_RETRY_TIMEOUT_MS];

        const stopContactNotice = slowStageNotice("Contacting the EMMA gateway…");

        for (let attempt = 0; attempt < budgets.length && active; attempt += 1) {
          if (attempt > 0) {
            notice("The voice gateway is still starting up — retrying…");
          }
          const controller = new AbortController();
          const timer = window.setTimeout(
            () => controller.abort(),
            budgets[attempt],
          );
          try {
            const response = await fetch(tokenUrl, {
              method: "POST",
              headers: { "content-type": "application/json" },
              body: tokenBody,
              signal: controller.signal,
            });

            if (!response.ok) {
              // A definitive answer (bad secret, no endpoint) will not improve
              // on retry, so surface it immediately.
              const detail =
                response.status === 404
                  ? "Gateway has no LiveKit token endpoint"
                  : `Gateway refused the call token (HTTP ${response.status})`;
              throw new Error(detail);
            }

            token = (await response.json()) as {
              token?: string;
              livekit_url?: string;
            };
            break;
          } catch (cause) {
            lastCause = cause;
            const aborted = controller.signal.aborted;
            if (!aborted) break; // real answer; do not retry
          } finally {
            window.clearTimeout(timer);
          }
        }

        if (!active) return;
        if (!token) {
          stopContactNotice();
          const host = trimUrl(gatewayHttpUrl);
          const timedOut =
            lastCause instanceof Error && lastCause.name === "AbortError";
          throw new Error(
            timedOut
              ? `No answer from the voice gateway at ${host} after ${Math.round(
                  TOKEN_TIMEOUT_MS / 1000,
                )}s and ${Math.round(
                  TOKEN_RETRY_TIMEOUT_MS / 1000,
                )}s. It may still be waking up — wait a minute and tap Call again.`
              : `${host} could not be reached (${
                  lastCause instanceof Error ? lastCause.message : "network error"
                }). Check the gateway is running, then tap Call again.`,
          );
        }

        if (!token.token) throw new Error("Gateway returned no room token");
        stopContactNotice();
        let url =
          token.livekit_url ??
          options.livekitUrl ??
          httpToWs(gatewayHttpUrl);

        if (typeof window !== "undefined" && window.location.protocol === "https:") {
          if (url.includes("127.0.0.1") || url.includes("localhost")) {
            throw new Error(
              "HTTPS sites cannot connect to local LiveKit (127.0.0.1). Configure a public LiveKit URL (e.g. LiveKit Cloud 'wss://<project>.livekit.cloud') in your Render environment variables."
            );
          }
          if (url.startsWith("ws://")) {
            url = url.replace(/^ws:\/\//i, "wss://");
          }
        }

        const nextRoom = new Room({
          adaptiveStream: false,
          dynacast: false,
          // Echo cancellation belongs to the browser: the caller's microphone
          // must not contain EMMA's own voice.
          audioCaptureDefaults: {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
          },
        });
        room = nextRoom;

        nextRoom.on(RoomEvent.DataReceived, (payload: Uint8Array) => {
          handleControl(payload);
        });
        nextRoom.on(
          RoomEvent.TrackSubscribed,
          (track: RemoteTrack, _pub: RemoteTrackPublication, _p: RemoteParticipant) => {
            if (track.kind === Track.Kind.Audio) attachRemoteAudio(track);
          },
        );
        nextRoom.on(RoomEvent.TrackUnsubscribed, (track: RemoteTrack) => {
          for (const element of track.detach()) {
            audioElements.delete(element);
            element.remove();
          }
        });
        nextRoom.on(RoomEvent.AudioPlaybackStatusChanged, () => {
          if (!nextRoom.canPlaybackAudio) {
            notice("Tap the page once to enable EMMA's voice output.");
            enablePlaybackOnGesture();
          }
        });
        nextRoom.on(RoomEvent.Disconnected, () => {
          if (!active) return;
          // The join failed: leave the error to the connect() catch so the
          // caller sees why instead of the form silently reappearing.
          if (!joined) return;
          active = false;
          assistantDrafts.clear();
          stopRingback();
          closeLevelMeter();
          clearPlaybackGesture();
          clearAudioElements();
          notice("Room closed");
          emit({ type: "ended", reason: "done" });
        });

        const stopJoinNotice = slowStageNotice("Joining the LiveKit room…");
        try {
          await withDeadline(
            nextRoom.connect(url, token.token),
            ROOM_CONNECT_TIMEOUT_MS,
            `Joining ${url}`,
          );
        } catch (cause) {
          void nextRoom.disconnect();
          throw cause;
        } finally {
          stopJoinNotice();
        }
        joined = true;

        // Autoplay policy: this runs inside the caller's click gesture chain,
        // so the room is allowed to play the agent's audio track.
        try {
          await nextRoom.startAudio();
        } catch {
          notice("Tap the page once to enable EMMA's voice output.");
          enablePlaybackOnGesture();
        }

        const stopMicNotice = slowStageNotice("Waiting for microphone permission…");
        let micLive = true;
        try {
          await withDeadline(
            nextRoom.localParticipant.setMicrophoneEnabled(true, {
              echoCancellation: true,
              noiseSuppression: true,
              autoGainControl: true,
            }),
            MIC_TIMEOUT_MS,
            "The microphone",
          );
        } catch (cause) {
          // The line is up and EMMA can speak, so keep the call. Say plainly
          // that the caller cannot be heard yet and how to fix it.
          micLive = false;
          const reason =
            cause instanceof Error ? cause.message : "microphone unavailable";
          notice(
            `Microphone not started (${reason}). Allow microphone access for this site, then start the call again.`,
          );
        } finally {
          stopMicNotice();
        }

        if (nextRoom.state === ConnectionState.Disconnected) return;

        if (micLive) startLevelMeter();
        startSpeechRecognition();
        setState("listening");
        if (micLive) notice("Joined the LiveKit room — streaming microphone over WebRTC");
      } catch (cause) {
        if (!active) return;
        active = false;
        stopRingback();
        closeLevelMeter();
        clearPlaybackGesture();
        clearAudioElements();
        room = null;
        joined = false;
        const message =
          cause instanceof Error ? cause.message : "Could not join the call";
        emit({ type: "error", message });
        emit({ type: "ended", reason: "error" });
      }
    })();
  };

  const stop = () => {
    if (!active && !room) {
      emit({ type: "ended", reason: "user" });
      return;
    }
    const current = room;
    if (current) {
      void current.localParticipant
        .publishData(
          new TextEncoder().encode(JSON.stringify({ event: "end-call" })),
          { reliable: true },
        )
        .catch(() => undefined);
    }
    active = false;
    joined = false;
    stopRingback();
    closeLevelMeter();
    clearPlaybackGesture();
    clearAudioElements();
    room = null;
    if (current) void current.disconnect();
    assistantDrafts.clear();
    emit({ type: "ended", reason: "user" });
  };

  const setMuted = (next: boolean) => {
    if (muted === next) return;
    muted = next;
    const current = room;
    if (current) {
      void current.localParticipant
        .setMicrophoneEnabled(!next)
        .catch(() => undefined);
    }
    if (next) {
      pauseSpeechRecognition();
    } else if (!agentSpeaking) {
      startSpeechRecognition();
    }
    for (const handler of muteHandlers) handler(next);
  };

  return {
    mode: "live",
    label: "LiveKit room",
    start,
    stop,
    setMuted,
    isMuted: () => muted,
    level: () => (muted ? 0 : smoothLevel),
    onMuteChange(handler: (value: boolean) => void) {
      muteHandlers.add(handler);
      return () => muteHandlers.delete(handler);
    },
  };
}

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
     NEXT_PUBLIC_VOICE_TRANSPORT=livekit|websocket   (default: livekit)
     NEXT_PUBLIC_VOICE_GATEWAY_HTTP=http://localhost:3000   (token endpoint)
     NEXT_PUBLIC_LIVEKIT_URL=ws://127.0.0.1:7880     (fallback if the token
                                                      response omits it)
     NEXT_PUBLIC_BROWSER_STT=1                       (optional fast transcriber) */

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

const BARGE_IN_LEVEL = 0.5;
const BARGE_IN_TICKS = 5;
const BARGE_IN_GRACE_TICKS = 12;
const MONITOR_MS = 60;

const BROWSER_STT_ENABLED =
  process.env.NEXT_PUBLIC_BROWSER_STT === "1";

interface ControlMessage {
  event?: string;
  kind?: string;
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

  const audioElements = new Set<HTMLMediaElement>();
  let unwirePlaybackGesture: (() => void) | null = null;

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
        emit({
          type: "transcript",
          line: { id: randomId("t"), kind, text: message.text, sample: false },
        });
        return;
      }
      case "assistant": {
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
          speakingTicks = 0;
          speechTicks = 0;
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
        return;
      }
      default:
        return;
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

  const start = (session: VoiceSessionContext) => {
    ctx = session;
    if (active) return;
    active = true;
    setState("connecting");

    void (async () => {
      try {
        const response = await fetch(`${gatewayHttpUrl}/api/voice/token`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            room_name: roomName,
            caller:
              options.caller ??
              (callerFirstName ? { firstName: callerFirstName } : undefined),
          }),
        });

        if (!response.ok) {
          const detail =
            response.status === 404
              ? "Gateway has no LiveKit token endpoint"
              : `Gateway refused the call token (HTTP ${response.status})`;
          throw new Error(detail);
        }

        const token = (await response.json()) as {
          token?: string;
          livekit_url?: string;
        };
        if (!token.token) throw new Error("Gateway returned no room token");
        if (!active) return;

        const url =
          token.livekit_url ??
          options.livekitUrl ??
          httpToWs(gatewayHttpUrl);

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
          active = false;
          closeLevelMeter();
          clearPlaybackGesture();
          clearAudioElements();
          notice("Room closed");
          emit({ type: "ended", reason: "done" });
        });

        await nextRoom.connect(url, token.token);

        // Autoplay policy: this runs inside the caller's click gesture chain,
        // so the room is allowed to play the agent's audio track.
        try {
          await nextRoom.startAudio();
        } catch {
          notice("Tap the page once to enable EMMA's voice output.");
          enablePlaybackOnGesture();
        }

        await nextRoom.localParticipant.setMicrophoneEnabled(true, {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        });

        if (nextRoom.state === ConnectionState.Disconnected) return;

        startLevelMeter();
        startSpeechRecognition();
        setState("listening");
        notice("Joined the LiveKit room — streaming microphone over WebRTC");
      } catch (cause) {
        if (!active) return;
        active = false;
        closeLevelMeter();
        clearPlaybackGesture();
        clearAudioElements();
        room = null;
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
    closeLevelMeter();
    clearPlaybackGesture();
    clearAudioElements();
    room = null;
    if (current) void current.disconnect();
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

"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  openAudioPipeline,
  type AudioPipelineHandle,
} from "@/lib/voice/audio-pipeline";
import { createDemoTransport } from "@/lib/voice/demo-transport";
import { createGatewayTransport } from "@/lib/voice/gateway-transport";
import { createLiveKitTransport } from "@/lib/voice/livekit-transport";
import type {
  TranscriptLine,
  VoiceCaller,
  VoiceEvent,
  VoiceMetrics,
  VoiceNoticeLine,
  VoiceState,
  VoiceTransport,
} from "@/lib/voice/types";

export type VoiceMode = "demo" | "live";

const EMPTY_CALLER: VoiceCaller = {
  firstName: "",
  lastName: "",
  email: "",
  mobile: "",
  reason: "",
};

/** One line of the on-stage feed: the caller, EMMA, or a system note. */
export interface VoiceFeedEntry {
  id: string;
  role: "caller" | "agent" | "notice" | "error";
  text: string;
  sample: boolean;
}

const DEFAULT_GATEWAY =
  process.env.NEXT_PUBLIC_VOICE_WS_URL ?? "ws://localhost:8000/ws/exotel";

/** Where the gateway mints LiveKit room tokens (and serves diagnostics). */
const GATEWAY_HTTP =
  process.env.NEXT_PUBLIC_VOICE_GATEWAY_HTTP ??
  DEFAULT_GATEWAY.replace(/^ws/i, "http").replace(/\/ws\/.*$/, "");

/** `livekit` (default): real WebRTC call through the EMMA agent room.
    `websocket`: the raw `/ws/exotel` media-stream transport. */
const LIVE_TRANSPORT: "livekit" | "websocket" =
  process.env.NEXT_PUBLIC_VOICE_TRANSPORT === "websocket"
    ? "websocket"
    : "livekit";

const FEED_LIMIT = 5;

/** Feed keys must stay unique: a failed line reports twice (socket error, then
    close), and both land in the same millisecond. */
let errorFeedSeq = 0;
function nextErrorId(): string {
  errorFeedSeq += 1;
  return `error-${errorFeedSeq}`;
}

function upsertLine(prev: TranscriptLine[], line: TranscriptLine) {
  const partialIndex = prev.findIndex((entry) => entry.kind === "partial");

  if (line.kind === "partial" || line.kind === "final") {
    if (partialIndex >= 0) {
      const next = [...prev];
      next[partialIndex] = line;
      return next;
    }
  }

  return [...prev, line];
}

function appendFeed(prev: VoiceFeedEntry[], entry: VoiceFeedEntry) {
  const next = [...prev, entry].slice(-FEED_LIMIT);
  const seen = new Set<string>();
  const deduped: VoiceFeedEntry[] = [];
  for (const item of next) {
    const key = `${item.role}:${item.id}`;
    if (seen.has(key)) continue;
    seen.add(key);
    deduped.push(item);
  }
  return deduped;
}

/**
 * Owns a voice session end to end: transport lifecycle, audio pipeline,
 * transcript/notice/metrics state and the demo/live switch.
 *
 * Both the full console and the minimal stage read from this hook, so the two
 * surfaces cannot drift apart. Settings (gateway URL, caller name) are part of
 * the return value but are optional for callers that only need defaults.
 */
export function useVoiceSession() {
  const [mode, setMode] = useState<VoiceMode>("demo");
  const [status, setStatus] = useState<VoiceState>("idle");
  const [active, setActive] = useState(false);
  const [lines, setLines] = useState<TranscriptLine[]>([]);
  const [notices, setNotices] = useState<VoiceNoticeLine[]>([]);
  const [feed, setFeed] = useState<VoiceFeedEntry[]>([]);
  const [metrics, setMetrics] = useState<VoiceMetrics | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [muted, setMuted] = useState(false);
  const [hasMic, setHasMic] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [frame, setFrame] = useState(0);
  const [micLevel, setMicLevel] = useState(0);
  const [outputLevel, setOutputLevel] = useState(0);

  const [gatewayUrl, setGatewayUrl] = useState(DEFAULT_GATEWAY);
  const [caller, setCallerState] = useState<VoiceCaller>(EMPTY_CALLER);

  const setCaller = useCallback((patch: Partial<VoiceCaller>) => {
    setCallerState((prev) => ({ ...prev, ...patch }));
  }, []);

  // The full console (settings dialog) still edits the first name on its own.
  const callerFirstName = caller.firstName;
  const setCallerFirstName = useCallback(
    (value: string) => setCaller({ firstName: value }),
    [setCaller],
  );

  const transportRef = useRef<VoiceTransport | null>(null);
  const pipelineRef = useRef<AudioPipelineHandle | null>(null);
  const startRef = useRef(0);
  const tokenRef = useRef(0);

  const closePipeline = useCallback(() => {
    const pipeline = pipelineRef.current;
    pipelineRef.current = null;
    setHasMic(false);
    if (pipeline) void pipeline.close();
  }, []);

  const handleEvent = useCallback(
    (event: VoiceEvent) => {
      switch (event.type) {
        case "state":
          setStatus(event.state);
          return;
        case "transcript":
          setLines((prev) => upsertLine(prev, event.line));
          if (event.line.kind === "final") {
            setFeed((prev) =>
              appendFeed(prev, {
                id: event.line.id,
                role: "caller",
                text: event.line.text,
                sample: event.line.sample,
              }),
            );
          } else if (event.line.kind === "agent") {
            setFeed((prev) =>
              appendFeed(prev, {
                id: event.line.id,
                role: "agent",
                text: event.line.text,
                sample: event.line.sample,
              }),
            );
          }
          return;
        case "notice":
          setNotices((prev) => [...prev.slice(-3), event.line]);
          setFeed((prev) =>
            appendFeed(prev, {
              id: event.line.id,
              role: "notice",
              text: event.line.text,
              sample: event.line.sample,
            }),
          );
          return;
        case "metrics":
          setMetrics(event.metrics);
          return;
        case "output-level":
          setOutputLevel(event.level);
          return;
        case "error":
          setError(event.message);
          setStatus("error");
          setFeed((prev) =>
            appendFeed(prev, {
              id: nextErrorId(),
              role: "error",
              text: event.message,
              sample: false,
            }),
          );
          return;
        case "ended":
          setActive(false);
          setOutputLevel(0);
          setMicLevel(0);
          setStatus(event.reason === "error" ? "error" : "idle");
          transportRef.current = null;
          closePipeline();
          setMuted(false);
          return;
        default:
          return;
      }
    },
    [closePipeline],
  );

  const sessionContext = useCallback(
    () => ({
      onEvent: handleEvent,
      micLevel: () => pipelineRef.current?.level() ?? 0,
    }),
    [handleEvent],
  );

  useEffect(() => {
    try {
      const saved = window.localStorage.getItem("emma.voice.config");
      if (!saved) return;
      const parsed = JSON.parse(saved) as {
        gatewayUrl?: string;
        caller?: Partial<VoiceCaller>;
        /** legacy shape: the first name was stored on its own */
        callerFirstName?: string;
      };
      if (parsed.gatewayUrl) setGatewayUrl(parsed.gatewayUrl);
      if (parsed.caller) {
        setCallerState((prev) => ({ ...prev, ...parsed.caller }));
      } else if (parsed.callerFirstName) {
        setCallerState((prev) => ({ ...prev, firstName: parsed.callerFirstName! }));
      }
    } catch {
      // A corrupt preference should never prevent the console from loading.
    }
  }, []);

  useEffect(() => {
    if (!active) return;

    const ticker = window.setInterval(() => {
      setElapsed(Math.floor((Date.now() - startRef.current) / 1000));
    }, 500);

    const meter = window.setInterval(() => {
      setMicLevel(
        pipelineRef.current?.level() ?? transportRef.current?.level?.() ?? 0,
      );
      setFrame((value) => value + 1);
    }, 90);

    return () => {
      window.clearInterval(ticker);
      window.clearInterval(meter);
    };
  }, [active]);

  useEffect(() => {
    return () => {
      tokenRef.current += 1;
      transportRef.current?.stop();
      void pipelineRef.current?.close();
    };
  }, []);

  const begin = useCallback(
    (nextMode: VoiceMode) => {
      if (active) return;

      const token = tokenRef.current + 1;
      tokenRef.current = token;

      setMode(nextMode);
      setError(null);
      setLines([]);
      setNotices([]);
      setFeed([]);
      setMetrics(null);
      setElapsed(0);
      setOutputLevel(0);
      setMicLevel(0);
      setMuted(false);
      startRef.current = Date.now();
      setStatus("connecting");
      setActive(true);

      window.localStorage.setItem(
        "emma.voice.config",
        JSON.stringify({ gatewayUrl, caller }),
      );

      if (nextMode === "demo") {
        const transport = createDemoTransport();
        transportRef.current = transport;
        transport.start(sessionContext());
        return;
      }

      void (async () => {
        try {
          // LiveKit owns capture itself (WebRTC publishes the mic track), so
          // the 16 kHz PCM pipeline is only needed for the raw WS transport.
          if (LIVE_TRANSPORT === "livekit") {
            const transport = createLiveKitTransport({
              gatewayHttpUrl: GATEWAY_HTTP,
              callerFirstName,
              caller,
            });
            transportRef.current = transport;
            setHasMic(true);
            transport.onMuteChange?.((value) => setMuted(value));
            transport.start(sessionContext());
            return;
          }

          const pipeline = await openAudioPipeline();
          if (tokenRef.current !== token) {
            void pipeline.close();
            return;
          }

          pipelineRef.current = pipeline;
          setHasMic(true);
          pipeline.onMuteChange(setMuted);

          const transport = createGatewayTransport({
            url: gatewayUrl,
            pipeline,
            callerFirstName,
            caller,
          });
          transportRef.current = transport;
          transport.start(sessionContext());
        } catch (cause) {
          if (tokenRef.current !== token) return;
          const message =
            cause instanceof Error
              ? cause.message
              : "Microphone access was refused by the browser.";
          setError(message);
          setStatus("error");
          setActive(false);
          setFeed((prev) =>
            appendFeed(prev, {
              id: nextErrorId(),
              role: "error",
              text: message,
              sample: false,
            }),
          );
        }
      })();
    },
    [active, sessionContext, gatewayUrl, callerFirstName],
  );

  const end = useCallback(() => {
    tokenRef.current += 1;
    const transport = transportRef.current;
    transportRef.current = null;
    transport?.stop();
    closePipeline();
    setActive(false);
    setStatus("idle");
    setMuted(false);
    setOutputLevel(0);
    setMicLevel(0);
  }, [closePipeline]);

  const toggleMute = useCallback(() => {
    const pipeline = pipelineRef.current;
    if (pipeline) {
      const next = !pipeline.isMuted();
      pipeline.setMuted(next);
      setMuted(next);
      return;
    }

    const transport = transportRef.current;
    if (!transport?.setMuted) return;
    const next = !(transport.isMuted?.() ?? muted);
    transport.setMuted(next);
    setMuted(next);
  }, [muted]);

  return {
    mode,
    setMode,
    status,
    active,
    lines,
    notices,
    feed,
    metrics,
    error,
    muted,
    hasMic,
    elapsed,
    frame,
    micLevel,
    outputLevel,
    canMute: active && hasMic,
    begin,
    end,
    toggleMute,
    gatewayUrl,
    setGatewayUrl,
    caller,
    setCaller,
    callerFirstName,
    setCallerFirstName,
  };
}

export type VoiceSession = ReturnType<typeof useVoiceSession>;

"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  AlertTriangle,
  AudioLines,
  Ear,
  Loader2,
  Mic,
  MicOff,
  Play,
  Radio,
  Settings2,
  ShieldCheck,
  Sparkles,
  Square,
  Volume2,
} from "lucide-react";

import { Shdr21 } from "@/components/ui/shdr-21";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  useVoiceSession,
  type VoiceMode,
} from "@/components/voice/use-voice-session";
import type { TranscriptLine, VoiceState } from "@/lib/voice/types";
import { cn } from "@/lib/utils";

type Mode = VoiceMode;

const STATE_COPY: Record<
  VoiceState,
  { label: string; detail: string; icon: ReactNode; tone: string }
> = {
  idle: {
    label: "Ready when you are",
    detail: "Start the demo to preview the listening, thinking and speaking loop.",
    icon: <Sparkles className="size-3.5" />,
    tone: "text-white/56",
  },
  connecting: {
    label: "Opening the line",
    detail: "Connecting the browser to the voice runtime…",
    icon: <Loader2 className="size-3.5 animate-spin" />,
    tone: "text-sky-200",
  },
  listening: {
    label: "Listening",
    detail: "The microphone is live. EMMA is transcribing the call and will reply when the caller finishes.",
    icon: <Ear className="size-3.5" />,
    tone: "text-emerald-200",
  },
  thinking: {
    label: "Thinking",
    detail: "Checking practice context and preparing a reply.",
    icon: <Loader2 className="size-3.5 animate-spin" />,
    tone: "text-indigo-200",
  },
  speaking: {
    label: "Speaking",
    detail: "EMMA is answering. Caller speech can interrupt immediately.",
    icon: <Volume2 className="size-3.5" />,
    tone: "text-sky-200",
  },
  error: {
    label: "Line interrupted",
    detail: "The session could not continue. Check the gateway settings and try again.",
    icon: <AlertTriangle className="size-3.5" />,
    tone: "text-rose-200",
  },
};

const ORB_STATE = {
  idle: "idle",
  connecting: "thinking",
  listening: "idle",
  thinking: "thinking",
  speaking: "speaking",
  error: "idle",
} as const;

const STATE_BADGE: Record<VoiceState, string> = {
  idle: "STANDBY",
  connecting: "CONNECTING",
  listening: "LISTENING",
  thinking: "THINKING",
  speaking: "SPEAKING",
  error: "ERROR",
};

function formatTime(totalSeconds: number) {
  const minutes = Math.floor(totalSeconds / 60)
    .toString()
    .padStart(2, "0");
  const seconds = (totalSeconds % 60).toString().padStart(2, "0");
  return `${minutes}:${seconds}`;
}

export function VoiceConsole() {
  const [settingsOpen, setSettingsOpen] = useState(false);
  const scrollHostRef = useRef<HTMLDivElement | null>(null);

  const {
    mode,
    setMode,
    status,
    active,
    lines,
    notices,
    metrics,
    error,
    muted,
    canMute,
    elapsed,
    frame,
    micLevel,
    outputLevel,
    begin,
    end,
    toggleMute,
    gatewayUrl,
    setGatewayUrl,
    callerFirstName,
    setCallerFirstName,
  } = useVoiceSession();

  useEffect(() => {
    if (lines.length === 0 && notices.length === 0) return;
    const viewport = scrollHostRef.current?.querySelector<HTMLElement>(
      '[data-slot="scroll-area-viewport"]',
    );
    if (viewport) {
      viewport.scrollTo({ top: viewport.scrollHeight, behavior: "smooth" });
    }
  }, [lines, notices]);

  const level =
    status === "speaking"
      ? outputLevel
      : active
        ? Math.max(micLevel, status === "listening" ? 0.16 : 0.08)
        : 0;
  const stateCopy = STATE_COPY[status];
  const orbVolumes = !active
    ? undefined
    : status === "speaking"
      ? { input: micLevel, output: outputLevel }
      : { input: micLevel };

  return (
    <>
      <div
        id="voice"
        className="surface noise relative scroll-mt-24 overflow-hidden rounded-[28px]"
      >
        <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-sky-200/35 to-transparent" />

        {/* Console chrome */}
        <div className="relative z-10 flex flex-wrap items-center justify-between gap-3 border-b border-white/[0.07] px-5 py-4 sm:px-6">
          <div className="flex items-center gap-3">
            <div className="relative flex size-10 items-center justify-center rounded-full border border-white/10 bg-white/[0.03]">
              <AudioLines className="size-[18px] text-sky-200" />
              <span
                className={cn(
                  "absolute -right-0.5 -top-0.5 size-2 rounded-full border border-[#08090c]",
                  active ? "bg-emerald-300" : "bg-white/25",
                )}
              />
            </div>
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-semibold tracking-[-0.02em] text-white">
                  EMMA voice console
                </span>
                <Badge
                  variant="outline"
                  className="h-5 rounded-full border-white/10 bg-white/[0.03] px-2 font-mono text-[9px] tracking-[0.16em] text-white/45"
                >
                  {mode === "demo" ? "DEMO" : "LIVE"}
                </Badge>
              </div>
              <p className="mt-0.5 text-[11px] text-white/38">
                Receptionist runtime · 16 kHz PCM16 · barge-in aware
              </p>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <span className="rounded-md border border-white/[0.07] bg-black/25 px-2 py-1 font-mono text-[11px] tabular-nums text-white/45">
              {formatTime(elapsed)}
            </span>

            <Tabs
              value={mode}
              onValueChange={(value) => setMode(value as Mode)}
              className="gap-0"
            >
              <TabsList className="h-8 rounded-full border border-white/[0.08] bg-black/25 p-0.5">
                <TabsTrigger
                  value="demo"
                  disabled={active}
                  className="h-7 rounded-full px-3 text-[11px] text-white/50 data-[state=active]:bg-white data-[state=active]:text-black"
                >
                  Demo
                </TabsTrigger>
                <TabsTrigger
                  value="live"
                  disabled={active}
                  className="h-7 rounded-full px-3 text-[11px] text-white/50 data-[state=active]:bg-white data-[state=active]:text-black"
                >
                  Live
                </TabsTrigger>
              </TabsList>
            </Tabs>

            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="outline"
                  size="icon"
                  onClick={() => setSettingsOpen(true)}
                  className="size-8 rounded-full border-white/10 bg-white/[0.03] text-white/60 hover:bg-white/[0.07] hover:text-white"
                  aria-label="Session settings"
                >
                  <Settings2 className="size-3.5" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>Session settings</TooltipContent>
            </Tooltip>
          </div>
        </div>

        <div className="relative z-10 grid lg:grid-cols-[1.04fr_0.96fr]">
          {/* Orb stage */}
          <div className="relative flex min-h-[510px] flex-col items-center justify-center overflow-hidden border-b border-white/[0.07] px-5 py-10 lg:border-b-0 lg:border-r">
            <div className="pointer-events-none absolute inset-0 grid-lines opacity-[0.22] [mask-image:radial-gradient(circle_at_center,black,transparent_72%)]" />
            <div className="pointer-events-none absolute left-1/2 top-1/2 size-[30rem] -translate-x-1/2 -translate-y-1/2 rounded-full bg-[radial-gradient(circle,rgba(125,211,252,0.12),transparent_62%)] blur-2xl" />

            <div className="absolute left-5 top-5 flex items-center gap-2 font-mono text-[9px] tracking-[0.18em] text-white/28">
              <span className="size-1 rounded-full bg-sky-200/60" />
              SESSION 01
            </div>
            <div className="absolute right-5 top-5 font-mono text-[9px] tracking-[0.18em] text-white/28">
              {mode === "demo" ? "SAMPLE DIALOGUE" : "LIVE GATEWAY"}
            </div>

            <div className="relative mt-2 flex items-center justify-center">
              <div className="absolute size-[292px] rounded-full border border-dashed border-white/[0.07] sm:size-[352px]" />
              <div className="absolute size-[332px] rounded-full border border-white/[0.035] sm:size-[398px]" />
              <div className="absolute size-[250px] rounded-full bg-sky-300/[0.06] blur-3xl sm:size-[300px]" />
              <Shdr21
                state={ORB_STATE[status]}
                className="size-[252px] sm:size-[318px]"
                wrapper="ring"
                wrapperColor="rgba(165, 243, 252, 0.48)"
                volumes={orbVolumes}
                params={{
                  exposure: 1.02,
                  alphaGain: 1.5,
                  scale: 1.7,
                  churn: 0.75,
                  density: 2.6,
                  threshold: 0.085,
                }}
                statePresets={{
                  idle: { power: 1.9, shadowAbsorb: 3.2 },
                  thinking: { power: 2.1, shadowAbsorb: 3.6, ambient: 0.18 },
                  speaking: {
                    power: 2.45,
                    shadowAbsorb: 4.4,
                    ambient: 0.3,
                    shadowLift: 0.8,
                  },
                }}
                stateColors={{
                  idle: { light: "#dff6ff", shadow: "#31417e" },
                  thinking: { light: "#e8dcff", shadow: "#49338f" },
                  speaking: { light: "#c8fbff", shadow: "#155e75" },
                }}
                ariaLabel={`EMMA voice assistant orb, ${stateCopy.label.toLowerCase()}`}
              />
            </div>

            <div className="relative mt-8 flex flex-col items-center text-center">
              <div
                className="flex items-center gap-2 font-mono text-[10px] tracking-[0.16em] text-white/35"
                aria-live="polite"
              >
                <span className={cn("flex items-center gap-1.5", stateCopy.tone)}>
                  {stateCopy.icon}
                  {STATE_BADGE[status]}
                </span>
                <span className="text-white/15">/</span>
                <span>{active ? "ON LINE" : "OFF LINE"}</span>
              </div>
              <h2 className="mt-3 text-[26px] font-semibold leading-none text-white sm:text-[30px]">
                {stateCopy.label}
              </h2>
              <p className="mt-2.5 max-w-sm text-[13px] leading-relaxed text-white/44">
                {stateCopy.detail}
              </p>
            </div>

            <Waveform active={active} level={level} frame={frame} speaking={status === "speaking"} />

            <div className="relative mt-6 flex flex-wrap items-center justify-center gap-2.5">
              <Button
                onClick={() => (active ? end() : begin(mode))}
                className={cn(
                  "h-11 rounded-full px-5 text-[13px] font-semibold",
                  active
                    ? "bg-white/10 text-white hover:bg-white/15"
                    : "bg-white text-black hover:bg-white/90",
                )}
              >
                {active ? (
                  <>
                    <Square className="size-3.5 fill-current" />
                    End session
                  </>
                ) : (
                  <>
                    <Play className="size-3.5 fill-current" />
                    {mode === "demo" ? "Start voice demo" : "Start live session"}
                  </>
                )}
              </Button>

              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="outline"
                    size="icon"
                    onClick={toggleMute}
                    disabled={!canMute}
                    className={cn(
                      "size-11 rounded-full border-white/10 bg-white/[0.03] text-white/60 hover:bg-white/[0.07] hover:text-white disabled:opacity-35",
                      muted && "border-rose-300/25 bg-rose-400/10 text-rose-200",
                    )}
                    aria-label={muted ? "Unmute microphone" : "Mute microphone"}
                  >
                    {muted ? (
                      <MicOff className="size-4" />
                    ) : (
                      <Mic className="size-4" />
                    )}
                  </Button>
                </TooltipTrigger>
                <TooltipContent>
                  {canMute
                    ? muted
                      ? "Unmute microphone"
                      : "Mute microphone"
                    : "Microphone is only used in a live session"}
                </TooltipContent>
              </Tooltip>
            </div>

            <p className="relative mt-4 max-w-xs text-center text-[10.5px] leading-relaxed text-white/28">
              {mode === "demo"
                ? "Clearly labelled sample dialogue. No call is placed and no microphone is opened."
                : "Microphone permission is requested only after you start. Audio streams to your configured gateway."}
            </p>
          </div>

          {/* Transcript */}
          <div className="relative flex min-h-[510px] flex-col bg-[#07080b]/55">
            <div className="flex items-center justify-between border-b border-white/[0.06] px-5 py-4 sm:px-6">
              <div>
                <h3 className="text-sm font-semibold text-white">Live transcript</h3>
                <p className="mt-0.5 text-[11px] text-white/36">
                  Caller turns and EMMA replies appear as they arrive.
                </p>
              </div>
              <Badge
                variant="outline"
                className="h-6 gap-1.5 rounded-full border-white/10 bg-white/[0.03] px-2.5 font-mono text-[9px] tracking-[0.12em] text-white/40"
              >
                <span
                  className={cn(
                    "size-1.5 rounded-full",
                    active ? "animate-pulse bg-emerald-300" : "bg-white/25",
                  )}
                />
                {lines.length} {lines.length === 1 ? "LINE" : "LINES"}
              </Badge>
            </div>

            <div ref={scrollHostRef} className="min-h-0 flex-1">
              <ScrollArea className="h-full">
                <div className="flex min-h-[344px] flex-col gap-4 px-5 py-5 sm:px-6">
                  {lines.length === 0 && notices.length === 0 ? (
                    <EmptyTranscript />
                  ) : null}

                  {notices.map((notice) => (
                    <div
                      key={notice.id}
                      className="flex items-start gap-2 rounded-xl border border-sky-200/[0.08] bg-sky-300/[0.035] px-3 py-2.5"
                    >
                      <Radio className="mt-0.5 size-3 shrink-0 text-sky-200/55" />
                      <p className="font-mono text-[10px] leading-relaxed text-sky-100/55">
                        {notice.text}
                        {notice.sample ? (
                          <span className="ml-1.5 rounded-sm border border-white/10 px-1 py-px text-[8px] tracking-[0.12em] text-white/30">
                            SAMPLE
                          </span>
                        ) : null}
                      </p>
                    </div>
                  ))}

                  {lines.map((line) => (
                    <TranscriptRow key={line.id} line={line} />
                  ))}
                </div>
              </ScrollArea>
            </div>

            <div className="border-t border-white/[0.06] px-5 py-3.5 sm:px-6">
              {error ? (
                <div className="mb-3 flex items-start gap-2 rounded-xl border border-rose-300/15 bg-rose-400/[0.06] px-3 py-2.5">
                  <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-rose-200" />
                  <p className="text-[11px] leading-relaxed text-rose-100/75">
                    {error}
                  </p>
                </div>
              ) : null}

              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[9.5px] tracking-[0.08em] text-white/30">
                  <span>
                    IN <span className="text-white/55">{metrics?.framesIn ?? 0}</span>
                  </span>
                  <span>
                    OUT <span className="text-white/55">{metrics?.framesOut ?? 0}</span>
                  </span>
                  <span>
                    CLOSE{" "}
                    <span className="text-white/55">
                      {metrics?.closeCode ?? "—"}
                    </span>
                  </span>
                </div>
                <button
                  type="button"
                  onClick={() => setSettingsOpen(true)}
                  className="rounded-full text-[10.5px] text-white/35 underline-offset-4 transition hover:text-white/65 hover:underline"
                >
                  {mode === "demo" ? "Configure live gateway" : "Gateway settings"}
                </button>
              </div>
            </div>
          </div>
        </div>

        <div className="relative z-10 flex flex-wrap items-center justify-between gap-2 border-t border-white/[0.06] bg-black/25 px-5 py-3 sm:px-6">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[10px] text-white/32">
            <span className="flex items-center gap-1.5">
              <ShieldCheck className="size-3 text-emerald-300/70" />
              Local providers by default
            </span>
            <span className="flex items-center gap-1.5">
              <Sparkles className="size-3 text-sky-200/60" />
              Sample data mode
            </span>
            <span className="flex items-center gap-1.5">
              <AudioLines className="size-3 text-indigo-200/60" />
              Interruption-safe
            </span>
          </div>
          <span className="font-mono text-[9px] tracking-[0.16em] text-white/22">
            QUANTUMLOOPAI / EMMA RUNTIME
          </span>
        </div>
      </div>

      <SessionSettings
        open={settingsOpen}
        onOpenChange={setSettingsOpen}
        gatewayUrl={gatewayUrl}
        onGatewayUrl={setGatewayUrl}
        callerFirstName={callerFirstName}
        onCallerFirstName={setCallerFirstName}
        mode={mode}
        active={active}
        onStartLive={() => {
          setSettingsOpen(false);
          begin("live");
        }}
      />
    </>
  );
}

function Waveform({
  active,
  level,
  frame,
  speaking,
}: {
  active: boolean;
  level: number;
  frame: number;
  speaking: boolean;
}) {
  const bars = Array.from({ length: 40 }, (_, index) => {
    const shape =
      0.28 +
      0.72 *
        Math.abs(
          Math.sin(index * 1.37 + (active ? frame * 0.13 : 0)) *
            Math.cos(index * 0.61 + frame * 0.04),
        );
    const energy = active ? 0.22 + level * 0.95 : 0.14;
    const height = Math.max(4, Math.round(shape * energy * 38));
    return { height, opacity: active ? 0.38 + level * 0.62 : 0.2 };
  });

  return (
    <div
      className="relative mt-7 flex h-10 items-center justify-center gap-[3px]"
      aria-hidden="true"
    >
      {bars.map((bar, index) => (
        <span
          key={index}
          className={cn(
            "w-[2.5px] rounded-full bg-gradient-to-t transition-[height,opacity] duration-100 ease-out",
            speaking
              ? "from-cyan-400/25 via-cyan-200/70 to-white"
              : "from-indigo-500/20 via-sky-300/60 to-sky-100",
          )}
          style={{ height: `${bar.height}px`, opacity: bar.opacity }}
        />
      ))}
    </div>
  );
}

function TranscriptRow({ line }: { line: TranscriptLine }) {
  const isAgent = line.kind === "agent";
  const isPartial = line.kind === "partial";

  return (
    <div
      className={cn(
        "flex gap-3",
        isAgent && "animate-in fade-in slide-in-from-bottom-1 duration-300",
      )}
    >
      <div
        className={cn(
          "mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full border text-[10px] font-semibold",
          isAgent
            ? "border-sky-200/20 bg-gradient-to-br from-sky-300/20 to-indigo-400/20 text-sky-100"
            : "border-white/10 bg-white/[0.04] text-white/55",
        )}
      >
        {isAgent ? "E" : "C"}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span
            className={cn(
              "text-[10px] font-semibold tracking-[0.1em]",
              isAgent ? "text-sky-100/80" : "text-white/45",
            )}
          >
            {isAgent ? "EMMA" : "CALLER"}
          </span>
          {isPartial ? (
            <span className="flex items-center gap-1 text-[9px] text-emerald-200/60">
              <span className="size-1 animate-pulse rounded-full bg-emerald-300" />
              HEARING
            </span>
          ) : null}
          {line.sample ? (
            <span className="rounded-sm border border-white/10 px-1 py-px font-mono text-[8px] tracking-[0.12em] text-white/28">
              SAMPLE
            </span>
          ) : null}
        </div>
        <p
          className={cn(
            "mt-1 text-[13.5px] leading-relaxed",
            isPartial
              ? "text-white/46"
              : isAgent
                ? "text-white/88"
                : "text-white/68",
          )}
        >
          {line.text}
          {isPartial ? (
            <span className="ml-0.5 inline-block h-3.5 w-px animate-pulse bg-sky-200/70 align-middle" />
          ) : null}
        </p>
      </div>
    </div>
  );
}

function EmptyTranscript() {
  return (
    <div className="flex flex-1 flex-col items-center justify-center py-12 text-center">
      <div className="relative flex size-12 items-center justify-center rounded-full border border-white/10 bg-white/[0.03]">
        <AudioLines className="size-5 text-white/35" />
        <span className="absolute inset-0 animate-pulse-soft rounded-full border border-sky-200/10" />
      </div>
      <p className="mt-4 text-[13px] font-medium text-white/55">
        No transcript yet
      </p>
      <p className="mt-1.5 max-w-[240px] text-[11.5px] leading-relaxed text-white/30">
        Start the demo to see caller turns, EMMA replies and system notices
        arrive here.
      </p>
    </div>
  );
}

function SessionSettings({
  open,
  onOpenChange,
  gatewayUrl,
  onGatewayUrl,
  callerFirstName,
  onCallerFirstName,
  mode,
  active,
  onStartLive,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  gatewayUrl: string;
  onGatewayUrl: (value: string) => void;
  callerFirstName: string;
  onCallerFirstName: (value: string) => void;
  mode: Mode;
  active: boolean;
  onStartLive: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="surface border-white/10 bg-[#08090c] text-white sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-lg">
            <Settings2 className="size-4 text-sky-200" />
            Session settings
          </DialogTitle>
          <DialogDescription className="text-white/42">
            The demo needs no setup. A live session joins an EMMA LiveKit room,
            where the agent runs transcription, the clinic tools and speech
            synthesis.
          </DialogDescription>
        </DialogHeader>

        <div className="mt-1 space-y-5">
          <div className="space-y-2">
            <Label
              htmlFor="gateway-url"
              className="text-[11px] font-medium tracking-[0.04em] text-white/60"
            >
              Fallback gateway WebSocket URL
            </Label>
            <Input
              id="gateway-url"
              value={gatewayUrl}
              onChange={(event) => onGatewayUrl(event.target.value)}
              spellCheck={false}
              placeholder="ws://localhost:8000/ws/exotel"
              className="h-10 border-white/10 bg-white/[0.03] font-mono text-[11px] text-white/80 placeholder:text-white/22"
            />
            <p className="text-[10.5px] leading-relaxed text-white/32">
              Only used when <code>NEXT_PUBLIC_VOICE_TRANSPORT=websocket</code>.
              The default live path is a LiveKit room, and the token endpoint is{" "}
              <code>/api/voice/token</code>.
            </p>
          </div>

          <div className="space-y-2">
            <Label
              htmlFor="caller-name"
              className="text-[11px] font-medium tracking-[0.04em] text-white/60"
            >
              Caller first name{" "}
              <span className="font-normal text-white/28">(optional)</span>
            </Label>
            <Input
              id="caller-name"
              value={callerFirstName}
              onChange={(event) => onCallerFirstName(event.target.value)}
              placeholder="Sarah"
              className="h-10 border-white/10 bg-white/[0.03] text-[12px] text-white/80 placeholder:text-white/22"
            />
            <p className="text-[10.5px] leading-relaxed text-white/32">
              Only the first name is sent, as a query parameter, so EMMA can
              greet the caller.
            </p>
          </div>

          <div className="rounded-xl border border-emerald-300/10 bg-emerald-300/[0.035] px-3.5 py-3">
            <div className="flex items-center gap-2 text-[11px] font-medium text-emerald-100/80">
              <ShieldCheck className="size-3.5" />
              Demo stays local
            </div>
            <p className="mt-1 text-[10.5px] leading-relaxed text-emerald-50/40">
              The demo uses labelled sample dialogue. It never opens the
              microphone, places a call, or contacts a model.
            </p>
          </div>

          <Separator className="bg-white/[0.07]" />

          <div className="flex flex-col gap-2 sm:flex-row sm:justify-end">
            <Button
              variant="outline"
              onClick={() => onOpenChange(false)}
              className="h-9 rounded-full border-white/10 bg-transparent text-white/65 hover:bg-white/[0.05] hover:text-white"
            >
              Close
            </Button>
            <Button
              onClick={onStartLive}
              disabled={active || !gatewayUrl.trim()}
              className="h-9 rounded-full bg-white text-black hover:bg-white/90 disabled:opacity-40"
            >
              <Mic className="size-3.5" />
              {mode === "live" ? "Start live session" : "Use live gateway"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

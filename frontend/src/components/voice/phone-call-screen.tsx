"use client";

import { Mic, MicOff, PhoneOff, TriangleAlert } from "lucide-react";

import type { VoiceCaller, VoiceState } from "@/lib/voice/types";
import { cn } from "@/lib/utils";

const STATE_LABEL: Record<VoiceState, string> = {
  idle: "READY",
  connecting: "CONNECTING",
  listening: "LISTENING",
  thinking: "THINKING",
  speaking: "SPEAKING",
  error: "LINE INTERRUPTED",
};

function clock(seconds: number): string {
  const safe = Math.max(0, Math.floor(seconds));
  const mm = String(Math.floor(safe / 60)).padStart(2, "0");
  const ss = String(safe % 60).padStart(2, "0");
  return `${mm}:${ss}`;
}

/** Three bars that follow the live level of whichever side is talking. */
function LevelBars({ level, tone }: { level: number; tone: string }) {
  const bars = [0.45, 1, 0.7];
  return (
    <div className="flex h-4 items-end gap-[2px]" aria-hidden="true">
      {bars.map((weight, index) => (
        <span
          key={index}
          className={cn("w-[2px] rounded-full transition-[height] duration-100", tone)}
          style={{
            height: `${Math.max(12, Math.min(100, level * 100 * weight + 12))}%`,
          }}
        />
      ))}
    </div>
  );
}

function StateGlyph({
  status,
  micLevel,
  outputLevel,
}: {
  status: VoiceState;
  micLevel: number;
  outputLevel: number;
}) {
  const speaking = status === "speaking";
  const listening = status === "listening" || status === "idle";

  return (
    <span className="relative flex size-9 items-center justify-center">
      {(listening || speaking) && (
        <span
          aria-hidden="true"
          className={cn(
            "absolute inset-0 animate-ping rounded-full border [animation-duration:2.4s]",
            listening ? "border-[#ffd39a]/35" : "border-[#efe0ff]/40",
          )}
        />
      )}
      <span
        aria-hidden="true"
        className={cn(
          "flex size-9 items-center justify-center rounded-full transition-colors duration-300",
          status === "error" && "bg-rose-500/15",
          status === "speaking" &&
            "bg-[radial-gradient(circle_at_32%_26%,#efe0ff,#8f2c60)]",
          (listening || status === "connecting" || status === "thinking") &&
            "bg-[radial-gradient(circle_at_32%_26%,#ffe3c0,#5d2547)]",
        )}
      >
        {status === "error" ? (
          <TriangleAlert className="size-3.5 text-rose-300" aria-hidden="true" />
        ) : status === "connecting" ? (
          <span
            aria-hidden="true"
            className="size-3.5 animate-spin rounded-full border-[1.5px] border-white/30 border-t-white/90"
          />
        ) : status === "thinking" ? (
          <span className="flex gap-[3px]" aria-hidden="true">
            {[0, 1, 2].map((dot) => (
              <span
                key={dot}
                className="size-[3px] animate-pulse rounded-full bg-white/85"
                style={{ animationDelay: `${dot * 160}ms` }}
              />
            ))}
          </span>
        ) : (
          <LevelBars level={speaking ? outputLevel : micLevel} tone="bg-white/90" />
        )}
      </span>
    </span>
  );
}

/**
 * In-call screen inside the docked phone.
 *
 * Mirrors the same transport state machine as the orb: connecting, listening,
 * thinking, speaking and the interrupted line, plus the two controls a caller
 * expects — mute and hang up. The caller's mobile and reason are dropped when
 * the handset is rendered at phone-booth width (mobile breakpoints), where
 * they would push the controls out of the screen.
 */
export function PhoneCallScreen({
  status,
  caller,
  elapsed,
  muted,
  canMute,
  demo,
  micLevel,
  outputLevel,
  onToggleMute,
  onEnd,
}: {
  status: VoiceState;
  caller: VoiceCaller;
  elapsed: number;
  muted: boolean;
  canMute: boolean;
  demo: boolean;
  micLevel: number;
  outputLevel: number;
  onToggleMute: () => void;
  onEnd: () => void;
}) {
  const name = [caller.firstName, caller.lastName]
    .map((part) => part.trim())
    .filter(Boolean)
    .join(" ");

  return (
    <div className="@container flex h-full flex-col bg-[#0a090c] px-2.5 pb-4 pt-7 text-white">
      <div className="flex items-center justify-between font-mono text-[7px] uppercase tracking-[0.18em]">
        <span className={cn(demo ? "text-amber-200/60" : "text-emerald-200/55")}>
          {demo ? "Demo" : "Live"}
        </span>
        <span className="tabular-nums text-white/40">{clock(elapsed)}</span>
      </div>

      <div className="mt-3 flex flex-col items-center text-center @[150px]:mt-4">
        <StateGlyph status={status} micLevel={micLevel} outputLevel={outputLevel} />

        <p className="mt-2.5 whitespace-nowrap text-[10px] font-semibold leading-tight @[150px]:text-[11px]">
          EMMA Reception
        </p>
        <p
          className={cn(
            "mt-1 font-mono text-[6px] uppercase tracking-[0.18em] @[150px]:text-[6.5px]",
            status === "error" ? "text-rose-300/80" : "text-white/45",
          )}
          aria-live="polite"
        >
          {STATE_LABEL[status]}
        </p>
        {name ? (
          <p className="mt-1 max-w-full truncate text-[8px] text-white/32 @[150px]:text-[8.5px]">
            {name}
          </p>
        ) : null}
        {caller.email ? (
          <p className="max-w-full truncate text-[7.5px] text-white/25">
            {caller.email}
          </p>
        ) : null}
        {muted && status !== "error" ? (
          <p className="mt-1 font-mono text-[6px] uppercase tracking-[0.16em] text-rose-200/70">
            Muted
          </p>
        ) : null}
      </div>

      {caller.mobile.trim() || caller.reason.trim() ? (
        <dl className="mt-4 hidden space-y-2 border-t border-white/8 pt-3 @[150px]:block">
          {caller.mobile.trim() ? (
            <div>
              <dt className="font-mono text-[6px] uppercase tracking-[0.18em] text-white/28">
                Mobile
              </dt>
              <dd className="mt-0.5 truncate text-[8.5px] tabular-nums text-white/70">
                {caller.mobile}
              </dd>
            </div>
          ) : null}
          {caller.reason.trim() ? (
            <div>
              <dt className="font-mono text-[6px] uppercase tracking-[0.18em] text-white/28">
                Reason
              </dt>
              <dd className="mt-0.5 line-clamp-2 text-[8.5px] leading-[1.45] text-white/60">
                {caller.reason}
              </dd>
            </div>
          ) : null}
        </dl>
      ) : null}

      <div className="mt-auto flex items-center justify-center gap-2 pt-3">
        <button
          type="button"
          onClick={onToggleMute}
          disabled={!canMute}
          aria-label={muted ? "Unmute microphone" : "Mute microphone"}
          aria-pressed={muted}
          title={canMute ? undefined : "Microphone is only used in a live session"}
          className={cn(
            "flex size-8 items-center justify-center rounded-full border border-white/12 bg-white/[0.06] text-white/70 transition hover:bg-white/[0.12] hover:text-white disabled:opacity-35 @[150px]:size-9",
            muted && "border-rose-300/25 bg-rose-400/12 text-rose-200",
          )}
        >
          {muted ? <MicOff className="size-3.5" /> : <Mic className="size-3.5" />}
        </button>

        <button
          type="button"
          onClick={onEnd}
          aria-label="End call"
          className="flex size-8 items-center justify-center rounded-full bg-[#c0262d] text-white transition hover:bg-[#d12b33] @[150px]:size-9"
        >
          <PhoneOff className="size-3.5" />
        </button>
      </div>
    </div>
  );
}

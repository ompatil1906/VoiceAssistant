"use client";

import { QuantumLoopWordmark } from "@/components/brand/quantumloop-wordmark";
import { PhoneMockupCard } from "@/components/ui/phone-mockup";
import { Shdr21 } from "@/components/ui/shdr-21";
import { Button } from "@/components/ui/button";
import { PhoneCallScreen } from "@/components/voice/phone-call-screen";
import { PhoneFormScreen } from "@/components/voice/phone-form-screen";
import { useVoiceSession } from "@/components/voice/use-voice-session";
import type { VoiceState } from "@/lib/voice/types";
import { cn } from "@/lib/utils";

const STATE_LABEL: Record<VoiceState, string> = {
  idle: "READY",
  connecting: "CONNECTING",
  listening: "LISTENING",
  thinking: "THINKING",
  speaking: "SPEAKING",
  error: "LINE INTERRUPTED",
};

const ORB_STATE = {
  idle: "idle",
  connecting: "thinking",
  listening: "idle",
  thinking: "thinking",
  speaking: "speaking",
  error: "idle",
} as const;

const ORB_COLORS = {
  idle: { light: "#ffe3c0", shadow: "#5d2547" },
  thinking: { light: "#efe0ff", shadow: "#4b2f86" },
  speaking: { light: "#ffd39a", shadow: "#8f2c60" },
} as const;

function clock(seconds: number): string {
  const safe = Math.max(0, Math.floor(seconds));
  return `${String(Math.floor(safe / 60)).padStart(2, "0")}:${String(
    safe % 60,
  ).padStart(2, "0")}`;
}

/**
 * The primary screen.
 *
 * Two phases, one stage: before a call the phone mockup sits centred and holds
 * the intake form; once the caller taps "Call now" it docks to the bottom
 * right as the live call widget while the orb takes the centre and the
 * conversation happens there. The demo is one tap away in the header.
 */
export function VoiceStage() {
  const {
    mode,
    status,
    active,
    feed,
    muted,
    canMute,
    elapsed,
    caller,
    setCaller,
    micLevel,
    outputLevel,
    begin,
    end,
    toggleMute,
  } = useVoiceSession();

  // `status === "error"` keeps the docked phone (and its retry-by-hanging-up
  // path) on screen after the transport has already wound down.
  const started = active || status === "error";
  const label = STATE_LABEL[status];

  const volumes = !active
    ? undefined
    : status === "speaking"
      ? { input: micLevel, output: outputLevel }
      : { input: micLevel };

  return (
    <main className="relative flex h-dvh flex-col overflow-hidden bg-black text-white sm:h-auto sm:min-h-dvh">
      <header className="absolute inset-x-0 top-0 z-20 flex items-center justify-between px-5 py-5 sm:px-10 sm:py-6">
        <a
          href="/"
          aria-label="QuantumLoopAI home"
          className="rounded-sm text-white outline-none focus-visible:ring-1 focus-visible:ring-white/40"
        >
          <QuantumLoopWordmark className="h-[20px] w-auto sm:h-[22px]" />
        </a>

        {started ? (
          <span className="flex items-center gap-2 rounded-full border border-white/10 bg-white/[0.03] px-3 py-1.5 font-mono text-[9.5px] uppercase tracking-[0.18em] text-white/45">
            <span
              className={cn(
                "size-1.5 rounded-full",
                status === "error"
                  ? "bg-rose-400"
                  : status === "speaking"
                    ? "bg-[#efe0ff]"
                    : status === "thinking"
                      ? "bg-[#c9a7ff]"
                      : "bg-emerald-300/80",
                active && "animate-pulse",
              )}
            />
            {label.toLowerCase()} · {mode === "demo" ? "sample" : clock(elapsed)}
          </span>
        ) : (
          <Button
            variant="outline"
            onClick={() => begin("demo")}
            className="h-9 rounded-full border-white/10 bg-white/[0.03] px-4 text-[12.5px] font-medium text-white/70 backdrop-blur-sm hover:bg-white/[0.07] hover:text-white"
          >
            Preview demo
          </Button>
        )}
      </header>

      {!started ? (
        <section className="relative flex flex-1 items-center justify-center px-6 pb-10 pt-24 sm:pt-28">
          {/* Soft seat behind the handset so it does not float on flat black. */}
          <div
            aria-hidden="true"
            className="pointer-events-none absolute left-1/2 top-1/2 h-[460px] w-[460px] -translate-x-1/2 -translate-y-1/2 rounded-full bg-[radial-gradient(circle,rgba(143,44,96,0.20),rgba(93,37,71,0.10)_45%,transparent_70%)] blur-2xl"
          />

          <div className="relative animate-in fade-in zoom-in-95 duration-500">
            <PhoneMockupCard
              variant="titanium"
              className="w-[268px] shadow-[0_50px_120px_-40px_rgba(0,0,0,0.95)] sm:w-[300px]"
            >
              <PhoneFormScreen
                caller={caller}
                onChange={setCaller}
                onSubmit={() => begin("live")}
              />
            </PhoneMockupCard>
          </div>
        </section>
      ) : (
        <>
          <section className="relative flex min-h-0 flex-1 flex-col items-center justify-end px-6 pb-[268px] pt-24 sm:justify-center sm:pb-10 sm:pt-32">
            <div className="size-[168px] [@media(max-height:720px)]:size-[128px] sm:size-[300px]">
              <Shdr21
                state={ORB_STATE[status]}
                size={300}
                style={{ width: "100%", height: "100%" }}
                wrapper="none"
                volumes={volumes}
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
                  idle: ORB_COLORS.idle,
                  thinking: ORB_COLORS.thinking,
                  speaking: ORB_COLORS.speaking,
                }}
                ariaLabel={`EMMA voice assistant orb, ${label.toLowerCase()}`}
              />
            </div>

            <p className="mt-6 text-[10.5px] font-medium uppercase tracking-[0.5em] text-white/38 sm:mt-7">
              {label}
            </p>

            <div
              aria-live="polite"
              className="mt-7 flex min-h-0 w-full max-w-md flex-1 flex-col items-center justify-end gap-2.5 overflow-hidden text-center sm:mt-8 sm:max-h-[14rem] sm:flex-none"
            >
              {feed.length === 0 ? (
                <p className="text-[13px] leading-relaxed text-white/22">
                  EMMA is joining the line.
                </p>
              ) : (
                feed.map((entry) => (
                  <p
                    key={entry.id}
                    className={cn(
                      "text-[13px] leading-relaxed",
                      entry.role === "agent" && "text-white/82",
                      entry.role === "caller" && "text-white/66",
                      entry.role === "notice" && "text-[11.5px] text-white/30",
                      entry.role === "error" && "text-[11.5px] text-rose-200/75",
                    )}
                  >
                    {entry.sample ? (
                      <span className="mr-1.5 inline-flex translate-y-[-1px] rounded-[3px] border border-white/12 px-1 py-px align-middle font-mono text-[8px] tracking-[0.14em] text-white/30">
                        SAMPLE
                      </span>
                    ) : null}
                    {entry.text}
                  </p>
                ))
              )}
            </div>

            <p className="mt-6 hidden max-w-sm text-center text-[11px] leading-relaxed text-white/26 sm:block">
              {status === "error"
                ? "The line dropped. End the call and start again."
                : active && mode === "live"
                  ? "Microphone is live. Audio streams to your EMMA LiveKit room."
                  : "Clearly labelled sample dialogue. No call is placed and no microphone is opened."}
            </p>
          </section>

          <div className="fixed bottom-2 right-2 z-30 animate-in slide-in-from-bottom-8 fade-in duration-500 sm:bottom-6 sm:right-6">
            <PhoneMockupCard
              variant="titanium"
              className="w-[124px] shadow-[0_30px_70px_-24px_rgba(0,0,0,0.95)] sm:w-[184px]"
            >
              <PhoneCallScreen
                status={status}
                caller={caller}
                elapsed={elapsed}
                muted={muted}
                canMute={canMute}
                demo={mode === "demo"}
                micLevel={micLevel}
                outputLevel={outputLevel}
                onToggleMute={toggleMute}
                onEnd={end}
              />
            </PhoneMockupCard>
          </div>
        </>
      )}
    </main>
  );
}

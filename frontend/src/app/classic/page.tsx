import {
  ArrowRight,
  Check,
  ChevronRight,
  Radio,
  ShieldCheck,
  Sparkles,
} from "lucide-react";

import { SiteHeader } from "@/components/layout/site-header";
import { FinalCta, SiteFooter } from "@/components/layout/site-footer";
import { Capabilities } from "@/components/sections/capabilities";
import { RuntimeSection } from "@/components/sections/runtime";
import { SafetySection } from "@/components/sections/safety";
import { VoiceConsole } from "@/components/voice/voice-console";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

export const metadata = {
  title: "EMMA Voice Console — QuantumLoopAI",
};

/**
 * Long-form landing page kept from the first design pass. The primary screen
 * is the minimal stage at `/`; this route preserves the full marketing layout
 * for demos and screenshots.
 */
export default function ClassicPage() {
  return (
    <div id="top" className="relative min-h-screen overflow-x-clip">
      <div className="pointer-events-none absolute inset-x-0 top-0 h-[52rem] overflow-hidden">
        <div className="absolute left-1/2 top-[-24rem] size-[64rem] -translate-x-1/2 rounded-full bg-[radial-gradient(circle,rgba(125,211,252,0.13),rgba(129,140,248,0.07)_38%,transparent_66%)] blur-2xl" />
        <div className="absolute left-[72%] top-[8rem] size-[26rem] rounded-full bg-fuchsia-400/[0.05] blur-3xl" />
        <div className="absolute inset-x-0 top-0 h-full grid-lines opacity-[0.16] [mask-image:radial-gradient(ellipse_at_50%_18%,black,transparent_62%)]" />
      </div>

      <SiteHeader />

      <main className="relative">
        <section className="relative">
          <div className="mx-auto w-full max-w-7xl px-5 pb-12 pt-16 sm:px-8 sm:pb-16 sm:pt-24">
            <div className="mx-auto flex max-w-4xl flex-col items-center text-center">
              <Badge
                variant="outline"
                className="h-7 gap-2 rounded-full border-white/10 bg-white/[0.03] px-3 font-mono text-[9px] tracking-[0.18em] text-white/40 backdrop-blur"
              >
                <span className="relative flex size-1.5">
                  <span className="absolute inline-flex size-full animate-ping rounded-full bg-sky-300/60" />
                  <span className="relative inline-flex size-1.5 rounded-full bg-sky-300" />
                </span>
                EMMA · ADMINISTRATIVE VOICE RECEPTIONIST
              </Badge>

              <h1 className="mt-7 text-[clamp(2.9rem,7.6vw,5.6rem)] font-semibold leading-[0.92] text-white">
                A front desk that
                <br />
                <span className="text-gradient">hears you.</span>
                <span className="block text-[clamp(2.2rem,5.4vw,4rem)] leading-[1.02] text-white/56">
                  Then gets to work.
                </span>
              </h1>

              <p className="mt-6 max-w-2xl text-[14.5px] leading-relaxed text-white/44 sm:text-[15.5px]">
                Meet EMMA, the voice-first receptionist for GP practices. She
                answers routine calls, captures appointment and prescription
                requests, and hands off to a human the moment it matters —
                without ever pretending to be clinical.
              </p>

              <div className="mt-8 flex flex-col items-center gap-3 sm:flex-row">
                <Button
                  asChild
                  size="lg"
                  className="h-11 rounded-full bg-white px-6 text-[13px] font-semibold text-black shadow-[0_0_0_1px_rgba(255,255,255,0.12),0_16px_44px_-18px_rgba(255,255,255,0.45)] hover:bg-white/90"
                >
                  <a href="#voice">
                    Start a voice demo
                    <ArrowRight className="size-4" />
                  </a>
                </Button>
                <Button
                  asChild
                  variant="outline"
                  size="lg"
                  className="h-11 rounded-full border-white/10 bg-white/[0.03] px-6 text-[13px] text-white/68 hover:bg-white/[0.07] hover:text-white"
                >
                  <a href="#runtime">
                    Explore the runtime
                    <ChevronRight className="size-4" />
                  </a>
                </Button>
              </div>

              <div className="mt-8 flex flex-wrap items-center justify-center gap-x-6 gap-y-2.5 text-[11px] text-white/30">
                <span className="flex items-center gap-1.5">
                  <Check className="size-3 text-emerald-200/70" />
                  Local models by default
                </span>
                <span className="flex items-center gap-1.5">
                  <ShieldCheck className="size-3 text-sky-200/60" />
                  Human handoff built in
                </span>
                <span className="flex items-center gap-1.5">
                  <Sparkles className="size-3 text-indigo-200/60" />
                  Demo needs no microphone
                </span>
              </div>
            </div>

            <div className="mx-auto mt-14 max-w-6xl sm:mt-16">
              <VoiceConsole />
            </div>

            <div className="mx-auto mt-6 flex max-w-6xl flex-wrap items-center justify-between gap-3 px-1 text-[10px] text-white/24">
              <span className="flex items-center gap-1.5">
                <Radio className="size-3 text-sky-200/45" />
                Demo plays labelled sample dialogue — it never places a call.
              </span>
              <span className="font-mono tracking-[0.12em]">
                ORBKIT SHDR-21 · NIMBUS
              </span>
            </div>
          </div>
        </section>

        <section className="relative border-y border-white/[0.06] bg-black/20">
          <div className="mx-auto grid w-full max-w-7xl grid-cols-2 px-5 sm:grid-cols-4 sm:px-8">
            {[
              { value: "16 kHz", label: "PCM16 wire rate" },
              { value: "20 ms", label: "Audio frame cadence" },
              { value: "$0.00", label: "Default spend ceiling" },
              { value: "5", label: "Runtime checkpoints" },
            ].map((stat, index) => (
              <div
                key={stat.label}
                className={
                  "flex flex-col gap-1 py-6 sm:py-7 " +
                  (index > 0 ? "sm:border-l sm:border-white/[0.06] sm:pl-6" : "")
                }
              >
                <span className="text-2xl font-semibold tracking-[-0.05em] text-white sm:text-[28px]">
                  {stat.value}
                </span>
                <span className="font-mono text-[9px] uppercase tracking-[0.14em] text-white/28">
                  {stat.label}
                </span>
              </div>
            ))}
          </div>
        </section>

        <Capabilities />
        <RuntimeSection />
        <SafetySection />
        <FinalCta />
      </main>

      <SiteFooter />
    </div>
  );
}

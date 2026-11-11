import {
  Check,
  Cpu,
  Gauge,
  LockKeyhole,
  Mic2,
  Radio,
  ShieldCheck,
  Waves,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { SectionEyebrow } from "@/components/sections/capabilities";

const PIPELINE = [
  {
    step: "01",
    title: "Capture",
    detail: "AudioWorklet · 20 ms frames · 16 kHz PCM16",
    icon: Mic2,
    status: "Browser",
  },
  {
    step: "02",
    title: "Understand",
    detail: "Silero VAD · faster-whisper speech-to-text",
    icon: Waves,
    status: "Local",
  },
  {
    step: "03",
    title: "Decide",
    detail: "Ollama / Gemma · practice context · tools",
    icon: Cpu,
    status: "Local",
  },
  {
    step: "04",
    title: "Speak",
    detail: "Kokoro-82M · interruption-safe playback",
    icon: Radio,
    status: "Local",
  },
  {
    step: "05",
    title: "Guard",
    detail: "Budget ceiling · data policy · redaction",
    icon: LockKeyhole,
    status: "Always on",
  },
];

const PRINCIPLES = [
  "Zero spend by default — local models handle every default path.",
  "Patient data stays put unless a human approved the route.",
  "Interruption is a first-class signal, not an edge case.",
  "A small, auditable surface — no hidden plugin search.",
];

export function RuntimeSection() {
  return (
    <section
      id="runtime"
      className="relative scroll-mt-20 border-t border-white/[0.06]"
    >
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(50rem_30rem_at_18%_38%,rgba(129,140,248,0.06),transparent_70%)]" />

      <div className="relative mx-auto grid w-full max-w-7xl gap-14 px-5 py-20 sm:px-8 sm:py-28 lg:grid-cols-[0.88fr_1.12fr] lg:items-center">
        <div>
          <SectionEyebrow>The runtime</SectionEyebrow>
          <h2 className="mt-4 text-4xl font-semibold leading-[0.98] text-white sm:text-5xl">
            Small, auditable,
            <br />
            <span className="text-gradient-soft">local-first.</span>
          </h2>
          <p className="mt-5 max-w-md text-sm leading-relaxed text-white/42">
            Frames are the public interface. Every provider, transport and
            module is a static fact in the tree — not something discovered at
            runtime. That is what makes the voice path inspectable.
          </p>

          <ul className="mt-8 space-y-3">
            {PRINCIPLES.map((principle) => (
              <li
                key={principle}
                className="flex items-start gap-3 text-[13px] leading-relaxed text-white/50"
              >
                <span className="mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-full border border-emerald-300/20 bg-emerald-300/[0.06]">
                  <Check className="size-2.5 text-emerald-200/80" />
                </span>
                {principle}
              </li>
            ))}
          </ul>

          <div className="mt-8 flex flex-wrap items-center gap-2">
            <Badge
              variant="outline"
              className="h-7 gap-1.5 rounded-full border-white/10 bg-white/[0.025] px-3 text-[10px] font-medium text-white/45"
            >
              <Gauge className="size-3 text-sky-200/70" />
              Budget guard
            </Badge>
            <Badge
              variant="outline"
              className="h-7 gap-1.5 rounded-full border-white/10 bg-white/[0.025] px-3 text-[10px] font-medium text-white/45"
            >
              <ShieldCheck className="size-3 text-emerald-200/70" />
              Redaction
            </Badge>
            <Badge
              variant="outline"
              className="h-7 gap-1.5 rounded-full border-white/10 bg-white/[0.025] px-3 text-[10px] font-medium text-white/45"
            >
              <Waves className="size-3 text-indigo-200/70" />
              Barge-in
            </Badge>
          </div>
        </div>

        <Card className="surface relative gap-0 overflow-hidden rounded-3xl py-0">
          <div className="flex items-center justify-between border-b border-white/[0.07] px-5 py-4 sm:px-6">
            <div>
              <p className="font-mono text-[9px] tracking-[0.18em] text-white/28">
                SIGNAL PATH
              </p>
              <h3 className="mt-1 text-sm font-semibold text-white">
                One conversation, five checkpoints
              </h3>
            </div>
            <Badge
              variant="outline"
              className="h-6 rounded-full border-emerald-300/15 bg-emerald-300/[0.05] px-2.5 font-mono text-[8.5px] tracking-[0.12em] text-emerald-100/60"
            >
              LOCAL DEFAULT
            </Badge>
          </div>

          <div className="px-5 py-2 sm:px-6">
            {PIPELINE.map((item, index) => {
              const Icon = item.icon;
              return (
                <div key={item.step}>
                  <div className="group flex items-center gap-3.5 py-4">
                    <div className="relative flex size-9 shrink-0 items-center justify-center rounded-xl border border-white/10 bg-white/[0.03] text-sky-100/70 transition-colors group-hover:border-sky-200/20 group-hover:bg-sky-300/[0.06]">
                      <Icon className="size-4" />
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-baseline gap-2">
                        <span className="font-mono text-[9px] text-white/22">
                          {item.step}
                        </span>
                        <span className="text-[13px] font-semibold text-white/85">
                          {item.title}
                        </span>
                      </div>
                      <p className="truncate text-[11px] text-white/34">
                        {item.detail}
                      </p>
                    </div>
                    <span className="shrink-0 rounded-full border border-white/[0.07] bg-black/20 px-2 py-0.5 font-mono text-[8.5px] tracking-[0.08em] text-white/30">
                      {item.status}
                    </span>
                  </div>
                  {index < PIPELINE.length - 1 ? (
                    <Separator className="ml-12 w-[calc(100%-3rem)] bg-white/[0.05]" />
                  ) : null}
                </div>
              );
            })}
          </div>

          <div className="mt-2 flex items-center gap-2 border-t border-white/[0.06] bg-black/20 px-5 py-3.5 sm:px-6">
            <span className="relative flex size-1.5">
              <span className="absolute inline-flex size-full animate-ping rounded-full bg-emerald-300/60" />
              <span className="relative inline-flex size-1.5 rounded-full bg-emerald-300" />
            </span>
            <p className="text-[10px] text-white/32">
              No billable path is reachable unless the deployment explicitly
              enables it.
            </p>
          </div>
        </Card>
      </div>
    </section>
  );
}

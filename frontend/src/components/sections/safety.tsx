import {
  Fingerprint,
  LockKeyhole,
  ScanEye,
  ShieldCheck,
  Sparkles,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { SectionEyebrow } from "@/components/sections/capabilities";

const CONTROLS = [
  {
    icon: Fingerprint,
    title: "Synthetic data mode",
    body: "Providers whose terms allow training on submitted content are unreachable while the runtime is in synthetic mode.",
  },
  {
    icon: LockKeyhole,
    title: "Spend ceiling",
    body: "Every paid provider is off by default. A session that reaches its ceiling stops instead of continuing to a charge.",
  },
  {
    icon: ScanEye,
    title: "Redacted by default",
    body: "Log output passes through the runtime's redaction layer before it reaches a file or the console.",
  },
];

export function SafetySection() {
  return (
    <section
      id="safety"
      className="relative scroll-mt-20 border-t border-white/[0.06]"
    >
      <div className="mx-auto w-full max-w-7xl px-5 py-20 sm:px-8 sm:py-28">
        <div className="surface noise relative overflow-hidden rounded-[32px]">
          <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(46rem_26rem_at_78%_42%,rgba(94,234,212,0.08),transparent_68%)]" />
          <div className="pointer-events-none absolute inset-0 grid-lines opacity-[0.16] [mask-image:radial-gradient(circle_at_78%_50%,black,transparent_64%)]" />

          <div className="relative grid gap-12 px-6 py-12 sm:px-10 sm:py-14 lg:grid-cols-[1.08fr_0.92fr] lg:items-center">
            <div>
              <SectionEyebrow>Privacy by construction</SectionEyebrow>
              <h2 className="mt-4 max-w-xl text-4xl font-semibold leading-[0.98] text-white sm:text-[46px]">
                Patient data stays put
                <br />
                <span className="text-gradient-soft">
                  unless a human decided otherwise.
                </span>
              </h2>
              <p className="mt-5 max-w-lg text-sm leading-relaxed text-white/44">
                The default configuration routes nothing to a provider that may
                train on submitted content. Remote paths are explicit,
                rate-limited and metered by an in-process budget guard. The
                safer setting is the one you get without asking.
              </p>

              <div className="mt-8 grid gap-3 sm:grid-cols-3">
                {CONTROLS.map((control) => {
                  const Icon = control.icon;
                  return (
                    <div
                      key={control.title}
                      className="rounded-2xl border border-white/[0.07] bg-black/20 p-4"
                    >
                      <Icon className="size-4 text-emerald-200/75" />
                      <h3 className="mt-3 text-[12px] font-semibold text-white/80">
                        {control.title}
                      </h3>
                      <p className="mt-1.5 text-[10.5px] leading-relaxed text-white/34">
                        {control.body}
                      </p>
                    </div>
                  );
                })}
              </div>

              <div className="mt-7 flex flex-wrap items-center gap-2">
                <Badge
                  variant="outline"
                  className="h-7 gap-1.5 rounded-full border-emerald-300/15 bg-emerald-300/[0.045] px-3 text-[10px] text-emerald-100/60"
                >
                  <ShieldCheck className="size-3" />
                  In-process policy
                </Badge>
                <Badge
                  variant="outline"
                  className="h-7 gap-1.5 rounded-full border-white/10 bg-white/[0.025] px-3 text-[10px] text-white/40"
                >
                  <Sparkles className="size-3 text-sky-200/65" />
                  Auditable by design
                </Badge>
              </div>
            </div>

            <div className="relative flex min-h-[300px] items-center justify-center">
              <div className="absolute size-[230px] rounded-full border border-white/[0.07]" />
              <div className="absolute size-[290px] rounded-full border border-dashed border-white/[0.055]" />
              <div className="absolute size-[350px] rounded-full border border-white/[0.035]" />
              <div className="absolute size-[230px] rounded-full bg-emerald-300/[0.06] blur-3xl" />

              <div className="animate-float-slow relative flex size-[184px] flex-col items-center justify-center rounded-full border border-white/10 bg-gradient-to-b from-white/[0.07] to-white/[0.015] text-center shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_30px_80px_-40px_rgba(0,0,0,0.9)] backdrop-blur-md">
                <div className="flex size-15 items-center justify-center rounded-full border border-emerald-200/20 bg-emerald-300/[0.08]">
                  <ShieldCheck className="size-7 text-emerald-200" />
                </div>
                <p className="mt-4 font-mono text-[9px] tracking-[0.18em] text-white/35">
                  DATA MODE
                </p>
                <p className="mt-1.5 text-[15px] font-semibold text-white">
                  Synthetic
                </p>
                <p className="mt-0.5 text-[10px] text-emerald-100/50">
                  Remote training blocked
                </p>
              </div>

              <div className="absolute left-0 top-6 rounded-full border border-white/[0.08] bg-[#08090c]/85 px-3 py-1.5 font-mono text-[8.5px] tracking-[0.1em] text-white/35 backdrop-blur-md">
                BUDGET $0.00
              </div>
              <div className="absolute bottom-8 right-0 rounded-full border border-white/[0.08] bg-[#08090c]/85 px-3 py-1.5 font-mono text-[8.5px] tracking-[0.1em] text-white/35 backdrop-blur-md">
                REDACTION ON
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

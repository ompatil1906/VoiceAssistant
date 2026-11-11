import { ArrowRight, ArrowUpRight, AudioLines } from "lucide-react";

import { QuantumLoopWordmark } from "@/components/brand/quantumloop-wordmark";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";

const FOOTER_LINKS = [
  {
    title: "Product",
    links: [
      { label: "Voice console", href: "#voice" },
      { label: "Capabilities", href: "#capabilities" },
      { label: "Runtime", href: "#runtime" },
      { label: "Safety", href: "#safety" },
    ],
  },
  {
    title: "Runtime",
    links: [
      { label: "Frames & pipeline", href: "#runtime" },
      { label: "Speech path", href: "#runtime" },
      { label: "Cost policy", href: "#safety" },
      { label: "Redaction", href: "#safety" },
    ],
  },
  {
    title: "EMMA",
    links: [
      { label: "Administrative reception", href: "#capabilities" },
      { label: "Human handoff", href: "#capabilities" },
      { label: "Not a medical device", href: "#safety" },
    ],
  },
];

export function FinalCta() {
  return (
    <section className="relative border-t border-white/[0.06]">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(46rem_24rem_at_50%_100%,rgba(125,211,252,0.08),transparent_70%)]" />
      <div className="relative mx-auto flex w-full max-w-4xl flex-col items-center px-5 py-24 text-center sm:px-8 sm:py-32">
        <div className="flex size-12 items-center justify-center rounded-full border border-white/10 bg-white/[0.03]">
          <AudioLines className="size-5 text-sky-200" />
        </div>
        <h2 className="mt-7 text-4xl font-semibold leading-[0.98] text-white sm:text-6xl">
          Ready to hear
          <br />
          <span className="text-gradient">EMMA answer?</span>
        </h2>
        <p className="mt-5 max-w-lg text-sm leading-relaxed text-white/42">
          Try the labelled demo in the browser, then point the console at your
          own gateway when the runtime is running. No microphone is needed to
          look around.
        </p>
        <div className="mt-8 flex flex-col items-center gap-3 sm:flex-row">
          <Button
            asChild
            className="h-11 rounded-full bg-white px-6 text-[13px] font-semibold text-black hover:bg-white/90"
          >
            <a href="#voice">
              Open the voice console
              <ArrowRight className="size-4" />
            </a>
          </Button>
          <Button
            asChild
            variant="outline"
            className="h-11 rounded-full border-white/10 bg-white/[0.03] px-6 text-[13px] text-white/70 hover:bg-white/[0.07] hover:text-white"
          >
            <a href="#runtime">
              Inspect the runtime
              <ArrowUpRight className="size-4" />
            </a>
          </Button>
        </div>
      </div>
    </section>
  );
}

export function SiteFooter() {
  return (
    <footer className="relative border-t border-white/[0.06] bg-black/20">
      <div className="mx-auto w-full max-w-7xl px-5 py-14 sm:px-8">
        <div className="grid gap-12 lg:grid-cols-[1.1fr_1.9fr]">
          <div>
            <QuantumLoopWordmark className="h-[22px] w-auto" />
            <p className="mt-4 max-w-xs text-[12px] leading-relaxed text-white/35">
              Real-time voice infrastructure for EMMA, an administrative
              receptionist for GP practices.
            </p>
            <div className="mt-5 flex items-center gap-2">
              <span className="relative flex size-1.5">
                <span className="absolute inline-flex size-full animate-ping rounded-full bg-emerald-300/60" />
                <span className="relative inline-flex size-1.5 rounded-full bg-emerald-300" />
              </span>
              <span className="font-mono text-[9px] tracking-[0.16em] text-white/28">
                QUANTUMLOOPAI / EMMA RUNTIME
              </span>
            </div>
          </div>

          <div className="grid gap-8 sm:grid-cols-3">
            {FOOTER_LINKS.map((column) => (
              <div key={column.title}>
                <h3 className="font-mono text-[9px] font-medium uppercase tracking-[0.18em] text-white/28">
                  {column.title}
                </h3>
                <ul className="mt-4 space-y-2.5">
                  {column.links.map((link) => (
                    <li key={link.label}>
                      <a
                        href={link.href}
                        className="text-[12px] text-white/42 transition-colors hover:text-white/80"
                      >
                        {link.label}
                      </a>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </div>

        <Separator className="my-10 bg-white/[0.06]" />

        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <p className="max-w-2xl text-[10.5px] leading-relaxed text-white/26">
            © 2026 QuantumLoopAI. EMMA is an administrative receptionist. It does
            not diagnose, triage or give clinical advice, and is not a medical
            device. Inherited portions retain their original notices.
          </p>
          <div className="flex shrink-0 items-center gap-4 font-mono text-[9px] tracking-[0.12em] text-white/24">
            <span>BSD-2-CLAUSE</span>
            <span className="size-1 rounded-full bg-white/15" />
            <span>ATTRIBUTION KEPT</span>
          </div>
        </div>
      </div>
    </footer>
  );
}

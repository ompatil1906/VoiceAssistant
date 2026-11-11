import {
  ArrowUpRight,
  CalendarCheck2,
  ClipboardList,
  UserRoundCheck,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";

const CAPABILITIES = [
  {
    index: "01",
    icon: CalendarCheck2,
    title: "Appointment requests",
    body: "Collects the reason, preferred window and caller details, then reads the request back before anything is submitted.",
    tag: "Structured intake",
  },
  {
    index: "02",
    icon: ClipboardList,
    title: "Prescription queries",
    body: "Routes routine prescription requests through the practice's approved workflow and captures what reception needs to follow up.",
    tag: "Practice workflow",
  },
  {
    index: "03",
    icon: UserRoundCheck,
    title: "Safe escalation",
    body: "Recognises when a caller needs a person, summarises the context, and hands off without pretending to be clinical.",
    tag: "Human handoff",
  },
];

export function Capabilities() {
  return (
    <section
      id="capabilities"
      className="relative scroll-mt-20 border-t border-white/[0.06]"
    >
      <div className="mx-auto w-full max-w-7xl px-5 py-20 sm:px-8 sm:py-28">
        <div className="flex flex-col gap-8 lg:flex-row lg:items-end lg:justify-between">
          <div className="max-w-2xl">
            <SectionEyebrow>Capabilities</SectionEyebrow>
            <h2 className="mt-4 text-4xl font-semibold leading-[0.98] text-white sm:text-5xl">
              Routine calls,
              <br />
              <span className="text-gradient-soft">handled with care.</span>
            </h2>
          </div>
          <p className="max-w-md text-sm leading-relaxed text-white/42">
            EMMA is built for the repetitive, high-volume moments that pull
            reception away from the people in front of them — without pretending
            to make a clinical judgement.
          </p>
        </div>

        <div className="mt-12 grid gap-4 md:grid-cols-3">
          {CAPABILITIES.map((item) => {
            const Icon = item.icon;
            return (
              <Card
                key={item.index}
                className="group surface relative gap-0 rounded-2xl py-0 transition-colors duration-300 hover:border-sky-200/20"
              >
                <div className="flex items-start justify-between px-6 pt-6">
                  <div className="relative flex size-11 items-center justify-center rounded-xl border border-white/10 bg-white/[0.03]">
                    <Icon className="size-5 text-sky-100/80" />
                    <span className="absolute inset-0 rounded-xl bg-sky-300/0 blur-md transition-colors duration-300 group-hover:bg-sky-300/10" />
                  </div>
                  <span className="font-mono text-[10px] tracking-[0.18em] text-white/22">
                    {item.index}
                  </span>
                </div>

                <div className="flex flex-1 flex-col px-6 pb-6 pt-8">
                  <Badge
                    variant="outline"
                    className="mb-4 h-5 w-fit rounded-full border-white/10 bg-white/[0.025] px-2 font-mono text-[9px] tracking-[0.12em] text-white/35"
                  >
                    {item.tag}
                  </Badge>
                  <h3 className="text-xl font-semibold tracking-[-0.035em] text-white">
                    {item.title}
                  </h3>
                  <p className="mt-2.5 flex-1 text-[13px] leading-relaxed text-white/44">
                    {item.body}
                  </p>
                  <div className="mt-6 flex items-center gap-1.5 text-[11px] font-medium text-white/28 transition-colors group-hover:text-sky-100/70">
                    Built into the runtime
                    <ArrowUpRight className="size-3 transition-transform duration-300 group-hover:-translate-y-0.5 group-hover:translate-x-0.5" />
                  </div>
                </div>
              </Card>
            );
          })}
        </div>
      </div>
    </section>
  );
}

export function SectionEyebrow({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-2.5 font-mono text-[10px] font-medium uppercase tracking-[0.2em] text-white/35">
      <span className="h-px w-6 bg-gradient-to-r from-sky-300/70 to-transparent" />
      {children}
    </div>
  );
}

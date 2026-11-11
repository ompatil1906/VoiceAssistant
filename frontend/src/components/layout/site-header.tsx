"use client";

import { useState } from "react";
import { ArrowUpRight, Menu, Radio, ShieldCheck } from "lucide-react";

import { QuantumLoopWordmark } from "@/components/brand/quantumloop-wordmark";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";

const NAV = [
  { label: "Voice console", href: "#voice" },
  { label: "Capabilities", href: "#capabilities" },
  { label: "Runtime", href: "#runtime" },
  { label: "Safety", href: "#safety" },
];

export function SiteHeader() {
  const [open, setOpen] = useState(false);

  return (
    <header className="sticky top-0 z-50 border-b border-white/[0.06] bg-[#050506]/72 backdrop-blur-xl">
      <div className="mx-auto flex h-16 w-full max-w-7xl items-center justify-between px-5 sm:px-8">
        <a
          href="#top"
          className="group flex items-center rounded-full outline-none focus-visible:ring-2 focus-visible:ring-sky-300/60"
          aria-label="QuantumLoopAI home"
        >
          <QuantumLoopWordmark className="h-[21px] w-auto" />
        </a>

        <nav className="hidden items-center gap-1 rounded-full border border-white/[0.07] bg-white/[0.025] p-1 md:flex">
          {NAV.map((item) => (
            <a
              key={item.href}
              href={item.href}
              className="rounded-full px-3.5 py-1.5 text-[13px] font-medium text-white/58 transition-colors hover:bg-white/[0.06] hover:text-white"
            >
              {item.label}
            </a>
          ))}
        </nav>

        <div className="flex items-center gap-2.5">
          <Badge
            variant="outline"
            className="hidden h-8 gap-1.5 border-white/10 bg-white/[0.03] px-3 text-[11px] font-medium tracking-[0.08em] text-white/58 lg:inline-flex"
          >
            <span className="relative flex size-1.5">
              <span className="absolute inline-flex size-full animate-ping rounded-full bg-emerald-300/70" />
              <span className="relative inline-flex size-1.5 rounded-full bg-emerald-300" />
            </span>
            LOCAL-FIRST
          </Badge>

          <Button
            asChild
            size="sm"
            className="hidden h-9 rounded-full bg-white px-4 text-[13px] font-semibold text-black hover:bg-white/90 sm:inline-flex"
          >
            <a href="#voice">
              Open console
              <ArrowUpRight className="size-3.5" />
            </a>
          </Button>

          <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger asChild>
              <Button
                variant="outline"
                size="icon"
                className="size-9 rounded-full border-white/10 bg-white/[0.03] text-white md:hidden"
                aria-label="Open navigation"
              >
                <Menu className="size-4" />
              </Button>
            </DialogTrigger>
            <DialogContent className="border-white/10 bg-[#0a0b0f] text-white sm:max-w-sm">
              <DialogHeader>
                <DialogTitle className="flex items-center gap-2.5">
                  <QuantumLoopWordmark className="h-[21px] w-auto" />
                </DialogTitle>
                <DialogDescription className="text-white/50">
                  EMMA voice runtime by QuantumLoopAI.
                </DialogDescription>
              </DialogHeader>
              <nav className="mt-2 flex flex-col gap-1">
                {NAV.map((item) => (
                  <a
                    key={item.href}
                    href={item.href}
                    onClick={() => setOpen(false)}
                    className="flex items-center justify-between rounded-xl border border-transparent px-3 py-3 text-sm text-white/70 transition hover:border-white/10 hover:bg-white/[0.04] hover:text-white"
                  >
                    {item.label}
                    <ArrowUpRight className="size-4 text-white/30" />
                  </a>
                ))}
              </nav>
              <div className="mt-2 flex items-center gap-2 rounded-xl border border-white/10 bg-white/[0.03] px-3 py-2.5 text-xs text-white/50">
                <ShieldCheck className="size-4 text-emerald-300" />
                Local providers by default. No billable path.
              </div>
              <Button
                asChild
                className="mt-1 h-10 rounded-xl bg-white text-black hover:bg-white/90"
                onClick={() => setOpen(false)}
              >
                <a href="#voice">
                  <Radio className="size-4" />
                  Open voice console
                </a>
              </Button>
            </DialogContent>
          </Dialog>
        </div>
      </div>
    </header>
  );
}

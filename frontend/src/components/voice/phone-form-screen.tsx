"use client";

import { useId, useRef, useState, type FormEvent } from "react";
import { ArrowRight, ShieldCheck } from "lucide-react";

import { QuantumLoopWordmark } from "@/components/brand/quantumloop-wordmark";
import type { VoiceCaller } from "@/lib/voice/types";
import { cn } from "@/lib/utils";

const inputClass =
  "h-[32px] w-full rounded-[7px] border border-[#16130f]/12 bg-white/75 px-2 text-[11px] text-[#16130f] outline-none transition placeholder:text-[#16130f]/28 focus:border-[#16130f]/40 focus:bg-white";

/**
 * Intake screen rendered inside the phone mockup.
 *
 * Collects the four details EMMA's practice record needs before the call
 * starts. Only the first name travels to the gateway (it is the single value
 * the `/ws/exotel` contract accepts); the rest stay in the session so the call
 * screen can identify the caller.
 */
export function PhoneFormScreen({
  caller,
  onChange,
  onSubmit,
}: {
  caller: VoiceCaller;
  onChange: (patch: Partial<VoiceCaller>) => void;
  onSubmit: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const firstNameRef = useRef<HTMLInputElement>(null);
  const emailRef = useRef<HTMLInputElement>(null);
  const mobileRef = useRef<HTMLInputElement>(null);
  const ids = useId();

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();

    if (!caller.firstName.trim()) {
      setError("Add your first name so EMMA can greet you.");
      firstNameRef.current?.focus();
      return;
    }

    if (!caller.email.trim() || !caller.email.includes("@")) {
      setError("Enter a valid email address so EMMA can send records and prescriptions.");
      emailRef.current?.focus();
      return;
    }

    const digits = caller.mobile.replace(/\D/g, "");
    if (digits.length < 8 || digits.length > 15) {
      setError("Enter a mobile number EMMA can call back on.");
      mobileRef.current?.focus();
      return;
    }

    setError(null);
    onSubmit();
  };

  return (
    <div className="flex h-full flex-col bg-[#f4efe6] px-4 pb-5 pt-8 text-[#16130f]">
      <div className="flex items-center justify-between">
        <QuantumLoopWordmark className="h-[11px] w-auto text-[#16130f]" />
        <span className="rounded-full border border-[#16130f]/15 px-1.5 py-[2px] font-mono text-[6.5px] uppercase tracking-[0.18em] text-[#16130f]/45">
          Reception
        </span>
      </div>

      <form
        onSubmit={submit}
        noValidate
        className="flex min-h-0 flex-1 flex-col"
      >
        <div className="flex flex-1 flex-col justify-center pb-3">
          <div
            aria-hidden="true"
            className="mx-auto mb-4 size-11 rounded-full bg-[radial-gradient(circle_at_32%_26%,#ffe3c0,#8f2c60_62%,#4b1d3a)] shadow-[0_10px_30px_-8px_rgba(143,44,96,0.55)]"
          />
          <h1 className="text-center text-[18px] font-semibold leading-[1.12] tracking-[-0.01em]">
            Talk to EMMA.
          </h1>
          <p className="mt-1 text-center text-[10px] leading-[1.5] text-[#16130f]/50">
            Add your details, then EMMA joins the line.
          </p>

          <div className="mt-3.5 space-y-[9px]">
            <label className="block">
              <span className="mb-[3px] block text-[7.5px] font-medium uppercase tracking-[0.16em] text-[#16130f]/40">
                First name
              </span>
              <input
                ref={firstNameRef}
                id={`${ids}-first`}
                name="firstName"
                autoComplete="given-name"
                maxLength={40}
                placeholder="Sarah"
                value={caller.firstName}
                onChange={(event) => onChange({ firstName: event.target.value })}
                aria-invalid={error !== null && !caller.firstName.trim()}
                className={inputClass}
              />
            </label>

            <label className="block">
              <span className="mb-[3px] block text-[7.5px] font-medium uppercase tracking-[0.16em] text-[#16130f]/40">
                Last name
              </span>
              <input
                name="lastName"
                autoComplete="family-name"
                maxLength={40}
                placeholder="Iyer"
                value={caller.lastName}
                onChange={(event) => onChange({ lastName: event.target.value })}
                className={inputClass}
              />
            </label>

            <label className="block">
              <span className="mb-[3px] block text-[7.5px] font-medium uppercase tracking-[0.16em] text-[#16130f]/40">
                Email address
              </span>
              <input
                ref={emailRef}
                name="email"
                type="email"
                autoComplete="email"
                maxLength={80}
                placeholder="sarah.iyer@example.com"
                value={caller.email}
                onChange={(event) => onChange({ email: event.target.value })}
                aria-invalid={error !== null && !caller.email.trim()}
                className={inputClass}
              />
            </label>

            <label className="block">
              <span className="mb-[3px] block text-[7.5px] font-medium uppercase tracking-[0.16em] text-[#16130f]/40">
                Mobile number
              </span>
              <input
                ref={mobileRef}
                name="mobile"
                type="tel"
                inputMode="tel"
                autoComplete="tel"
                maxLength={20}
                placeholder="+91 98765 43210"
                value={caller.mobile}
                onChange={(event) => onChange({ mobile: event.target.value })}
                className={inputClass}
              />
            </label>

            <label className="block">
              <span className="mb-[3px] block text-[7.5px] font-medium uppercase tracking-[0.16em] text-[#16130f]/40">
                Reason
              </span>
              <textarea
                name="reason"
                rows={2}
                maxLength={200}
                placeholder="Appointment, refill, results…"
                value={caller.reason}
                onChange={(event) => onChange({ reason: event.target.value })}
                className={cn(inputClass, "h-auto resize-none py-1.5 leading-[1.4]")}
              />
            </label>
          </div>

          <div className="mt-3 flex items-start gap-1.5 rounded-[7px] border border-[#16130f]/10 bg-white/40 px-2 py-1.5">
            <ShieldCheck
              className="mt-px size-3 shrink-0 text-[#16130f]/40"
              aria-hidden="true"
            />
            <p className="text-[7.5px] leading-[1.5] text-[#16130f]/45">
              Audio streams only while the call is live. Nothing is kept in this
              browser beyond the details above.
            </p>
          </div>
        </div>

        <div className="border-t border-[#16130f]/10 pt-3">
          {error ? (
            <p role="alert" className="mb-2 text-[9px] leading-[1.45] text-[#a3212a]">
              {error}
            </p>
          ) : null}

          <button
            type="submit"
            className="flex h-10 w-full items-center justify-center gap-1.5 rounded-full bg-[#16130f] text-[11.5px] font-semibold text-[#f7f2ea] transition active:scale-[0.985]"
          >
            Call now
            <ArrowRight className="size-3.5" aria-hidden="true" />
          </button>
        </div>
      </form>
    </div>
  );
}

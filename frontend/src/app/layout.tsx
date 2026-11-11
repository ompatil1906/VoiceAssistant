import type { Metadata, Viewport } from "next";

import { TooltipProvider } from "@/components/ui/tooltip";
import "./globals.css";

export const metadata: Metadata = {
  title: "EMMA Voice Console — QuantumLoopAI",
  description:
    "EMMA is a voice-first administrative receptionist for GP practices, built by QuantumLoopAI. Answer routine calls, capture appointment and prescription requests, and hand off to a human when it matters.",
  applicationName: "EMMA",
  authors: [{ name: "QuantumLoopAI" }],
  robots: { index: true, follow: true },
  openGraph: {
    title: "EMMA Voice Console — QuantumLoopAI",
    description:
      "A voice-first administrative receptionist for GP practices. Local-first, interruption-safe, auditable by design.",
    type: "website",
  },
};

export const viewport: Viewport = {
  themeColor: "#050506",
  colorScheme: "dark",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen bg-background text-foreground">
        <TooltipProvider delayDuration={220}>{children}</TooltipProvider>
      </body>
    </html>
  );
}

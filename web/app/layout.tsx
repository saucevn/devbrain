import type { Metadata } from "next";
import { JetBrains_Mono, Newsreader } from "next/font/google";
import "./globals.css";

// Data / labels / nav / code.
const mono = JetBrains_Mono({
  variable: "--font-jbmono",
  subsets: ["latin", "latin-ext", "vietnamese"],
  display: "swap",
});

// Editorial display + narrative prose. Newsreader supports the vietnamese
// subset (the brain's narratives are VN+EN) and has true italics for the
// "living diary" voice.
const serif = Newsreader({
  variable: "--font-newsreader",
  weight: ["400", "500", "600", "700"],
  style: ["normal", "italic"],
  subsets: ["latin", "latin-ext", "vietnamese"],
  display: "swap",
});

export const metadata: Metadata = {
  title: "devbrain — living changelog",
  description:
    "Event-sourced second brain for a dev team. Deterministic activity heat + co-change, AI narrative, semantic search.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={`${mono.variable} ${serif.variable} h-full antialiased`}>
      <body className="min-h-full">{children}</body>
    </html>
  );
}

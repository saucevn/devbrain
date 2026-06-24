import type { Metadata } from "next";
import { JetBrains_Mono, IBM_Plex_Sans } from "next/font/google";
import "./globals.css";

const mono = JetBrains_Mono({
  variable: "--font-jbmono",
  subsets: ["latin", "latin-ext", "vietnamese"],
  display: "swap",
});

const sans = IBM_Plex_Sans({
  variable: "--font-plex",
  weight: ["400", "500", "600", "700"],
  subsets: ["latin", "latin-ext", "vietnamese"],
  display: "swap",
});

export const metadata: Metadata = {
  title: "devbrain — living changelog",
  description:
    "Event-sourced second brain for a dev team. Deterministic activity heat + co-change.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={`${mono.variable} ${sans.variable} h-full antialiased`}>
      <body className="min-h-full">{children}</body>
    </html>
  );
}

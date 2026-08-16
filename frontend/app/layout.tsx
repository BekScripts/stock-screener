import type { Metadata } from "next";
import Link from "next/link";

import "./globals.css";

export const metadata: Metadata = {
  title: "Compounder Radar",
  description: "Rankings, grounded AI research and a watchlist.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="shell">
          <header className="masthead">
            <div>
              <h1>
                <Link href="/">Compounder Radar</Link>
              </h1>
              <div className="tagline">Stocks worth researching, and why.</div>
            </div>
            <nav className="top">
              <Link href="/">Rankings</Link>
              <Link href="/watchlist">Watchlist</Link>
            </nav>
          </header>
          {children}
        </div>
      </body>
    </html>
  );
}

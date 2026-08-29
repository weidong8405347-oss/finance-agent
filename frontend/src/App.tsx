import { useState } from "react";
import SessionsPage from "./pages/SessionsPage";
import KnowledgePage from "./pages/KnowledgePage";
import DecisionsPage from "./pages/DecisionsPage";
import EvaluationsPage from "./pages/EvaluationsPage";

type Page = "sessions" | "knowledge" | "decisions" | "evaluations";

const NAV: { key: Page; label: string }[] = [
  { key: "sessions", label: "Sessions" },
  { key: "knowledge", label: "Knowledge" },
  { key: "decisions", label: "Decisions" },
  { key: "evaluations", label: "Evaluations" },
];

export default function App() {
  const [page, setPage] = useState<Page>("sessions");
  return (
    <div className="min-h-screen">
      <header className="border-b border-neutral-200 bg-white">
        <div className="mx-auto flex max-w-6xl items-center gap-6 px-6 py-3">
          <span className="font-mono text-sm font-semibold tracking-tight">finance-agent</span>
          <nav className="flex gap-1">
            {NAV.map((n) => (
              <button
                key={n.key}
                onClick={() => setPage(n.key)}
                className={`rounded px-3 py-1.5 text-sm ${
                  page === n.key
                    ? "bg-neutral-900 text-white"
                    : "text-neutral-600 hover:bg-neutral-100"
                }`}
              >
                {n.label}
              </button>
            ))}
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-6 py-6">
        {page === "sessions" && <SessionsPage />}
        {page === "knowledge" && <KnowledgePage />}
        {page === "decisions" && <DecisionsPage />}
        {page === "evaluations" && <EvaluationsPage />}
      </main>
    </div>
  );
}

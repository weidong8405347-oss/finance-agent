import { useEffect, useState } from "react";
import SessionsPage from "./pages/SessionsPage";
import KnowledgePage from "./pages/KnowledgePage";
import DecisionsPage from "./pages/DecisionsPage";
import EvaluationsPage from "./pages/EvaluationsPage";
import CapabilitiesPage from "./pages/CapabilitiesPage";
import ProvidersPage from "./pages/ProvidersPage";
// ApprovalsBanner 已删：审批内联在对话流（approval/asked 事件 + SSE 驱动），无轮询

type Page = "sessions" | "knowledge" | "decisions" | "evaluations" | "capabilities" | "providers";

const NAV: { key: Page; label: string }[] = [
  { key: "sessions", label: "对话" },
  { key: "knowledge", label: "Knowledge" },
  { key: "decisions", label: "Decisions" },
  { key: "evaluations", label: "Evaluations" },
  { key: "capabilities", label: "能力" },
  { key: "providers", label: "模型" },
];

export default function App() {
  // hash 深链：/#knowledge 直达页面，刷新/分享不丢上下文
  const [page, setPageState] = useState<Page>(() => {
    const h = window.location.hash.slice(1) as Page;
    return NAV.some((n) => n.key === h) ? h : "sessions";
  });
  const setPage = (p: Page) => {
    setPageState(p);
    window.location.hash = p;
  };
  // ProfileCard 等组件经自定义事件请求跳页（对话流 → 档案钻取）
  useEffect(() => {
    const h = (e: Event) => setPage((e as CustomEvent).detail as Page);
    window.addEventListener("nav", h);
    return () => window.removeEventListener("nav", h);
  }, []);
  return (
    <div className="min-h-screen">
      <header className="border-b border-neutral-200 bg-white">
        <div className="mx-auto flex max-w-6xl items-center gap-6 px-6 py-2.5">
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
      {page === "sessions" ? (
        <SessionsPage />
      ) : (
        <main className="mx-auto max-w-6xl px-6 py-6">
          {page === "knowledge" && <KnowledgePage />}
          {page === "decisions" && <DecisionsPage />}
          {page === "evaluations" && <EvaluationsPage />}
          {page === "capabilities" && <CapabilitiesPage />}
          {page === "providers" && <ProvidersPage />}
        </main>
      )}
    </div>
  );
}

// 应用外壳：hash 路由驱动（app/route.ts）——实体、章节、时间、快照、报告级深链
// 可刷新恢复、前进后退；兼容旧 `#knowledge`/`#sessions` 入口与 "nav" 自定义事件。

import { lazy, Suspense, useEffect, useState } from "react";

import { currentRoute, navigate, subscribeRoute, type Route, type TopPage } from "./app/route";
import KnowledgePage from "./pages/KnowledgePage";
import SessionsPage from "./pages/SessionsPage";
import CapabilitiesPage from "./pages/CapabilitiesPage";
import DecisionsPage from "./pages/DecisionsPage";
import EvaluationsPage from "./pages/EvaluationsPage";
import ProvidersPage from "./pages/ProvidersPage";

// 重页面懒加载（§13.3：首屏可读 <2s，图表不阻塞列表与对话）
const StockDossierPage = lazy(() => import("./pages/StockDossierPage"));
const ResearchReportPage = lazy(() => import("./pages/ResearchReportPage"));
const ComparePage = lazy(() => import("./pages/ComparePage"));

const PageFallback = (
  <div className="py-16 text-center text-sm text-neutral-400">页面加载中…</div>
);

const NAV: { key: string; label: string; route: Route }[] = [
  { key: "sessions", label: "对话", route: { page: "sessions" } },
  { key: "knowledge", label: "Knowledge", route: { page: "knowledge", params: {} } },
  { key: "decisions", label: "Decisions", route: { page: "decisions" } },
  { key: "evaluations", label: "Evaluations", route: { page: "evaluations" } },
  { key: "capabilities", label: "能力", route: { page: "capabilities" } },
  { key: "providers", label: "模型", route: { page: "providers" } },
];

function activeNavKey(route: Route): string {
  if (route.page === "knowledge" || route.page === "research" || route.page === "compare") {
    return "knowledge";
  }
  if (route.page === "not_found") return "";
  return route.page;
}

export default function App() {
  const [route, setRoute] = useState<Route>(() => currentRoute());

  useEffect(() => subscribeRoute(setRoute), []);

  // 旧组件（ProfileCard 等）经 "nav" 自定义事件请求跳页——映射到路由对象
  useEffect(() => {
    const h = (e: Event) => {
      const page = String((e as CustomEvent).detail ?? "sessions");
      if (page === "knowledge") navigate({ page: "knowledge", params: {} });
      else navigate({ page: page as TopPage });
    };
    window.addEventListener("nav", h);
    return () => window.removeEventListener("nav", h);
  }, []);

  const active = activeNavKey(route);
  const isDossier = route.page === "knowledge" && route.kind && route.id;

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-30 border-b border-neutral-200 bg-white">
        <div className="mx-auto flex max-w-[1560px] items-center gap-3 px-4 py-2.5 md:gap-6 md:px-6">
          <span className="shrink-0 font-mono text-sm font-semibold tracking-tight">finance-agent</span>
          <nav className="flex gap-1 overflow-x-auto">
            {NAV.map((n) => (
              <button
                key={n.key}
                onClick={() => navigate(n.route)}
                className={`rounded px-3 py-1.5 text-sm ${
                  active === n.key
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
      {route.page === "sessions" ? (
        <SessionsPage />
      ) : isDossier ? (
        // 档案页自管宽度（正文列 720-840 / 桌面最大 1440-1560，§4.5 规则 7）
        <main className="mx-auto max-w-[1560px] px-4 py-5 md:px-6">
          <Suspense fallback={PageFallback}>
            <StockDossierPage kind={route.kind!} id={route.id!} params={route.params} />
          </Suspense>
        </main>
      ) : route.page === "research" ? (
        <main className="mx-auto max-w-6xl px-4 py-6 md:px-6">
          <Suspense fallback={PageFallback}>
            <ResearchReportPage artifactId={route.artifactId} />
          </Suspense>
        </main>
      ) : (
        <main className="mx-auto max-w-[1560px] px-4 py-6 md:px-6">
          {route.page === "knowledge" && <KnowledgePage />}
          {route.page === "compare" && (
            <Suspense fallback={PageFallback}>
              <ComparePage params={route.params} />
            </Suspense>
          )}
          {route.page === "decisions" && <DecisionsPage />}
          {route.page === "evaluations" && <EvaluationsPage />}
          {route.page === "capabilities" && <CapabilitiesPage />}
          {route.page === "providers" && <ProvidersPage />}
          {route.page === "not_found" && (
            <div className="mx-auto max-w-md rounded-lg border border-neutral-200 bg-white p-6 text-center">
              <div className="mb-2 text-sm font-semibold text-neutral-700">未知路由</div>
              <div className="mb-4 font-mono text-xs text-neutral-400">#{route.raw}</div>
              <button onClick={() => navigate({ page: "sessions" })}
                      className="rounded bg-neutral-900 px-3 py-1.5 text-xs text-white">
                回到对话
              </button>
            </div>
          )}
        </main>
      )}
    </div>
  );
}

// 离线导出应用壳（§11.4）：与在线 App 同版式顶栏 + 同一 hash 路由（file:// 下
// hash 路由天然可用，深链/前进后退一致）。只支持本文件包含的路由——档案页与
// 研究报告页；其他页面（档案库/对话/决策/比较）显示离线说明并给返回链接。

import { useEffect, useState } from "react";

import { currentRoute, navigate, subscribeRoute, type Route } from "../app/route";
import type { DossierExportData } from "../features/dossier/api";
import StockDossierPage from "../pages/StockDossierPage";
import ResearchReportPage from "../pages/ResearchReportPage";

export default function ExportApp({ data }: { data: DossierExportData }) {
  const [route, setRoute] = useState<Route>(() => currentRoute());
  useEffect(() => subscribeRoute(setRoute), []);

  const snap = data.snapshot;
  const home: Route = {
    page: "knowledge",
    kind: snap.entity.kind as "stock" | "industry",
    id: snap.entity.id,
    params: { snapshot: snap.context.snapshot_id },
  };
  const isDossier =
    route.page === "knowledge" &&
    route.kind === snap.entity.kind &&
    route.id === snap.entity.id;

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-30 border-b border-neutral-200 bg-white">
        <div className="mx-auto flex max-w-[1560px] items-center gap-3 px-6 py-2.5">
          <span className="font-mono text-sm font-semibold tracking-tight">finance-agent</span>
          <span className="rounded-full border border-indigo-200 bg-indigo-50 px-2 py-0.5 font-mono text-[11px] text-indigo-700"
                title="本文件是冻结导出的自包含 HTML：展示/交互与在线页面一致；as_of 时间旅行、补研、估值试算等服务器操作不可用">
            离线导出 · 冻结于 {snap.context.as_of.slice(0, 10)}
          </span>
          <span className="font-mono text-[11px] text-neutral-400">
            快照 {snap.context.snapshot_id}
          </span>
          <button onClick={() => navigate(home)}
                  className="ml-auto rounded px-3 py-1.5 text-sm text-neutral-600 hover:bg-neutral-100">
            研究档案
          </button>
        </div>
      </header>
      {isDossier ? (
        // 与 App 相同的档案页容器宽度（档案页自管宽度，§4.5 规则 7）
        <main className="mx-auto max-w-[1560px] px-4 py-5 md:px-6">
          <StockDossierPage kind={snap.entity.kind as "stock" | "industry"} id={snap.entity.id}
                            params={route.params} />
        </main>
      ) : route.page === "research" ? (
        <main className="mx-auto max-w-6xl px-4 py-6 md:px-6">
          <ResearchReportPage artifactId={route.artifactId} />
        </main>
      ) : (
        <main className="mx-auto max-w-md px-4 py-16">
          <div className="rounded-lg border border-neutral-200 bg-white p-6 text-center">
            <div className="mb-2 text-sm font-semibold text-neutral-700">离线导出文件</div>
            <p className="mb-4 text-xs leading-relaxed text-neutral-500">
              本文件只包含 {snap.entity.kind}:{snap.entity.id} 的冻结研究档案
              （as_of {snap.context.as_of.slice(0, 10)}）。
              档案库、对话、决策、比较等页面需在线版。
            </p>
            <button onClick={() => navigate(home)}
                    className="rounded bg-neutral-900 px-3 py-1.5 text-xs text-white">
              打开冻结档案
            </button>
          </div>
        </main>
      )}
    </div>
  );
}

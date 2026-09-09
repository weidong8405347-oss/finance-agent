// 股票/行业档案阅读页（设计 §4.3）：三层阅读（10 秒首屏 → 2-5 分钟章节 →
// 10-30 分钟审计）。章节导航（≥1280 侧栏 / 中屏顶部 / 小屏单列），模块懒加载，
// 统一快照上下文（DossierContext），来源抽屉一次点击，as_of 时光机整页一致切换。

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { navigate, withParams, type Route } from "../app/route";
import { dossierApi, ApiError } from "../features/dossier/api";
import {
  ExportMenu, KeyMetricBar, ModuleReasons, ModuleStateBadge, ResearchPanel,
  SourceDrawer, ThesisPanel,
} from "../features/dossier/components";
import { MODULE_COMPONENTS } from "../features/dossier/modules";
import type { DossierSnapshot, ModulePayload } from "../features/dossier/types";

const SECTION_ORDER = [
  "investment_snapshot", "business_engine", "revenue_segments", "key_kpi",
  "financial_quality", "expectations", "valuation_lab", "peers",
  "catalysts_risks", "research_sources",
] as const;

/** 导航模块（audit §3.6）：注册表优先（行业/股票各自的信息架构），
 *  旧快照无注册表时回退固定十模块；not_applicable 不占默认导航。 */
function navSections(snap: DossierSnapshot | null): string[] {
  const reg = snap?.module_registry;
  if (reg?.modules?.length) {
    return reg.modules
      .filter((m) => m.default_nav && snap?.modules[m.module_id]?.status !== "not_applicable")
      .map((m) => m.module_id);
  }
  return [...SECTION_ORDER];
}

interface Props {
  kind: "stock" | "industry";
  id: string;
  params: Record<string, string>;
}

export default function StockDossierPage({ kind, id, params }: Props) {
  const [snap, setSnap] = useState<DossierSnapshot | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [moduleCache, setModuleCache] = useState<Record<string, ModulePayload>>({});
  // 模块加载失败是独立错误态（review #29）：不包成空 payload 交给正常组件
  const [moduleErrors, setModuleErrors] = useState<Record<string, string>>({});
  const [moduleBusy, setModuleBusy] = useState<string | null>(null);
  const [evidenceId, setEvidenceId] = useState<string | null>(params.evidence ?? null);
  const [reloadTick, setReloadTick] = useState(0);
  // 新版本快照提醒（audit §3.9）：只提示，用户确认后才整体切换
  const [newer, setNewer] = useState<{ id: string; modules: string[] } | null>(null);
  // 快照与模块请求分开计数（review #19）：模块加载不得取消快照请求，反之亦然
  const snapSeq = useRef(0);
  const modSeq = useRef(0);

  const sections = useMemo(() => navSections(snap), [snap]);
  const section = params.section && sections.includes(params.section)
    ? params.section
    : sections[0] ?? "investment_snapshot";

  const routeOf = useCallback((): Route => ({ page: "knowledge", kind, id, params }), [kind, id, params]);

  // ---- 快照加载（snapshot 深链优先；as_of/namespace 进入同一冻结上下文） ----
  useEffect(() => {
    const seq = ++snapSeq.current;
    setLoading(true);
    setLoadError(null);
    const p = params.snapshot
      ? dossierApi.snapshot(params.snapshot).then((s) => {
          // URL 同时给 snapshot 与 as_of/entity → 服务端已校验；这里防御性核对
          if (s.entity.id !== id || s.entity.kind !== kind) {
            throw new ApiError(409, `快照属于 ${s.entity.kind}:${s.entity.id}，与 URL 不符`, "snapshot");
          }
          return s;
        })
      : dossierApi.openDossier(kind, id, {
          as_of: params.as_of || undefined,
          namespace: params.namespace || undefined,
          mode: params.as_of ? "historical" : undefined,
        });
    p.then((s) => {
      if (seq !== snapSeq.current) return; // 竞态：过期响应不覆盖（§13.1 前端组）
      // 新快照到达 → 同时作废旧快照的在飞模块请求（防写入过期 payload，review #19）
      modSeq.current += 1;
      setSnap(s);
      setModuleCache({});
      setModuleErrors({});
      setModuleBusy(null);
      setLoading(false);
      // 规范化 URL：把服务端固定的 snapshot_id 写回深链（刷新可恢复）
      if (!params.snapshot && s.context.snapshot_id) {
        navigate(withParams(routeOf(), { snapshot: s.context.snapshot_id }));
      }
    }).catch((e) => {
      if (seq !== snapSeq.current) return;
      setLoadError(e instanceof Error ? e.message : String(e));
      setLoading(false);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, id, params.snapshot, params.as_of, params.namespace, reloadTick]);

  // ---- 模块懒加载（同一快照上下文；独立序号，不与快照请求互取消） ----
  useEffect(() => {
    if (!snap || moduleCache[section] || moduleErrors[section] || moduleBusy === section) return;
    const seq = ++modSeq.current;
    const snapshotId = snap.context.snapshot_id;
    setModuleBusy(section);
    dossierApi.module(snapshotId, section)
      .then((p) => {
        if (seq !== modSeq.current) return;
        setModuleCache((c) => ({ ...c, [section]: p }));
      })
      .catch((e) => {
        if (seq !== modSeq.current) return;
        setModuleErrors((m) => ({
          ...m,
          [section]: e instanceof Error ? e.message : String(e),
        }));
      })
      .finally(() => {
        if (seq === modSeq.current) setModuleBusy(null);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [snap, section]);

  const onEvidenceClick = useCallback((evId: string) => setEvidenceId(evId), []);
  const onOpenArtifact = useCallback((artifactId: string) => {
    navigate({ page: "research", artifactId, params: {} });
  }, []);

  // ---- 新版本快照提醒（audit §3.9）：钉住旧快照时只提示，不混换 ----
  // 历史一致性设计不变（URL 钉住快照）；补研发新快照后，页面提示 changed_modules，
  // 由用户整体切换（绝不把新旧快照的模块混在一页）。
  useEffect(() => {
    if (!params.snapshot) return;
    let cancelled = false;
    const check = () => {
      dossierApi.openDossier(kind, id, { namespace: params.namespace || undefined })
        .then((live) => {
          if (cancelled) return;
          const liveId = live.context.snapshot_id;
          if (!liveId || liveId === params.snapshot) {
            setNewer(null);
            return;
          }
          dossierApi.changes(liveId, params.snapshot)
            .then((diff) => {
              if (!cancelled) setNewer({ id: liveId, modules: diff.changed_modules ?? [] });
            })
            .catch(() => {
              if (!cancelled) setNewer({ id: liveId, modules: [] });
            });
        })
        .catch(() => { /* 提醒失败不影响阅读（旧快照仍可用） */ });
    };
    check();
    const timer = window.setInterval(check, 20000);
    return () => { cancelled = true; window.clearInterval(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, id, params.snapshot, params.namespace, reloadTick]);

  const ModuleComp = MODULE_COMPONENTS[section];
  const moduleState = snap?.modules[section];
  const payload = moduleCache[section];

  const asOfInput = useMemo(() => (params.as_of ?? "").slice(0, 10), [params.as_of]);

  if (loading && !snap) {
    return <div className="py-16 text-center text-sm text-neutral-400">档案投影加载中…</div>;
  }
  if (loadError && !snap) {
    return (
      <div className="mx-auto max-w-md rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
        <div className="mb-1 font-semibold">档案不可读</div>
        <div className="mb-3 text-xs">{loadError}</div>
        <div className="flex gap-2">
          <button onClick={() => setReloadTick((t) => t + 1)}
                  className="rounded border border-red-300 bg-white px-3 py-1 text-xs hover:bg-red-100">
            重试
          </button>
          <button onClick={() => navigate({ page: "knowledge", params: {} })}
                  className="rounded border border-neutral-300 bg-white px-3 py-1 text-xs">
            ← 返回档案库
          </button>
        </div>
      </div>
    );
  }
  if (!snap) return null;

  const isHistorical = snap.context.mode !== "live";
  return (
    <div className="mx-auto max-w-[1560px]">
      {/* ---- 首屏：身份 + 时间锁 + 指标 + 结论（10 秒层） ---- */}
      <header className="mb-4">
        <div className="mb-2 flex flex-wrap items-center gap-3">
          <button onClick={() => navigate({ page: "knowledge", params: {} })}
                  className="rounded border border-neutral-200 px-2 py-1 text-xs text-neutral-500 hover:border-neutral-400">
            ← 档案库
          </button>
          <h2 className="text-2xl font-bold tracking-tight text-neutral-900 md:text-[36px] md:leading-tight">
            {snap.entity.name || snap.entity.id}
            <span className="ml-2 font-mono text-sm font-normal text-neutral-400">
              {snap.entity.kind}:{snap.entity.id}
            </span>
          </h2>
          <span className="rounded-full border border-neutral-200 bg-white px-2 py-0.5 font-mono text-[11px] text-neutral-500">
            配方 {snap.recipe.id}@{snap.recipe.version}
          </span>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <label className="flex items-center gap-1 text-[11px] text-neutral-500">
              知识截止
              <input
                type="date"
                value={asOfInput}
                onChange={(e) => {
                  const v = e.target.value;
                  navigate(withParams(routeOf(), {
                    as_of: v ? `${v}T23:59:59+00:00` : null,
                    snapshot: null, // 切换截止时点 = 打开新快照（时间锁整页生效）
                  }));
                }}
                className="rounded border border-neutral-200 px-1.5 py-0.5 font-mono text-[11px]"
              />
            </label>
            <ResearchPanel snap={snap} onStarted={(sessionId) => {
              navigate({ page: "sessions" });
              window.dispatchEvent(new CustomEvent("nav", { detail: "sessions" }));
              void sessionId;
            }} />
            <ExportMenu snap={snap} />
          </div>
        </div>
        <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-[11px] text-neutral-400">
          <span>as_of {snap.context.as_of.replace("T", " ").slice(0, 19)}Z</span>
          <span>快照 {snap.context.snapshot_id}</span>
          <span>模式 {snap.context.mode}{snap.context.namespace !== "prod" ? ` · ${snap.context.namespace}` : ""}</span>
          <button
            onClick={() => {
              // 刷新 = 重新打开当前档案（review #20）：去掉钉住的 snapshot 参数，
              // 重新投影 live/历史上下文；数据有变则切到新快照并提示 changed_modules
              navigate(withParams(routeOf(), { snapshot: null }));
              setReloadTick((t) => t + 1);
            }}
            className="text-blue-600 hover:underline"
          >
            刷新（检查新快照）
          </button>
          <a href={`/api/knowledge/${snap.entity.kind}/${snap.entity.id}/archives/latest.html`}
             target="_blank" rel="noreferrer" className="hover:underline">
            旧 HTML 存档 ↗
          </a>
        </div>
        {newer && (
          <div className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-900">
            <span>
              本实体已有新版本快照 <span className="font-mono">{newer.id}</span>
              {newer.modules.length > 0 && <>（变化模块：{newer.modules.join("、")}）</>}
              ——当前页仍为钉住的旧快照，不会自动混换。
            </span>
            <button
              onClick={() => {
                navigate(withParams(routeOf(), { snapshot: null }));
                setNewer(null);
                setReloadTick((t) => t + 1);
              }}
              className="rounded border border-emerald-300 bg-white px-2 py-0.5 text-emerald-800 hover:border-emerald-500"
            >
              整体切换到新版本
            </button>
          </div>
        )}
        {isHistorical && (
          <div className="mb-3 rounded-lg border border-indigo-200 bg-indigo-50 px-3 py-2 text-xs text-indigo-800">
            历史视图：正文、图表、来源与质量全部截至 {snap.context.as_of.slice(0, 10)}——
            不借用未来裁决与资料。补研将「以此为基线，研究当前情况」，不回写过去。
            {snap.context.mode === "rebuilt" && "（基于历史证据重建：分析生成于今天）"}
          </div>
        )}
        <KeyMetricBar snap={snap} onMetricClick={(m) => {
          // 指标 → 自己的来源（review #21）：只用该指标观测绑定的证据，
          // 不得把指标关联到无关摘录制造错误背书
          const firstEv = (m.evidence_refs ?? []).find((r) => r.startsWith("ev-"));
          if (firstEv) onEvidenceClick(firstEv);
        }} />
        <div className="mt-3">
          <ThesisPanel snap={snap} />
        </div>
      </header>

      {/* ---- 章节导航 + 模块正文（2-5 分钟层 / 10-30 分钟层） ---- */}
      <div className="flex flex-col gap-4 lg:flex-row">
        <nav className="shrink-0 lg:w-48" aria-label="章节导航">
          <div className="flex gap-1 overflow-x-auto lg:flex-col lg:overflow-visible">
            {sections.map((m) => {
              const st = snap.modules[m];
              if (!st) return null;
              return (
                <button
                  key={m}
                  onClick={() => navigate(withParams(routeOf(), { section: m, evidence: null }))}
                  className={`flex items-center justify-between gap-2 whitespace-nowrap rounded px-2.5 py-1.5 text-left text-xs lg:whitespace-normal ${
                    section === m ? "bg-neutral-900 text-white" : "text-neutral-600 hover:bg-neutral-100"
                  }`}
                >
                  <span>{st.title || m}</span>
                  <ModuleStateBadge status={st.status} reasons={st.reasons} />
                </button>
              );
            })}
          </div>
        </nav>
        <main className="min-w-0 flex-1">
          <section>
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <h3 className="text-lg font-semibold text-neutral-800 md:text-2xl">
                {moduleState?.title || section}
              </h3>
              {moduleState && <ModuleStateBadge status={moduleState.status} reasons={moduleState.reasons} />}
            </div>
            {moduleState && (
              <ModuleReasons reasons={moduleState.reasons} gapRefs={moduleState.gap_refs} />
            )}
            {snap.decision_refs.length > 0 && section === "investment_snapshot" && (
              <div className="mb-2 rounded border border-neutral-200 bg-neutral-50 px-3 py-1.5 text-[11px] text-neutral-500">
                关联决策：{snap.decision_refs.join("、")}（买卖评级/仓位归 Decisions 页，研究更新不改写旧卡）
                <button onClick={() => navigate({ page: "decisions" })} className="ml-2 text-blue-700 hover:underline">查看 →</button>
              </div>
            )}
            <div className="mt-2">
              {moduleBusy === section && !payload && !moduleErrors[section] && (
                <div className="py-8 text-center text-xs text-neutral-400">模块加载中…</div>
              )}
              {moduleErrors[section] && (
                // 模块加载失败独立错误态（review #29）：可重试，不把空 payload
                // 交给正常组件导致页面崩溃，也不用空数据遮蔽失败
                <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-xs text-red-700">
                  <div className="mb-1 font-semibold">模块读取失败（{section}）</div>
                  <div className="mb-2">{moduleErrors[section]}</div>
                  <button
                    onClick={() => {
                      setModuleErrors((m) => { const n = { ...m }; delete n[section]; return n; });
                      setReloadTick((t) => t + 1);
                    }}
                    className="rounded border border-red-300 bg-white px-2 py-1 hover:bg-red-100"
                  >
                    重试
                  </button>
                </div>
              )}
              {payload && ModuleComp && (
                <ModuleComp
                  snap={snap}
                  payload={payload}
                  onEvidenceClick={onEvidenceClick}
                  onOpenArtifact={onOpenArtifact}
                  params={params}
                  onNavigateSection={(next, extra) =>
                    navigate(withParams(routeOf(), { section: next, evidence: null, ...(extra ?? {}) }))}
                  onResolved={() => {
                    // 裁决后丢弃冻结模块缓存并重开档案（新快照反映裁决结果）
                    setModuleCache({});
                    navigate(withParams(routeOf(), { snapshot: null }));
                    setReloadTick((t) => t + 1);
                  }}
                />
              )}
            </div>
            {snap.limitations.length > 0 && section === "research_sources" && (
              <div className="mt-4 rounded border border-neutral-200 bg-neutral-50 p-3 text-[11px] text-neutral-500">
                {snap.limitations.map((l, i) => <div key={i}>· {l}</div>)}
              </div>
            )}
          </section>
        </main>
      </div>

      <SourceDrawer
        snapshotId={snap.context.snapshot_id}
        evidenceId={evidenceId}
        onClose={() => {
          setEvidenceId(null);
          navigate(withParams(routeOf(), { evidence: null }));
        }}
      />
    </div>
  );
}

// 股票/行业档案阅读页（dossier 优化方案 §2-§8 重构版）：
// - Investor Mode（默认）：Hero（身份/阶段/质量/更新）→ Overview 撕页（L0 投资判断）
//   → 深读章节（L2）→ 研究与来源（L3）；运行时元数据（snapshot/namespace/run）不进首屏；
// - Audit Mode（?view=audit 或头部开关）：恢复全部审计信息——快照/模式/配方/模块状态/
//   未完成问题/可信度分层/限制清单；raw evidence ID 只在审计面出现。
// 路由纪律不变：实体、章节、as_of、快照、证据、视图全部可深链、刷新可恢复。

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { navigate, withParams, type Route } from "../app/route";
import { dossierApi, ApiError, isOfflineExport } from "../features/dossier/api";
import {
  AuditSummaryPanel, ExportMenu, ModuleReasons, ModuleStateBadge, ResearchPanel, SourceDrawer,
} from "../features/dossier/components";
import { MODULE_COMPONENTS } from "../features/dossier/modules";
import { OverviewPage, QualityDots } from "../features/dossier/overview";
import { ThesisList } from "../features/dossier/thesis";
import type { ClaimItem, DossierSnapshot, ModulePayload, ThesisObject } from "../features/dossier/types";

const SECTION_ORDER = [
  "investment_snapshot", "business_engine", "revenue_segments", "key_kpi",
  "financial_quality", "expectations", "valuation_lab", "peers",
  "catalysts_risks", "research_sources",
] as const;

/** 审计区模块（§10：研究过程/计划/来源/充分度归 Audit 层，不占投资者导航主线）。 */
const AUDIT_SECTIONS = new Set(["research_sources"]);

/** 导航模块（audit §3.6）：注册表优先；旧快照无注册表时回退固定十模块。 */
function navSections(snap: DossierSnapshot | null): string[] {
  const reg = snap?.module_registry;
  if (reg?.modules?.length) {
    return reg.modules
      .filter((m) => m.default_nav && snap?.modules[m.module_id]?.status !== "not_applicable")
      .map((m) => m.module_id);
  }
  return [...SECTION_ORDER];
}

/** 投资者视图的章节名（§4/§5 的信息架构词汇；注册表标题偏内部命名，这里做展示层映射）。 */
const SECTION_LABEL: Record<string, string> = {
  investment_snapshot: "投资观点",
  industry_chain: "产业链",
  candidate_pool: "公司",
  key_kpi: "关键指标",
  catalysts_risks: "催化与风险",
  business_engine: "商业模式",
  revenue_segments: "收入结构",
  financial_quality: "财务",
  expectations: "预期差",
  valuation_lab: "估值",
  peers: "同业",
  research_sources: "研究与来源",
};

export type DossierView = "investor" | "audit";

function initialView(params: Record<string, string>): DossierView {
  if (params.view === "audit" || params.view === "investor") return params.view;
  try {
    return window.localStorage.getItem("dossier:view") === "audit" ? "audit" : "investor";
  } catch {
    return "investor";
  }
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
  // 离线导出模式（§11.4）：数据层已短路到内嵌冻结数据；服务器依赖的
  // 交互（时间旅行/补研/刷新/再导出/旧存档链接）在此禁用并注明
  const offline = isOfflineExport();
  const [moduleCache, setModuleCache] = useState<Record<string, ModulePayload>>({});
  // 模块加载失败是独立错误态（review #29）：不包成空 payload 交给正常组件
  const [moduleErrors, setModuleErrors] = useState<Record<string, string>>({});
  const [moduleBusy, setModuleBusy] = useState<string | null>(null);
  const [evidenceId, setEvidenceId] = useState<string | null>(params.evidence ?? null);
  const [reloadTick, setReloadTick] = useState(0);
  // 新版本快照提醒（audit §3.9）：只提示，用户确认后才整体切换
  const [newer, setNewer] = useState<{ id: string; modules: string[] } | null>(null);
  const [view, setView] = useState<DossierView>(() => initialView(params));
  // 快照与模块请求分开计数（review #19）：模块加载不得取消快照请求，反之亦然
  const snapSeq = useRef(0);
  const modSeq = useRef(0);

  const sections = useMemo(() => navSections(snap), [snap]);
  const section = params.section && sections.includes(params.section)
    ? params.section
    : sections[0] ?? "investment_snapshot";

  const routeOf = useCallback((): Route => ({ page: "knowledge", kind, id, params }), [kind, id, params]);

  const switchView = useCallback((v: DossierView) => {
    setView(v);
    try { window.localStorage.setItem("dossier:view", v); } catch { /* 隐私模式忽略 */ }
    navigate(withParams(routeOf(), { view: v === "audit" ? "audit" : null }));
  }, [routeOf]);

  // ---- 快照加载（snapshot 深链优先；as_of/namespace 进入同一冻结上下文） ----
  useEffect(() => {
    const seq = ++snapSeq.current;
    setLoading(true);
    setLoadError(null);
    const p = params.snapshot
      ? dossierApi.snapshot(params.snapshot).then((s) => {
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
  useEffect(() => {
    if (!params.snapshot || offline) return; // 离线导出：冻结文件没有「新版本」可查
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
  const isOverview = section === "investment_snapshot";

  if (loading && !snap) {
    return <div className="py-16 text-center text-sm text-ink-faint">档案投影加载中…</div>;
  }
  if (loadError && !snap) {
    return (
      <div className="mx-auto max-w-md dos-card border-risk/40 text-sm text-risk">
        <div className="mb-1 font-semibold">档案不可读</div>
        <div className="mb-3 text-meta">{loadError}</div>
        <div className="flex gap-2">
          <button onClick={() => setReloadTick((t) => t + 1)}
                  className="rounded border border-risk/40 bg-white px-3 py-1 text-meta hover:bg-risk-soft">
            重试
          </button>
          <button onClick={() => navigate({ page: "knowledge", params: {} })}
                  className="rounded border border-line bg-white px-3 py-1 text-meta text-ink-soft">
            ← 返回档案库
          </button>
        </div>
      </div>
    );
  }
  if (!snap) return null;

  const isHistorical = snap.context.mode !== "live";
  const updatedDate = (snap.summary.updated_at ?? snap.context.as_of ?? "").slice(0, 10);
  const candidateCount = ((snap.structures?.candidate_assessment as any)?.candidates ?? []).length;
  const readSections = sections.filter((s) => !AUDIT_SECTIONS.has(s));
  const auditSections = sections.filter((s) => AUDIT_SECTIONS.has(s));

  return (
    <div className="mx-auto max-w-[1440px]">
      {/* ---- Hero（§7/§8：身份 + 阶段 + 质量 + 更新时间；头部徽标 ≤3） ---- */}
      <header className="mb-6">
        {/* 移动端三行堆叠（返回 / 标题+元信息 / 操作），sm 起并排行（§50：单列） */}
        <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-start sm:gap-x-4">
          {!offline && (
            <button onClick={() => navigate({ page: "knowledge", params: {} })}
                    className="self-start rounded border border-line bg-white px-2 py-1 text-meta text-ink-mute hover:border-ink-faint sm:mt-1.5">
              ← 档案库
            </button>
          )}
          <div className="min-w-0 sm:flex-1">
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <h1 className="break-all text-[28px] font-bold tracking-tight text-ink md:text-[32px]">
                {snap.entity.name || snap.entity.id}
              </h1>
              <span className="dos-num text-sm text-ink-faint">
                {snap.entity.kind === "stock" ? "股票" : "行业"} · {snap.entity.id}
              </span>
            </div>
            <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-meta text-ink-mute">
              {snap.summary.stage && (
                <span className="max-w-[52ch] truncate rounded-full border border-accent/30 bg-accent-soft px-2 py-0.5 font-medium text-accent"
                      title={snap.summary.stage}>
                  {/* 展示层截断（全文在 title 悬停）——stage 语义上限是「一句话」 */}
                  {snap.summary.stage.length > 48
                    ? `${snap.summary.stage.slice(0, 48)}…`
                    : snap.summary.stage}
                </span>
              )}
              <QualityDots snap={snap} />
              {candidateCount > 0 && <span>{candidateCount} 家在册</span>}
              {updatedDate && <span className="dos-num">更新于 {updatedDate}</span>}
              {isHistorical && (
                <span className="rounded-full border border-accent/30 bg-accent-soft px-2 py-0.5 text-accent">
                  历史视图 · 截至 {snap.context.as_of.slice(0, 10)}
                </span>
              )}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-1.5 self-start">
            {!offline && (
              <>
                <ResearchPanel snap={snap} onStarted={(sessionId) => {
                  navigate({ page: "sessions" });
                  window.dispatchEvent(new CustomEvent("nav", { detail: "sessions" }));
                  void sessionId;
                }} />
                <ExportMenu snap={snap} />
              </>
            )}
            <button
              onClick={() => switchView(view === "audit" ? "investor" : "audit")}
              className={`rounded border px-2.5 py-1.5 text-meta ${
                view === "audit"
                  ? "border-ink bg-ink text-white"
                  : "border-line bg-white text-ink-mute hover:border-ink-faint"
              }`}
              title={view === "audit"
                ? "切回投资者视图（隐藏快照/命名空间/研究运行时等审计信息）"
                : "切到审计视图（显示快照、模式、配方、模块状态、可信度与限制明细）"}
            >
              {view === "audit" ? "审计视图 ✓" : "审计视图"}
            </button>
          </div>
        </div>

        {/* ---- 审计元数据行（§10：仅 Audit Mode；Investor Mode 0 个运行时字段） ---- */}
        {view === "audit" && (
          <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 rounded-card border border-line bg-white px-3 py-2 dos-num text-meta text-ink-faint">
            <span>as_of {snap.context.as_of.replace("T", " ").slice(0, 19)}Z</span>
            <span>快照 {snap.context.snapshot_id}</span>
            <span>模式 {snap.context.mode}{snap.context.namespace !== "prod" ? ` · ${snap.context.namespace}` : ""}</span>
            <span>配方 {snap.recipe.id}@{snap.recipe.version}</span>
            {!offline && (
              <>
                <label className="flex items-center gap-1 text-ink-mute">
                  知识截止
                  <input
                    type="date"
                    value={(params.as_of ?? "").slice(0, 10)}
                    onChange={(e) => {
                      const v = e.target.value;
                      navigate(withParams(routeOf(), {
                        as_of: v ? `${v}T23:59:59+00:00` : null,
                        snapshot: null, // 切换截止时点 = 打开新快照（时间锁整页生效）
                      }));
                    }}
                    className="rounded border border-line px-1.5 py-0.5 dos-num"
                  />
                </label>
                <button
                  onClick={() => {
                    navigate(withParams(routeOf(), { snapshot: null }));
                    setReloadTick((t) => t + 1);
                  }}
                  className="text-accent hover:underline"
                >
                  刷新（检查新快照）
                </button>
                <a href={`/api/knowledge/${snap.entity.kind}/${snap.entity.id}/archives/latest.html`}
                   target="_blank" rel="noreferrer" className="hover:underline">
                  旧 HTML 存档 ↗
                </a>
              </>
            )}
            {offline && <span>离线导出：冻结快照（时间旅行/补研/试算需在线版）</span>}
          </div>
        )}

        {newer && (
          <div className="mt-3 flex flex-wrap items-center gap-2 rounded-card border border-pos/30 bg-pos-soft px-3 py-2 text-meta text-pos">
            <span>
              本实体已有新版本快照{newer.modules.length > 0 && <>（变化模块：{newer.modules.join("、")}）</>}
              ——当前页仍为钉住的旧快照，不会自动混换。
            </span>
            <button
              onClick={() => {
                navigate(withParams(routeOf(), { snapshot: null }));
                setNewer(null);
                setReloadTick((t) => t + 1);
              }}
              className="rounded border border-pos/40 bg-white px-2 py-0.5 hover:border-pos"
            >
              整体切换到新版本
            </button>
          </div>
        )}
        {isHistorical && view === "audit" && (
          <div className="mt-3 rounded-card border border-accent/30 bg-accent-soft px-3 py-2 text-meta text-accent">
            历史视图：正文、图表、来源与质量全部截至 {snap.context.as_of.slice(0, 10)}——
            不借用未来裁决与资料。补研将「以此为基线，研究当前情况」，不回写过去。
            {snap.context.mode === "rebuilt" && "（基于历史证据重建：分析生成于今天）"}
          </div>
        )}
      </header>

      {/* ---- 章节导航 + 模块正文：阅读区（L0-L2）与审计区（L3）分组 ---- */}
      <div className="flex flex-col gap-5 lg:flex-row">
        <nav className="shrink-0 lg:w-44" aria-label="章节导航">
          <div className="flex gap-1 overflow-x-auto lg:sticky lg:top-16 lg:flex-col lg:overflow-visible">
            {view === "audit" && (
              <div className="hidden px-2.5 pb-1 text-[11px] font-semibold uppercase tracking-wider text-ink-faint lg:block">
                阅读
              </div>
            )}
            {readSections.map((m) => {
              const st = snap.modules[m];
              if (!st) return null;
              const bad = st.status !== "ready";
              return (
                <button
                  key={m}
                  onClick={() => navigate(withParams(routeOf(), { section: m, evidence: null }))}
                  className={`flex items-center justify-between gap-2 whitespace-nowrap rounded-md px-2.5 py-2 text-left text-[13px] lg:whitespace-normal ${
                    section === m ? "bg-ink font-medium text-white" : "text-ink-soft hover:bg-line/50"
                  }`}
                >
                  <span>{SECTION_LABEL[m] ?? st.title ?? m}</span>
                  {view === "audit"
                    ? <ModuleStateBadge status={st.status} reasons={st.reasons} />
                    : bad && (
                      <span
                        className={`h-1.5 w-1.5 rounded-full ${
                          st.status === "conflicted" ? "bg-risk" : st.status === "missing" ? "bg-ink-faint" : "bg-warn"
                        }`}
                        title={`${st.status}${st.reasons?.length ? `：${st.reasons.join("；")}` : ""}`}
                      />
                    )}
                </button>
              );
            })}
            {auditSections.length > 0 && (
              <>
                <div className="hidden px-2.5 pb-1 pt-3 text-[11px] font-semibold uppercase tracking-wider text-ink-faint lg:block">
                  审计
                </div>
                <div className="my-1 hidden border-t border-line lg:block" />
                {auditSections.map((m) => {
                  const st = snap.modules[m];
                  if (!st) return null;
                  return (
                    <button
                      key={m}
                      onClick={() => navigate(withParams(routeOf(), { section: m, evidence: null }))}
                      className={`flex items-center justify-between gap-2 whitespace-nowrap rounded-md px-2.5 py-2 text-left text-[13px] lg:whitespace-normal ${
                        section === m ? "bg-ink font-medium text-white" : "text-ink-mute hover:bg-line/50"
                      }`}
                    >
                      <span>{SECTION_LABEL[m] ?? st.title ?? m}</span>
                      {view === "audit" && <ModuleStateBadge status={st.status} reasons={st.reasons} />}
                    </button>
                  );
                })}
              </>
            )}
          </div>
        </nav>

        <main className="min-w-0 flex-1">
          {/* Overview（投资观点）：撕页 + 论点对象；其余章节：对应模块渲染器 */}
          {isOverview ? (
            <div className="space-y-8">
              <OverviewPage
                snap={snap}
                citeRefs={[]}
                onCite={onEvidenceClick}
                onViewAllCompanies={
                  sections.includes("candidate_pool")
                    ? () => navigate(withParams(routeOf(), { section: "candidate_pool", evidence: null }))
                    : undefined
                }
              />
              {/* 论点对象区（§13/§14）：claims 从 investment_snapshot 模块懒加载 */}
              <section>
                <div className="mb-3 border-b border-line pb-1.5">
                  <span className="dos-kicker">Theses · 论点与证据平衡</span>
                </div>
                {moduleBusy === section && !payload && !moduleErrors[section] && (
                  <div className="py-6 text-center text-meta text-ink-faint">论点加载中…</div>
                )}
                {moduleErrors[section] && (
                  <ModuleLoadError section={section} message={moduleErrors[section]} onRetry={() => {
                    setModuleErrors((m) => { const n = { ...m }; delete n[section]; return n; });
                    setReloadTick((t) => t + 1);
                  }} />
                )}
                {payload && (
                  <ThesisList
                    claims={(payload.payload.claims ?? []) as ClaimItem[]}
                    theses={(payload.payload.theses ?? undefined) as ThesisObject[] | undefined}
                    onCite={onEvidenceClick}
                  />
                )}
              </section>
              {snap.decision_refs.length > 0 && (
                <div className="rounded-card border border-line bg-paper px-4 py-2.5 text-meta text-ink-mute">
                  关联决策：{snap.decision_refs.join("、")}（买卖评级/仓位归 Decisions 页，研究更新不改写旧卡）
                  {!offline && (
                    <button onClick={() => navigate({ page: "decisions" })} className="ml-2 text-accent hover:underline">查看 →</button>
                  )}
                </div>
              )}
              {/* 审计视图：研究目标/问题进展/可信度分层/限制清单（§10：默认不进投资者首屏） */}
              {view === "audit" && <AuditSummaryPanel snap={snap} />}
            </div>
          ) : (
            <section>
              <div className="mb-3 flex flex-wrap items-center gap-2 border-b border-line pb-2">
                <h2 className="text-[20px] font-semibold tracking-tight text-ink">
                  {SECTION_LABEL[section] ?? moduleState?.title ?? section}
                </h2>
                {view === "audit" && moduleState && (
                  <ModuleStateBadge status={moduleState.status} reasons={moduleState.reasons} />
                )}
              </div>
              {view === "audit" && moduleState && (
                <ModuleReasons reasons={moduleState.reasons} gapRefs={moduleState.gap_refs} />
              )}
              <div className="mt-3">
                {moduleBusy === section && !payload && !moduleErrors[section] && (
                  <div className="py-8 text-center text-meta text-ink-faint">模块加载中…</div>
                )}
                {moduleErrors[section] && (
                  <ModuleLoadError section={section} message={moduleErrors[section]} onRetry={() => {
                    setModuleErrors((m) => { const n = { ...m }; delete n[section]; return n; });
                    setReloadTick((t) => t + 1);
                  }} />
                )}
                {payload && ModuleComp && (
                  <ModuleComp
                    snap={snap}
                    payload={payload}
                    onEvidenceClick={onEvidenceClick}
                    onOpenArtifact={onOpenArtifact}
                    params={params}
                    view={view}
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
                <div className="mt-4 rounded-card border border-line bg-paper p-4 text-meta text-ink-mute">
                  <div className="mb-1 font-semibold text-ink-soft">投影层限制（快照级）</div>
                  {snap.limitations.map((l, i) => <div key={i}>· {l}</div>)}
                </div>
              )}
            </section>
          )}
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

/** 模块加载失败独立错误态（review #29）：可重试，不把空 payload 交给正常组件。 */
function ModuleLoadError({ section, message, onRetry }: {
  section: string; message: string; onRetry: () => void;
}) {
  return (
    <div className="rounded-card border border-risk/40 bg-risk-soft p-4 text-meta text-risk">
      <div className="mb-1 font-semibold">模块读取失败（{section}）</div>
      <div className="mb-2">{message}</div>
      <button onClick={onRetry}
              className="rounded border border-risk/40 bg-white px-2 py-1 hover:bg-risk-soft">
        重试
      </button>
    </div>
  );
}

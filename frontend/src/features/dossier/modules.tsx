// 十模块渲染器（设计 §4.4）：每章回答一个主要问题；主图 + 短解释 + 可展开
// 数据/原文/假设；缺失降级给原因（不以空图宣称完成）。
// 页面组件由固定 registry 选择——模型不能注入组件名之外的执行逻辑。

import { useEffect, useRef, useState } from "react";

import { navigate } from "../../app/route";
import { dossierApi, isOfflineExport } from "./api";
import {
  MetricChart, MetricSmallMultiples, NATURE_STYLE, PriceVsRevisionChart, SeriesTable,
  formatMetricValue,
} from "./charts";
import { CitationChips, RawRefChips } from "./citations";
import { ConflictResolver, FactValue } from "./legacy";
import { splitTextRefs, stripRefsDeep, STAGE_LABEL_CN } from "./overview";
import { ThesisList } from "./thesis";
import {
  CandidateQuadrant, LAYER_LABELS, ProfitPoolBar, RankedBars, RELATION_LABELS,
  StageLadder, TierStrip, TimelineStrip, ValueChainGraph, rankedBarColumns,
  type NumericCell,
} from "./viz";
import type {
  BusinessGraph, CandidateItem, ClaimItem, DossierSnapshot, EvidenceItem,
  IndustryMapEdge, IndustryMapNode, LegacyFactItem, MetricSeries, MetricSeriesSet,
  ModulePayload, PricePoint, ProfitPool, QuadrantPayload, RevisionSeries, ValidationItem,
} from "./types";

export interface ModuleProps {
  snap: DossierSnapshot;
  payload: ModulePayload;
  onEvidenceClick: (evidenceId: string) => void;
  onOpenArtifact: (artifactId: string) => void;
  onResolved?: () => void;
  /** 路由参数（audit §4 联动高亮：highlight=<entity_id>） */
  params?: Record<string, string>;
  /** 跳到另一章节（可带参数）：产业链节点 ↔ 公司行联动用 */
  onNavigateSection?: (section: string, params?: Record<string, string | null>) => void;
  /** 投资者/审计视图（§3：审计面显示 raw ref、运行时字段；投资者面只有 [n] 引用） */
  view?: "investor" | "audit";
}

// ---------------- 通用件 ----------------

function Notes({ notes }: { notes?: string[] }) {
  if (!notes?.length) return null;
  return (
    <ul className="mt-2 space-y-0.5 text-meta text-ink-mute">
      {notes.map((n, i) => <li key={i}>· {n}</li>)}
    </ul>
  );
}

function LegacyNeedsNormBadge() {
  return (
    <span className="rounded bg-warn-soft px-1.5 py-0.5 text-[11px] text-warn"
          title="旧文本含数字但单位/期间口径不明——不进图表（不猜数），待补研标准化">
      待标准化
    </span>
  );
}

function LegacyFacts({ items, kind, id, onResolved, readOnly = false, view = "audit" }: {
  items: LegacyFactItem[]; kind: string; id: string; onResolved?: () => void;
  /** 历史/eval 视图只读（review #9）：不得从这里调用生产 v1 裁决入口 */
  readOnly?: boolean;
  /** 投资者视图不显示 raw evidence id（§16：raw ID 只在抽屉/审计模式） */
  view?: "investor" | "audit";
}) {
  if (!items.length) return null;
  return (
    <div className="mt-3 rounded-card border border-line bg-paper/60 p-4">
      <div className="mb-1.5 text-meta font-semibold text-ink-mute">
        旧字段（数据与审计）
        {readOnly && <span className="ml-2 font-normal text-accent">历史/隔离视图只读</span>}
      </div>
      {items.map((f) => (
        <div key={f.field} className="border-b border-line/60 py-2 last:border-0">
          <div className="mb-0.5 flex flex-wrap items-baseline gap-2">
            <span className="font-mono text-meta font-semibold text-ink-soft">{f.field}</span>
            <span className="font-mono text-[11px] text-ink-faint">
              v{f.version} · 可知 {f.knowledge_time.slice(0, 10)}
            </span>
            {f.needs_normalization && <LegacyNeedsNormBadge />}
            {f.conflict && <span className="text-meta text-warn">⚠冲突</span>}
            {view === "audit" && f.evidence_ids.map((e) => (
              <span key={e} className="font-mono text-[11px] text-ink-faint">{e}</span>
            ))}
          </div>
          <div className="text-sm leading-relaxed text-ink">
            <FactValue value={view === "investor" ? stripRefsDeep(f.value) : f.value} />
          </div>
          {f.conflict && (
            <div className="mt-1.5">
              <ConflictResolver kind={kind} id={id} field={f.field} factId={f.fact_id}
                                onResolved={onResolved ?? (() => {})} readOnly={readOnly} />
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function CalculationCard({ calc }: { calc: Record<string, any> }) {
  const [open, setOpen] = useState(false);
  const statusCls = calc.status === "ok" ? "text-pos"
    : calc.status === "not_meaningful" ? "text-ink-mute" : "text-risk";
  return (
    <div className="rounded-card border border-line bg-white p-3 text-sm">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="font-mono text-meta font-semibold text-ink-soft">
          {calc.formula_id}<span className="text-ink-faint">@v{calc.formula_version}</span>
        </span>
        <span className={`font-mono dos-num ${statusCls}`}>
          {calc.status === "ok"
            ? `= ${calc.result ?? "—"}${calc.unit ? ` ${calc.unit}` : ""}`
            : calc.status === "not_meaningful" ? "N/M" : "计算失败"}
        </span>
        <span className="font-mono text-[11px] text-ink-faint">{calc.calculation_id}</span>
        <button onClick={() => setOpen(!open)} className="ml-auto text-meta text-ink-mute hover:underline">
          {open ? "收起" : "输入与假设"}
        </button>
      </div>
      {calc.error && <div className="mt-1 text-meta text-risk">{calc.error}</div>}
      {(calc.warnings ?? []).map((w: string, i: number) => (
        <div key={i} className="mt-1 text-meta text-warn">⚠ {w}</div>
      ))}
      {open && (
        <div className="mt-2 space-y-1 border-t border-neutral-100 pt-2">
          {(calc.input_refs ?? []).map((r: Record<string, any>, i: number) => (
            <div key={i} className="flex gap-2 font-mono text-[10px] text-neutral-600">
              <span className="w-28 shrink-0">{r.label}</span>
              <span className="text-neutral-400">{r.kind}</span>
              <span>{r.value ?? "—"}</span>
              {r.ref_id && <span className="text-blue-700">{r.ref_id}</span>}
            </div>
          ))}
          {Object.entries(calc.assumptions ?? {}).map(([k, v]) => (
            <div key={k} className="flex gap-2 font-mono text-[10px] text-neutral-500">
              <span className="w-28 shrink-0">{k}</span><span>{String(v)}</span>
            </div>
          ))}
          {calc.extra?.cells && <SensitivityGrid extra={calc.extra} />}
        </div>
      )}
    </div>
  );
}

function SensitivityGrid({ extra }: { extra: Record<string, any> }) {
  const cells: Record<string, any>[] = extra.cells ?? [];
  const xs = [...new Set(cells.map((c) => c.x))];
  const ys = [...new Set(cells.map((c) => c.y))];
  return (
    <div className="mt-2 overflow-x-auto">
      <table className="border-collapse text-[10px]">
        <thead>
          <tr>
            <th className="border border-neutral-200 bg-neutral-50 px-1.5 py-0.5 font-mono">
              {extra.vary_y}↓ / {extra.vary_x}→
            </th>
            {xs.map((x) => <th key={x} className="border border-neutral-200 bg-neutral-50 px-1.5 py-0.5 font-mono">{x}</th>)}
          </tr>
        </thead>
        <tbody>
          {ys.map((y) => (
            <tr key={y}>
              <td className="border border-neutral-200 bg-neutral-50 px-1.5 py-0.5 font-mono">{y}</td>
              {xs.map((x) => {
                const cell = cells.find((c) => c.x === x && c.y === y);
                return (
                  <td key={x} className="border border-neutral-200 px-1.5 py-0.5 font-mono tabular-nums"
                      title={cell?.error ?? "继承同一输入来源与模型版本"}>
                    {cell?.ev ?? <span className="text-red-400">N/A</span>}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ClaimsList({ claims, onEvidenceClick }: { claims: ClaimItem[]; onEvidenceClick: (id: string) => void }) {
  // 论点卡（§13/§14）：事实分层 + 支撑/反证计数 + [n] 引用（替代旧 ClaimCard 的 raw ref 墙）
  return <ThesisList claims={claims} onCite={onEvidenceClick} />;
}

// ---------------- 模块 registry ----------------

function InvestmentSnapshotModule({ payload, onEvidenceClick, view }: ModuleProps) {
  // 注意：页面默认将 investment_snapshot 渲染为 OverviewPage 撕页 + ThesisList（见
  // StockDossierPage）；本渲染器是 registry 完整性的兜底（评估行阐只在审计视图显示）。
  const claims = (payload.payload.claims ?? []) as ClaimItem[];
  const assessment = payload.payload.assessment as Record<string, any> | null;
  return (
    <div className="space-y-3">
      {view === "audit" && assessment && (
        <div className="dos-panel text-sm">
          <div className="mb-1 font-semibold text-ink">研究充分度评估（硬门禁由代码运行）</div>
          <div className="flex flex-wrap gap-3 dos-num text-meta text-ink-soft">
            <span>verdict: <b>{assessment.verdict}</b></span>
            <span>问题覆盖: {assessment.question_coverage?.answered}/{assessment.question_coverage?.applicable}</span>
            <span>硬门禁: {assessment.hard_gate_passed ? "✓ 通过" : "✗ 未过"}</span>
            {assessment.stop_reason && <span>stop: {assessment.stop_reason}</span>}
          </div>
          {(assessment.gaps ?? []).length > 0 && (
            <ul className="mt-1.5 space-y-0.5 text-meta text-warn">
              {assessment.gaps.slice(0, 5).map((g: string, i: number) => <li key={i}>· {g}</li>)}
            </ul>
          )}
          {(assessment.notes ?? []).map((n: string, i: number) => (
            <div key={i} className="mt-1 text-meta text-ink-mute">{n}</div>
          ))}
        </div>
      )}
      <ClaimsList claims={claims} onEvidenceClick={onEvidenceClick} />
    </div>
  );
}

const EMPTY_GRAPH: BusinessGraph = { nodes: [], edges: [], narrative: "", narrative_refs: [] };

function BusinessEngineModule({ payload, onEvidenceClick }: ModuleProps) {
  // 防御性默认值（review #29）：加载异常/旧快照缺字段不致页面崩溃
  const graph = (payload.payload.graph ?? EMPTY_GRAPH) as BusinessGraph;
  return (
    <div className="space-y-3">
      {graph.narrative ? (
        <div className="dos-card">
          <div className="mb-1 dos-h">谁付钱，公司怎样赚钱</div>
          <p className="max-w-[76ch] whitespace-pre-wrap text-sm leading-[1.8] text-ink-soft">{graph.narrative}</p>
          <div className="mt-2">
            <CitationChips refs={graph.narrative_refs} onCite={onEvidenceClick} className="ml-0" />
          </div>
        </div>
      ) : (
        <div className="text-sm text-ink-faint">（无业务描述——待补研 business-model 问题）</div>
      )}
      {graph.nodes.length > 0 && (
        <div className="dos-card">
          <div className="mb-2 dos-h">
            业务流<span className="ml-2 text-meta font-normal text-ink-faint">客户 → 产品 → 收费 → 成本 → 现金流</span>
          </div>
          <div className="flex flex-wrap items-center gap-1.5 text-sm">
            {graph.nodes.map((n, i) => (
              <span key={n.node_id} className="flex items-center gap-1.5">
                {i > 0 && <span className="text-ink-faint">→</span>}
                <span className="rounded border border-line bg-paper px-2.5 py-1" title={n.note}>
                  {n.label}
                </span>
              </span>
            ))}
          </div>
          <div className="mt-1 text-meta text-ink-faint">
            流量宽度仅在有带来源数值时展示（不编造 Sankey 宽度）
          </div>
        </div>
      )}
    </div>
  );
}

function RevenueSegmentsModule({ payload }: ModuleProps) {
  const total = payload.payload.total as MetricSeriesSet | undefined;
  const segments = (payload.payload.segments ?? []) as MetricSeries[];
  const notes = (payload.payload.notes ?? []) as string[];
  return (
    <div className="space-y-3">
      {total?.series.length
        ? <MetricChart series={total.series} title="总收入：增长来自哪一块？" />
        : <div className="text-sm text-ink-faint">（无总收入 typed 观测）</div>}
      {segments.length > 0 && (
        <MetricSmallMultiples series={segments} title="分部收入（同口径；合计与总额差异见未分配/抵销说明）" />
      )}
      <Notes notes={notes} />
    </div>
  );
}

function KeyKpiModule({ payload }: ModuleProps) {
  const set = payload.payload.series_set as MetricSeriesSet | undefined;
  const defs = (payload.payload.kpi_definitions ?? []) as Record<string, any>[];
  return (
    <div className="space-y-3">
      {set?.series.length
        ? <MetricSmallMultiples series={set.series} title="什么领先指标决定未来？（按单位分面）" />
        : <div className="text-sm text-ink-faint">（无 KPI typed 观测——未披露项保留缺口，不从文本猜数）</div>}
      <Notes notes={set?.notes} />
      {defs.length > 0 && (
        <details className="dos-panel text-sm">
          <summary className="cursor-pointer font-semibold text-ink-soft">
            KPI 定义与口径（行业配方 {defs.length} 项）
          </summary>
          <table className="mt-2 w-full border-collapse text-meta">
            <thead>
              <tr className="border-b border-line text-left text-ink-faint">
                <th className="px-1.5 py-1">键</th><th className="px-1.5 py-1">名称</th>
                <th className="px-1.5 py-1">必需</th><th className="px-1.5 py-1">定义</th>
              </tr>
            </thead>
            <tbody>
              {defs.map((d) => (
                <tr key={d.key} className="border-b border-line/60">
                  <td className="px-1.5 py-1 font-mono">{d.key}</td>
                  <td className="px-1.5 py-1">{d.label}</td>
                  <td className="px-1.5 py-1">{d.required ? "✓" : "—"}</td>
                  <td className="px-1.5 py-1 text-ink-mute">{d.definition || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </div>
  );
}

function FinancialQualityModule({ snap, payload, onResolved, view = "investor" }: ModuleProps) {
  const [freq, setFreq] = useState<"fy" | "quarterly">("fy");
  const auditReadOnly = snap.context.mode !== "live" || snap.context.namespace !== "prod";
  const set = (freq === "fy" ? payload.payload.fy : payload.payload.quarterly) as MetricSeriesSet | undefined;
  const calcs = (payload.payload.calculations ?? []) as Record<string, any>[];
  const legacy = (payload.payload.legacy ?? []) as LegacyFactItem[];
  const notes = (payload.payload.notes ?? []) as string[];
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <span className="text-sm text-ink-mute">利润是否变成现金？</span>
        <div className="ml-auto flex gap-1">
          {(["fy", "quarterly"] as const).map((f) => (
            <button key={f} onClick={() => setFreq(f)}
                    className={`rounded border px-2.5 py-1 text-meta ${freq === f ? "border-ink bg-ink text-white" : "border-line text-ink-soft"}`}>
              {f === "fy" ? "年度" : "季度"}
            </button>
          ))}
        </div>
      </div>
      {set?.series.length
        ? <MetricSmallMultiples series={set.series} title={freq === "fy" ? "年度：收入 / 利润 / 现金流（按单位分面）" : "季度序列（缺期保留断点）"} />
        : <div className="text-sm text-ink-faint">（无标准化报表观测——旧字段见下方审计区，缺期不补零）</div>}
      <Notes notes={notes} />
      {calcs.length > 0 && (
        <div className="space-y-2">
          <div className="text-meta font-semibold text-ink-mute">计算链（每个值可回指输入与公式版本）</div>
          {calcs.slice(0, 8).map((c) => <CalculationCard key={c.calculation_id} calc={c} />)}
        </div>
      )}
      <LegacyFacts items={legacy} kind={snap.entity.kind} id={snap.entity.id}
                   onResolved={onResolved} readOnly={auditReadOnly} view={view} />
    </div>
  );
}

function ExpectationsModule({ payload }: ModuleProps) {
  const gc = payload.payload.guidance_consensus as MetricSeriesSet | undefined;
  const actuals = payload.payload.actuals as MetricSeriesSet | undefined;
  const deltas = (payload.payload.guidance_delta ?? []) as Record<string, any>[];
  const notes = (payload.payload.notes ?? []) as string[];
  // §26 Price vs EPS Revision：服务端按可知时刻排好的修订序列 + 价格序列；
  // 两腿都 ≥2 点才会出图（组件内部护栏），缺一则如实不进图
  const revision = (payload.payload.revision_series ?? []) as RevisionSeries[];
  const price = (payload.payload.price_series ?? []) as PricePoint[];
  return (
    <div className="space-y-3">
      <PriceVsRevisionChart revision={revision} price={price}
                            title="Price vs EPS Revision（预期上修 or 估值扩张）" />
      {gc?.series.length
        ? (
          // 按指标键分面（EPS/收入/EBITDA 量级悬殊，同轴会压扁小量级序列——
          // 2026-09-12 BE 实测：consensus_eps 2.7-4.9 与 consensus_revenue 4.1B 同轴，
          // EPS 线被压成零线）
          <div className="space-y-2">
            <div className="text-xs font-semibold text-neutral-700">公司表现与预期差在哪里？（指引/一致预期分层，按指标分面）</div>
            <div className="grid gap-2 xl:grid-cols-2">
              {Object.entries(
                gc.series.reduce<Record<string, MetricSeries[]>>((acc, s) => {
                  (acc[s.metric_key] ??= []).push(s);
                  return acc;
                }, {}),
              ).map(([key, list]) => (
                <MetricChart key={key} series={list} title={list[0]?.label ?? key} height={220} />
              ))}
            </div>
          </div>
        )
        : <div className="rounded-card border border-dashed border-line bg-paper p-4 text-sm text-ink-mute">
            预期模块降级：无指引/一致预期观测。没有 consensus 就只比较指引；两者都缺时如实显示能力缺口。
          </div>}
      {actuals?.series.length ? <SeriesTable series={actuals.series} /> : null}
      {deltas.map((c) => <CalculationCard key={c.calculation_id} calc={c} />)}
      <Notes notes={notes} />
    </div>
  );
}

// 假设滑块配置（§8.5：范围与步长由模型配置给出，可同时键盘输入）
const DCF_ASSUMPTION_SPEC: { key: string; label: string; min: number; max: number; step: number; def: string }[] = [
  { key: "wacc", label: "WACC", min: 0.04, max: 0.25, step: 0.005, def: "0.10" },
  { key: "terminal_g", label: "终值增速", min: 0, max: 0.05, step: 0.0025, def: "0.025" },
  { key: "ebit_margin", label: "EBIT 利润率", min: -0.2, max: 0.6, step: 0.01, def: "0.10" },
  { key: "tax_rate", label: "税率", min: 0, max: 0.35, step: 0.01, def: "0.21" },
  { key: "capex_ratio", label: "Capex/收入", min: 0, max: 0.3, step: 0.005, def: "0.04" },
  { key: "da_ratio", label: "D&A/收入", min: 0, max: 0.3, step: 0.005, def: "0.03" },
  { key: "nwc_ratio", label: "ΔNWC/Δ收入", min: 0, max: 0.5, step: 0.01, def: "0.05" },
];

function ReverseDcfPanel({ snap }: { snap: DossierSnapshot }) {
  const [revenue0, setRevenue0] = useState("");
  const [revenueRef, setRevenueRef] = useState("");
  const [targetEv, setTargetEv] = useState("");
  const [years, setYears] = useState("10");
  const [assumptions, setAssumptions] = useState<Record<string, string>>(
    Object.fromEntries(DCF_ASSUMPTION_SPEC.map((a) => [a.key, a.def])),
  );
  const [result, setResult] = useState<Record<string, any> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // 离线导出（§11.4）：试算/保存情景是服务器计算与写入——离线冻结展示，不做新计算
  const offline = isOfflineExport();
  const [saved, setSaved] = useState<{ artifact_id: string; name: string } | null>(null);
  const reqSeq = useRef(0);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const inputsReady = (revenue0.trim() !== "" || revenueRef.trim() !== "") && targetEv.trim() !== "";

  /** 任何输入/假设变化 → 立即作废旧结果与在飞请求（review #28）：
   *  等待/计算期间不得拿旧结果去保存（旧 assumption_hash 与当前表单已不一致）。 */
  const invalidate = () => {
    reqSeq.current += 1;          // 在飞响应全部过期
    if (debounceRef.current) clearTimeout(debounceRef.current);
    setResult(null);
    setError(null);
    setSaved(null);
    setBusy(false);
  };

  // 200ms debounce → 后端确定性计算；请求序号取消过期响应（慢响应不覆盖新值，§8.5）
  useEffect(() => {
    if (offline) return; // 离线导出：不发计算请求
    if (!inputsReady) {
      // 清空必填输入 → 结果一并清除，不得残留可保存的旧值（review #28）
      reqSeq.current += 1;
      setResult(null);
      setBusy(false);
      return;
    }
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      const seq = ++reqSeq.current;
      setBusy(true);
      setError(null);
      dossierApi.valuationPreview({
        entity_kind: snap.entity.kind,
        entity_id: snap.entity.id,
        formula_id: "reverse_dcf",
        // net_debt 未知不默认零：不附带 → 服务端不输出 Equity_model（带警示）
        inputs: [
          revenueRef.trim()
            ? { kind: "observation", label: "revenue_0", ref_id: revenueRef.trim() }
            : { kind: "assumption", label: "revenue_0", value: revenue0.trim() },
        ],
        assumptions: {
          ...assumptions, years,
          target_ev: targetEv.trim(),
          unit: "USD", currency: "USD",
        },
      }).then((r) => {
        if (seq !== reqSeq.current) return; // 过期响应丢弃
        setResult(r);
        setBusy(false);
      }).catch((e) => {
        if (seq !== reqSeq.current) return;
        setError(e instanceof Error ? e.message : String(e));
        setResult(null);
        setBusy(false);
      });
    }, 200);
    return () => { if (debounceRef.current) clearTimeout(debounceRef.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revenue0, revenueRef, targetEv, years, JSON.stringify(assumptions)]);

  const saveScenario = async () => {
    if (!result || result.status !== "ok") return;
    try {
      const r = await dossierApi.saveScenario({
        base_snapshot: snap.context.snapshot_id,
        model_version: `reverse_dcf@v${result.formula_version}`,
        assumption_hash: result.input_hash,
        validated_calculation_id: result.calculation_id,
        name: `${snap.entity.id} 隐含增长情景 g=${(Number(result.result) * 100).toFixed(1)}%`,
        idempotency_key: `${snap.context.snapshot_id}-${result.input_hash}`,
      });
      setSaved({ artifact_id: r.artifact_id, name: r.name });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const num = (v: string) => Number(v);
  return (
    <div className="dos-card">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h4 className="dos-h">研究假设实验：Reverse DCF（FCFF 模型）</h4>
        <span className="rounded bg-warn-soft px-1.5 py-0.5 text-meta text-warn">
          预览计算——滑动不写事实；保存后是模型 artifact，不是披露事实或建议
        </span>
        {offline && (
          <span className="rounded bg-paper px-1.5 py-0.5 text-meta text-ink-mute">
            离线导出：试算与情景保存需在线版（冻结文件不做新计算）
          </span>
        )}
      </div>
      <fieldset disabled={offline} className={offline ? "opacity-60" : ""}>
      <div className="grid gap-3 text-sm md:grid-cols-2">
        <div className="space-y-2">
          <label className="block">
            <span className="mb-0.5 block text-meta text-ink-mute">基期收入 revenue_0（十进制字符串，或填观测 ref）</span>
            <div className="flex gap-1.5">
              <input value={revenue0} onChange={(e) => { invalidate(); setRevenue0(e.target.value); setRevenueRef(""); }}
                     placeholder="如 1500000000" className="w-1/2 rounded border border-line px-2 py-1 font-mono text-meta" />
              <input value={revenueRef} onChange={(e) => { invalidate(); setRevenueRef(e.target.value); setRevenue0(""); }}
                     placeholder="obs-…（引用观测，服务端解析）" className="w-1/2 rounded border border-line px-2 py-1 font-mono text-meta" />
            </div>
          </label>
          <label className="block">
            <span className="mb-0.5 block text-meta text-ink-mute">目标 EV（经调整的市场企业价值）</span>
            <input value={targetEv} onChange={(e) => { invalidate(); setTargetEv(e.target.value); }}
                   placeholder="如 4000000000" className="w-full rounded border border-line px-2 py-1 font-mono text-meta" />
          </label>
          <label className="block">
            <span className="mb-0.5 block text-meta text-ink-mute">预测年数</span>
            <input value={years} onChange={(e) => { invalidate(); setYears(e.target.value); }}
                   className="w-24 rounded border border-line px-2 py-1 font-mono text-meta" />
          </label>
          <button
            onClick={() => {
              invalidate();
              setAssumptions(Object.fromEntries(DCF_ASSUMPTION_SPEC.map((a) => [a.key, a.def])));
              setYears("10");
            }}
            className="rounded border border-line px-2 py-0.5 text-meta text-ink-mute hover:border-ink-faint"
          >
            重置假设（恢复默认值）
          </button>
          <div className="text-meta text-ink-faint">
            净债务未知不默认零：未提供时不输出 Equity_model。买卖评级/目标价区间归 /decide。
          </div>
        </div>
        <div className="space-y-1.5">
          {DCF_ASSUMPTION_SPEC.map((a) => (
            <label key={a.key} className="flex items-center gap-2">
              <span className="w-24 shrink-0 text-meta text-ink-soft">{a.label}</span>
              <input type="range" min={a.min} max={a.max} step={a.step} value={num(assumptions[a.key])}
                     onChange={(e) => { invalidate(); setAssumptions((s) => ({ ...s, [a.key]: e.target.value })); }}
                     className="flex-1 accent-neutral-800" aria-label={a.label} />
              <input value={assumptions[a.key]}
                     onChange={(e) => { invalidate(); setAssumptions((s) => ({ ...s, [a.key]: e.target.value })); }}
                     className="w-16 rounded border border-line px-1 py-0.5 text-right font-mono dos-num text-meta" />
            </label>
          ))}
        </div>
      </div>
      </fieldset>
      <div className="mt-3 border-t border-line pt-3">
        {busy && <div className="text-sm text-ink-faint">计算中（后端确定性公式，非前端估算）…</div>}
        {error && <div className="text-sm text-risk">{error}</div>}
        {result && result.status === "ok" && (
          <div className="flex flex-wrap items-center gap-3">
            <div className="font-mono dos-num text-2xl font-semibold text-ink">
              隐含 g = {(Number(result.result) * 100).toFixed(2)}%
            </div>
            <div className="text-meta text-ink-faint">
              <div>calculation {result.calculation_id} · assumption_hash {String(result.input_hash).slice(0, 20)}…</div>
              <div>求解区间 {JSON.stringify(result.extra?.solve_interval)} · 残差 {String(result.extra?.residual ?? "").slice(0, 12)}</div>
            </div>
            <button onClick={saveScenario}
                    className="ml-auto rounded border border-line px-2.5 py-1 text-meta font-semibold text-ink-soft hover:border-ink-faint">
              保存情景（建立模型 artifact）
            </button>
          </div>
        )}
        {result && result.status !== "ok" && (
          <div className="text-sm text-warn">
            {result.status === "not_meaningful" ? "N/M" : "计算失败"}：{result.error}
            {(result.warnings ?? []).map((w: string, i: number) => <div key={i}>⚠ {w}</div>)}
          </div>
        )}
        {result !== null && result.status === "ok" && (result.warnings ?? []).length > 0 && (
          <div className="mt-1 text-meta text-warn">
            {result.warnings.map((w: string, i: number) => <div key={i}>⚠ {w}</div>)}
          </div>
        )}
        {saved && (
          <div className="mt-2 rounded border border-pos/40 bg-pos-soft px-2 py-1.5 text-meta text-pos">
            已保存情景 {saved.name}（不改变发布快照）
            <button onClick={() => navigate({ page: "research", artifactId: saved.artifact_id, params: {} })}
                    className="ml-2 text-accent hover:underline">查看 →</button>
            <button onClick={() => setSaved(null)} className="ml-2 text-ink-mute hover:underline">继续实验</button>
          </div>
        )}
      </div>
    </div>
  );
}

function ValuationLabModule({ snap, payload, onResolved, view = "investor" }: ModuleProps) {
  const auditReadOnly = snap.context.mode !== "live" || snap.context.namespace !== "prod";
  const calcs = (payload.payload.calculations ?? []) as Record<string, any>[];
  const legacy = (payload.payload.legacy ?? []) as LegacyFactItem[];
  const notes = (payload.payload.notes ?? []) as string[];
  const dcf = calcs.find((c) => c.formula_id === "reverse_dcf");
  const dcfDisabled = snap.modules.valuation_lab?.status === "not_applicable";
  return (
    <div className="space-y-3">
      {dcf && dcf.status === "ok" && (
        <div className="dos-card">
          <div className="text-meta font-semibold text-ink-mute">价格要求怎样的经营表现？（反向求解）</div>
          <div className="mt-1 font-mono dos-num text-3xl font-semibold text-ink">
            g = {(Number(dcf.result) * 100).toFixed(1)}%
            <span className="ml-2 text-sm font-normal text-ink-faint">隐含收入增速（在下列假设下）</span>
          </div>
          <div className="mt-1 text-meta text-ink-mute">
            这是「在这些条件下价格隐含的增长率」，不是从股价唯一反推增长/利润率/倍数三项。
          </div>
        </div>
      )}
      {!dcfDisabled && <ReverseDcfPanel snap={snap} />}
      {dcfDisabled && (
        <div className="dos-panel text-sm text-ink-mute">
          行业配方禁用通用 EV/FCFF 模型（未盈利/现金流不可建模）——不硬套，见配方说明。
        </div>
      )}
      {calcs.length
        ? calcs.map((c) => <CalculationCard key={c.calculation_id} calc={c} />)
        : !dcfDisabled && (
          <div className="text-meta text-ink-faint">
            尚无已登记的正式估值计算（上方实验为预览；保存后成为模型 artifact）。
          </div>
        )}
      <Notes notes={notes} />
      <LegacyFacts items={legacy} kind={snap.entity.kind} id={snap.entity.id}
                   onResolved={onResolved} readOnly={auditReadOnly} view={view} />
    </div>
  );
}

function PeersModule({ snap, payload, onResolved, view = "investor" }: ModuleProps) {
  const auditReadOnly = snap.context.mode !== "live" || snap.context.namespace !== "prod";
  const legacy = (payload.payload.legacy ?? []) as LegacyFactItem[];
  const peerSeries = (payload.payload.peer_series ?? []) as Record<string, any>[];
  const notes = (payload.payload.notes ?? []) as string[];
  return (
    <div className="space-y-3">
      {peerSeries.length > 0 ? (
        <div className="overflow-x-auto rounded-card border border-line bg-white">
          <table className="w-full border-collapse text-[13px]">
            <thead>
              <tr className="border-b border-line bg-paper text-left text-meta text-ink-mute">
                <th className="px-2.5 py-2 font-medium">实体</th><th className="px-2.5 py-2 font-medium">指标</th>
                <th className="px-2.5 py-2 font-medium">值</th><th className="px-2.5 py-2 font-medium">期间</th>
              </tr>
            </thead>
            <tbody>
              {peerSeries.flatMap((p) => (p.metrics ?? []).map((m: Record<string, any>, i: number) => (
                <tr key={`${p.entity_id}-${i}`} className="border-b border-line/60">
                  <td className="px-2.5 py-2 font-mono">{p.entity_id}</td>
                  <td className="px-2.5 py-2 font-mono text-ink-mute">{m.metric_key}</td>
                  <td className="px-2.5 py-2 font-mono dos-num">{formatMetricValue(m.value, "", null)}</td>
                  <td className="px-2.5 py-2 font-mono dos-num text-meta text-ink-faint">{m.period_label}</td>
                </tr>
              )))}
            </tbody>
          </table>
        </div>
      ) : null}
      <Notes notes={notes} />
      <LegacyFacts items={legacy} kind={snap.entity.kind} id={snap.entity.id}
                   onResolved={onResolved} readOnly={auditReadOnly} view={view} />
    </div>
  );
}

// ---------------- 行业模块（audit §3.6/§3.7） ----------------
// 层级/关系标签已迁至 viz.tsx（LAYER_LABELS 含 demand，RELATION_LABELS 含 value_flow；
// 未知关系保留原文显示，不硬译）

/** 引用 chips（§16/§48.3）：投资者视图只给 [n] 编号；审计视图给 raw ref。
 *  raw evidence ID 是数据库主键，不是阅读界面元素。 */
function EvidenceChips({ refs, onEvidenceClick, view = "investor" }: {
  refs?: string[]; onEvidenceClick: (id: string) => void; view?: "investor" | "audit";
}) {
  if (view === "audit") return <RawRefChips refs={refs} onCite={onEvidenceClick} />;
  return <CitationChips refs={refs} onCite={onEvidenceClick} className="ml-0" />;
}

function IndustryChainModule({ payload, onEvidenceClick, params, onNavigateSection, view = "investor" }: ModuleProps) {
  const graph = (payload.payload.graph ?? EMPTY_GRAPH) as BusinessGraph;
  const nodes = (payload.payload.nodes ?? graph.nodes ?? []) as IndustryMapNode[];
  const edges = (payload.payload.edges ?? graph.edges ?? []) as IndustryMapEdge[];
  const layers = (payload.payload.layers ?? graph.layers ?? []) as string[];
  const layerLabels = (payload.payload.layer_labels ?? graph.layer_labels ?? {}) as Record<string, string>;
  const valueFlowNote = (payload.payload.value_flow_note ?? graph.value_flow_note ?? "") as string;
  const routes = (payload.payload.routes ?? graph.routes ?? []) as Record<string, string>[];
  const bottlenecks = (payload.payload.bottlenecks ?? graph.bottlenecks ?? []) as string[];
  const notes = (payload.payload.notes ?? []) as string[];
  const limitations = (payload.payload.limitations ?? []) as string[];
  // 联动高亮（audit §4）：从公司行跳回来时 highlight=<entity_id> → 高亮包含该公司的节点；
  // 点节点则高亮它的上下游边（本地状态，不污染路由）
  const highlightCompany = params?.highlight ?? "";
  const [activeNode, setActiveNode] = useState<string | null>(null);
  // 瓶颈红标只在可分辨时（0<瓶颈数<节点总数）；全标=未标，红失效为噪音
  const bnCount = nodes.filter((n) => n.bottleneck).length;
  const bnDiscriminate = bnCount > 0 && bnCount < nodes.length;
  const companyNodes = new Set(
    nodes.filter((n) => (n.company_refs ?? []).some((c) => c.toUpperCase() === highlightCompany.toUpperCase()))
          .map((n) => n.node_id),
  );
  const edgeLit = (e: IndustryMapEdge) =>
    !activeNode || e.source === activeNode || e.target === activeNode;
  // 分层渲染：有 layers 按其顺序，否则按节点出现顺序（不猜层级）
  const ordered = layers.length
    ? layers
    : Array.from(new Set(nodes.map((n) => n.layer).filter(Boolean)));
  const byLayer = new Map<string, IndustryMapNode[]>();
  for (const n of nodes) {
    const key = n.layer || "other";
    byLayer.set(key, [...(byLayer.get(key) ?? []), n]);
  }
  const label = (id: string) => nodes.find((n) => n.node_id === id)?.label ?? id;
  return (
    <div className="space-y-3">
      {nodes.length > 0 ? (
        <div className="dos-card">
          <div className="mb-3 dos-h">
            产业链分层流图
            <span className="ml-2 text-meta font-normal text-ink-faint">节点带证据，边带关系标签；点击节点聚焦</span>
          </div>
          <ValueChainGraph
            nodes={nodes} edges={edges} layers={ordered} layerLabels={layerLabels}
            activeNode={activeNode}
            onNodeClick={(id) => setActiveNode(id || null)}
            onCompanyClick={(c) => onNavigateSection?.("candidate_pool", { highlight: c })}
            onEvidenceClick={onEvidenceClick}
            highlightCompany={highlightCompany}
          />
          {bottlenecks.length > 0 && (
            <div className="mt-2 text-meta text-risk">
              瓶颈环节：
              {view === "investor"
                // 投资者视图：剥离内嵌 raw ID（§16），引用进 [n] 芯片不丢链
                ? bottlenecks.map((b, i) => {
                  const { clean, refs } = splitTextRefs(label(b));
                  return (
                    <span key={i}>
                      {i > 0 && "、"}{clean}
                      <CitationChips refs={refs} onCite={onEvidenceClick} className="ml-1" />
                    </span>
                  );
                })
                : bottlenecks.map(label).join("、")}
            </div>
          )}
          <details className="mt-2">
            <summary className="cursor-pointer text-meta font-semibold text-ink-mute">
              节点与关系数据表（流图的键盘可达等价物，含全部公司/证据引用）
            </summary>
          <div className="mt-2 flex flex-col gap-2 md:flex-row md:items-stretch md:gap-3">
            {(ordered.length ? ordered : Array.from(byLayer.keys())).map((layer) => (
              <div key={layer} className="min-w-0 flex-1 rounded border border-line bg-paper/60 p-2">
                <div className="mb-1.5 text-meta font-semibold text-ink-mute">
                  {LAYER_LABELS[layer] ?? layer}
                </div>
                <div className="space-y-1.5">
                  {(byLayer.get(layer) ?? []).map((n) => (
                    <div key={n.node_id}
                         onClick={() => setActiveNode(activeNode === n.node_id ? null : n.node_id)}
                         role="button"
                         tabIndex={0}
                         onKeyDown={(e) => { if (e.key === "Enter") setActiveNode(n.node_id); }}
                         title={activeNode === n.node_id ? "再点一次取消高亮关联边"
                           : "点击高亮该节点的上下游关系"}
                         className={`cursor-pointer rounded border px-2 py-1.5 text-[13px] ${
                           companyNodes.has(n.node_id)
                             ? "border-accent/50 bg-accent-soft ring-1 ring-accent/30"
                             : bnDiscriminate && n.bottleneck ? "border-risk/40 bg-risk-soft/60" : "border-line bg-white"
                         } ${activeNode === n.node_id ? "ring-2 ring-ink" : ""}`}>
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="font-medium text-ink">{n.label}</span>
                        {bnDiscriminate && n.bottleneck && (
                          <span className="rounded bg-risk px-1 py-0.5 text-[11px] font-semibold text-white">瓶颈</span>
                        )}
                        {companyNodes.has(n.node_id) && (
                          <span className="rounded bg-accent px-1 py-0.5 text-[11px] text-white">已定位</span>
                        )}
                      </div>
                      {n.note && <div className="mt-0.5 text-meta text-ink-mute">{n.note}</div>}
                      {(n.company_refs ?? []).length > 0 && (
                        <div className="mt-1 flex flex-wrap gap-1">
                          {n.company_refs.map((c) => (
                            <button
                              key={c}
                              onClick={(e) => {
                                e.stopPropagation();
                                // 公司芯片 → 候选矩阵同一行（联动高亮，不丢快照上下文）
                                onNavigateSection?.("candidate_pool", { highlight: c });
                              }}
                              title="在「公司」章节里定位这一行"
                              className="rounded border border-line px-1 py-0.5 font-mono text-[11px] text-ink-soft hover:border-accent hover:bg-accent-soft hover:text-accent"
                            >
                              {c}
                            </button>
                          ))}
                        </div>
                      )}
                      <div className="mt-1"><EvidenceChips refs={n.evidence_refs} onEvidenceClick={onEvidenceClick} view={view} /></div>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
          {edges.length > 0 && (
            <ul className="mt-3 space-y-1 border-t border-line pt-2 text-meta text-ink-soft">
              {edges.map((e, i) => (
                <li key={i} className={`flex flex-wrap items-center gap-1.5 ${
                  edgeLit(e) ? "" : "opacity-30"
                }`}>
                  <span className="font-mono">{label(e.source)} → {label(e.target)}</span>
                  <span className="rounded bg-paper px-1 py-0.5 text-[11px] text-ink-soft">
                    {RELATION_LABELS[e.relation] ?? e.relation}
                  </span>
                  {!e.flow_known && (
                    <span className="text-[11px] text-ink-faint" title="流量/份额未知：等宽边，不估算">
                      流量未知
                    </span>
                  )}
                  {e.note && <span className="text-ink-mute">{e.note}</span>}
                  <EvidenceChips refs={e.evidence_refs} onEvidenceClick={onEvidenceClick} view={view} />
                </li>
              ))}
            </ul>
          )}
          </details>
        </div>
      ) : (
        <div className="dos-card border-dashed text-sm text-ink-mute">
          尚无结构化产业链图（nodes/edges）——不拿旧字段文本冒充关系图。
        </div>
      )}
      {/* Profit Pool（§22）：value_flow 边有定量份额时画堆叠条；单一来源
          标 Estimated（§48.4）；无定量证据时维持下方定性文本（不伪造精确图） */}
      {(() => {
        const pool = (payload.payload.profit_pool ?? null) as ProfitPool | null;
        return pool && pool.entries.length > 0
          ? <ProfitPoolBar pool={pool} onEvidenceClick={onEvidenceClick} />
          : null;
      })()}
      {valueFlowNote && (() => {
        // 文本内嵌的 ev-*/claim-* 转为 [n] 引用芯片（§16：正文不出现 raw ID）
        const { clean, refs } = splitTextRefs(valueFlowNote);
        return (
          <div className="dos-card border-accent/30 bg-accent-soft/50 text-sm leading-[1.8] text-ink">
            <span className="mr-1 font-semibold">价值流/利润池：</span>{clean}
            <CitationChips refs={refs} onCite={onEvidenceClick} />
          </div>
        );
      })()}
      {routes.length > 0 && (
        <div className="overflow-x-auto dos-card">
          <div className="mb-1.5 dos-h">技术路线对比</div>
          <table className="w-full text-[13px]">
            <thead>
              <tr className="border-b border-line text-left text-meta text-ink-mute">
                {Object.keys(routes[0]).map((k) => <th key={k} className="py-1 pr-3 font-medium">{k}</th>)}
              </tr>
            </thead>
            <tbody>
              {routes.map((r, i) => (
                <tr key={i} className="border-b border-line/60 last:border-0">
                  {Object.keys(routes[0]).map((k) => (
                    <td key={k} className="py-1.5 pr-3 align-top text-ink-soft">{r[k]}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {graph.narrative && (
        <details className="dos-panel bg-paper/60">
          <summary className="cursor-pointer text-meta font-semibold text-ink-mute">
            旧字段叙述（兼容区，不是关系图）
          </summary>
          <p className="mt-2 whitespace-pre-wrap text-sm leading-relaxed text-ink-soft">{graph.narrative}</p>
          <div className="mt-2"><EvidenceChips refs={graph.narrative_refs} onEvidenceClick={onEvidenceClick} view={view} /></div>
        </details>
      )}
      {limitations.length > 0 && (
        <ul className="space-y-0.5 text-meta text-warn">
          {limitations.map((l, i) => <li key={i}>· {l}</li>)}
        </ul>
      )}
      <Notes notes={notes} />
    </div>
  );
}

const TIER_LABELS: Record<string, string> = {
  included: "入选", watchlist: "观察", excluded: "淘汰", needs_review: "待核实",
};
const TIER_CLS: Record<string, string> = {
  included: "bg-green-50 text-green-700 border-green-200",
  watchlist: "bg-blue-50 text-blue-700 border-blue-200",
  excluded: "bg-neutral-100 text-neutral-500 border-neutral-200",
  needs_review: "bg-amber-50 text-amber-700 border-amber-200",
};

function CandidatePoolModule({ payload, onEvidenceClick, params, onNavigateSection, view = "investor" }: ModuleProps) {
  const candidates = (payload.payload.candidates ?? []) as CandidateItem[];
  const criteria = (payload.payload.criteria ?? []) as string[];
  const objective = (payload.payload.objective ?? "") as string;
  const stageDefs = (payload.payload.stage_definitions ?? {}) as Record<string, string>;
  // 四象限（§20）：服务端离散序映射；无可定位公司时为 null（不画空图）
  const quadrant = (payload.payload.quadrant ?? null) as QuadrantPayload | null;
  const comparison = (payload.payload.comparison ?? {}) as Record<string, any>;
  const numerics = (payload.payload.comparison_numerics ?? []) as {
    label: string; cells: Record<string, NumericCell>;
  }[];
  const legacy = (payload.payload.legacy ?? []) as LegacyFactItem[];
  const limitations = (payload.payload.limitations ?? []) as string[];
  const notes = (payload.payload.notes ?? []) as string[];
  const [tier, setTier] = useState<string>("all");
  // 密度规则（§9/§19：默认 Top 5-8，点击「查看全部」展开）：按 tier 序（入选→观察→待核实→淘汰）
  const [showAll, setShowAll] = useState(false);
  // 阶段阶梯芯片点击 → 本模块内定位（不污染路由）；从产业链节点跳过来时
  // highlight=<entity_id> 优先：该行高亮 + 自动滚到可见
  const [locate, setLocate] = useState("");
  const highlight = ((params?.highlight ?? "") || locate).toUpperCase();
  const rowRef = useRef<HTMLTableRowElement | null>(null);
  useEffect(() => {
    if (highlight && rowRef.current) {
      rowRef.current.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }, [highlight]);
  const tiers = Array.from(new Set(candidates.map((c) => c.tier)));
  const TIER_RANK: Record<string, number> = { included: 0, watchlist: 1, needs_review: 2, excluded: 3 };
  const tierSorted = [...candidates].map((c, i) => ({ c, i }))
    .sort((a, b) => (TIER_RANK[a.c.tier] ?? 9) - (TIER_RANK[b.c.tier] ?? 9) || a.i - b.i)
    .map((x) => x.c);
  const filtered = tier === "all" ? tierSorted : tierSorted.filter((c) => c.tier === tier);
  // 有过滤/定位时显示全部命中（折叠只在默认全览时生效）
  const collapse = !showAll && tier === "all" && !highlight && filtered.length > 8;
  const shown = collapse ? filtered.slice(0, 8) : filtered;
  const rows = (comparison.rows ?? []) as Record<string, any>[];
  const cols = (comparison.columns ?? []) as Record<string, string>[];
  return (
    <div className="space-y-3">
      {(objective || criteria.length > 0) && view === "audit" && (
        <div className="dos-panel text-sm text-ink-soft">
          {objective && <div className="mb-1"><span className="font-semibold text-ink">筛选目标：</span>{objective}</div>}
          {criteria.length > 0 && (
            <div><span className="font-semibold text-ink">筛选标准：</span>{criteria.join("；")}</div>
          )}
        </div>
      )}
      {candidates.length > 0 && (
        <div className="dos-panel">
          <div className="mb-1.5 text-meta font-semibold text-ink-mute">
            候选分层（条宽 = 数量占比；点击过滤下表）
          </div>
          <TierStrip candidates={candidates} active={tier} onSelect={setTier} />
        </div>
      )}
      {quadrant && quadrant.points.length > 0 && (
        <CandidateQuadrant payload={quadrant} onLocate={setLocate} />
      )}
      <StageLadder candidates={candidates} onLocate={setLocate} />
      {candidates.length > 0 ? (
        <div className="overflow-hidden rounded-card border border-line bg-white">
          {tiers.length > 1 && (
            <div className="flex flex-wrap gap-1.5 border-b border-line p-3">
              {["all", ...tiers].map((t) => (
                <button key={t} onClick={() => setTier(t)}
                        className={`rounded-full border px-2.5 py-1 text-meta ${
                          tier === t ? "border-ink bg-ink text-white" : "border-line text-ink-soft hover:border-ink-faint"
                        }`}>
                  {t === "all" ? `全部 ${candidates.length}` : `${TIER_LABELS[t] ?? t} ${candidates.filter((c) => c.tier === t).length}`}
                </button>
              ))}
            </div>
          )}
          <div className="overflow-x-auto">
            <table className="w-full min-w-[880px] text-[13px]">
              <thead>
                <tr className="border-b border-line bg-paper text-left text-meta text-ink-mute">
                  <th className="px-3 py-2 font-medium">公司</th>
                  <th className="px-3 py-2 font-medium">可投资范围</th>
                  <th className="px-3 py-2 font-medium">技术验证阶段</th>
                  <th className="px-3 py-2 font-medium">商业兜现阶段</th>
                  <th className="px-3 py-2 font-medium">护城河证据</th>
                  <th className="px-3 py-2 font-medium">反证</th>
                  <th className="px-3 py-2 font-medium">结论与下一次验证</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((c) => (
                  <tr key={c.entity_id}
                      ref={c.entity_id.toUpperCase() === highlight ? rowRef : undefined}
                      className={`border-b border-line/60 align-top last:border-0 ${
                        c.entity_id.toUpperCase() === highlight ? "bg-accent-soft/70 ring-1 ring-inset ring-accent/30" : ""
                      }`}>
                    <td className="px-3 py-2.5">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="font-medium text-ink">{c.name || c.entity_id}</span>
                        <span className={`rounded-full border px-1.5 py-0.5 text-[11px] ${TIER_CLS[c.tier] ?? TIER_CLS.needs_review}`}>
                          {TIER_LABELS[c.tier] ?? c.tier}
                        </span>
                      </div>
                      <div className="mt-0.5 font-mono text-meta text-ink-faint">{c.entity_id}</div>
                      {onNavigateSection && (
                        <button
                          onClick={() => onNavigateSection("industry_chain", { highlight: c.entity_id })}
                          title="在产业链图里高亮包含该公司的环节"
                          className="mt-1 rounded border border-line px-1 py-0.5 text-[11px] text-ink-mute hover:border-accent hover:text-accent"
                        >
                          在产业链中定位
                        </button>
                      )}
                      <div className="mt-1"><EvidenceChips refs={c.evidence_refs} onEvidenceClick={onEvidenceClick} view={view} /></div>
                    </td>
                    <td className="px-3 py-2.5 text-ink-soft">
                      {c.listing_status === "listed"
                        ? <>已上市·{c.market || "市场未注明"}</>
                        : c.listing_status === "private" ? "未上市（技术参照）"
                        : c.listing_status === "subsidiary" ? "子公司/关联主体"
                        : "上市状态待核实"}
                      {c.security_relation && (
                        <div className="mt-0.5 text-meta text-ink-faint">
                          {view === "investor" ? splitTextRefs(c.security_relation).clean : c.security_relation}
                        </div>
                      )}
                      {c.investable === false && <div className="mt-0.5 text-meta text-warn">不混入可交易候选</div>}
                    </td>
                    <td className="px-3 py-2.5 text-ink-soft">
                      {c.technology_stage
                        ? (STAGE_LABEL_CN[c.technology_stage] ?? c.technology_stage)
                        : <span className="text-ink-faint">未判定</span>}
                      {stageDefs[c.technology_stage] && (
                        <div className="mt-0.5 text-meta text-ink-faint" title={stageDefs[c.technology_stage]}>
                          {stageDefs[c.technology_stage]}
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-2.5 text-ink-soft">
                      {c.commercial_stage
                        ? (STAGE_LABEL_CN[c.commercial_stage] ?? c.commercial_stage)
                        : <span className="text-ink-faint">未判定</span>}
                      {(c.commercial_evidence ?? []).length > 0 && (
                        <ul className="mt-1 space-y-0.5 text-meta text-ink-mute">
                          {c.commercial_evidence.map((x, i) => (
                            <li key={i}>· {view === "investor" ? splitTextRefs(x).clean : x}</li>
                          ))}
                        </ul>
                      )}
                    </td>
                    <td className="px-3 py-2.5">
                      {(c.moat_evidence ?? []).length
                        ? <ul className="space-y-0.5 text-meta text-ink-soft">
                            {c.moat_evidence.map((x, i) => (
                              <li key={i}>· {view === "investor" ? splitTextRefs(x).clean : x}</li>
                            ))}
                          </ul>
                        : <span className="text-meta text-ink-faint">无一手证据</span>}
                    </td>
                    <td className="px-3 py-2.5">
                      {(c.counter_evidence ?? []).length
                        ? <ul className="space-y-0.5 text-meta text-risk">
                            {c.counter_evidence.map((x, i) => (
                              <li key={i}>· {view === "investor" ? splitTextRefs(x).clean : x}</li>
                            ))}
                          </ul>
                        : <span className="text-meta text-ink-faint">未检索到反证</span>}
                    </td>
                    <td className="px-3 py-2.5 text-ink-soft">
                      {/* 内嵌 raw ID 剥离子（§16）；剥离出的引用并入该行 chips */}
                      {(() => {
                        const reason = splitTextRefs(c.reason || "（未给原因）");
                        const nextV = c.next_validation ? splitTextRefs(c.next_validation) : null;
                        return (
                          <>
                            <div className="text-meta leading-relaxed">
                              {reason.clean}
                              {view === "investor" && (
                                <CitationChips refs={reason.refs} onCite={onEvidenceClick} className="ml-1" />
                              )}
                            </div>
                            {nextV && (
                              <div className="mt-1 text-meta text-accent">
                                下次验证：{nextV.clean}
                                {view === "investor" && (
                                  <CitationChips refs={nextV.refs} onCite={onEvidenceClick} className="ml-1" />
                                )}
                              </div>
                            )}
                          </>
                        );
                      })()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {(collapse || (showAll && tier === "all" && filtered.length > 8)) && (
            <button onClick={() => setShowAll((v) => !v)}
                    className="block w-full border-t border-line bg-paper/60 px-3 py-2 text-center text-meta font-medium text-accent hover:bg-accent-soft">
              {collapse ? `查看全部 ${filtered.length} 家 ↓` : "收起为 Top 8 ↑"}
            </button>
          )}
        </div>
      ) : (
        <div className="dos-card border-dashed text-sm text-ink-mute">
          尚无结构化候选评估（CandidateAssessment）——下方旧字段不能当作筛选结果（入选/淘汰/待核实原因缺失）。
        </div>
      )}
      {rows.length > 0 && cols.length > 0 && (() => {
        // 排序条形（§9 Peer Comparison → Ranked Bar）：数值来自服务端解析的冻结观测，
        // 同列同单位同币种才出图（混币种/不可比 → 只给表，不硬画）
        const barCols = rankedBarColumns(cols, rows, numerics, comparison.chartable === true);
        return barCols.length
          ? <RankedBars cols={barCols} formatValue={formatMetricValue} />
          : null;
      })()}
      {rows.length > 0 && cols.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-neutral-200 bg-white p-3">
          <div className="mb-1.5 text-xs font-semibold text-neutral-500">
            {comparison.title || "同口径对照"}
            {comparison.chartable === false && (
              <span className="ml-2 font-normal text-neutral-400">（存在不可比行：只给表，不绘图）</span>
            )}
          </div>
          <table className="w-full text-[13px]">
            <thead>
              <tr className="border-b border-line text-left text-meta text-ink-mute">
                <th className="py-1.5 pr-3 font-medium">项目</th>
                {cols.map((c) => (
                  <th key={c.id} className="py-1.5 pr-3 font-medium">
                    {c.label}{c.period ? <span className="ml-1 text-ink-faint">{c.period}</span> : null}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i} className="border-b border-line/60 last:border-0">
                  <td className="py-2 pr-3 text-ink">
                    {r.label}
                    {r.comparable === false && (
                      <span className="ml-1 text-meta text-warn" title={r.incomparable_reason}>不可比</span>
                    )}
                  </td>
                  {cols.map((c) => {
                    const rawCell = (r.cells ?? {})[c.id];
                    const cell = typeof rawCell === "string" && view === "investor"
                      ? splitTextRefs(rawCell).clean
                      : rawCell;
                    return (
                      <td key={c.id} className="py-2 pr-3 font-mono dos-num text-ink-soft">
                        {cell ?? <span className="text-line">—</span>}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
          {(comparison.incomparable_reasons ?? []).length > 0 && (
            <ul className="mt-2 space-y-0.5 text-meta text-warn">
              {(comparison.incomparable_reasons as string[]).map((x, i) => <li key={i}>· {x}</li>)}
            </ul>
          )}
        </div>
      )}
      {Object.keys(stageDefs).length > 0 && (
        <details className="rounded-card border border-line bg-paper/60 p-3">
          <summary className="cursor-pointer text-meta font-semibold text-ink-mute">阶段定义（不是评分）</summary>
          <ul className="mt-1.5 space-y-0.5 text-meta text-ink-soft">
            {Object.entries(stageDefs).map(([k, v]) => <li key={k}>· <b>{k}</b>：{v}</li>)}
          </ul>
        </details>
      )}
      {limitations.length > 0 && (
        <ul className="space-y-0.5 text-meta text-warn">
          {limitations.map((l, i) => <li key={i}>· {l}</li>)}
        </ul>
      )}
      <LegacyFacts items={legacy} kind="industry" id={String(payload.snapshot_id ?? "")} readOnly view={view} />
      <Notes notes={notes} />
    </div>
  );
}

function ValidationTimeline({ items, onEvidenceClick, view = "investor" }: {
  items: ValidationItem[]; onEvidenceClick: (id: string) => void; view?: "investor" | "audit";
}) {
  if (!items.length) return null;
  const badge = (status: string) =>
    status === "occurred" ? "bg-pos-soft text-pos border-pos/40"
    : status === "expected" ? "bg-accent-soft text-accent border-accent/40"
    : "bg-paper text-ink-mute border-line";
  const label = (status: string) =>
    status === "occurred" ? "已发生" : status === "expected" ? "预计" : "时间未知";
  return (
    <div className="dos-card">
      <div className="mb-2 dos-h">
        验证时间线
        <span className="ml-2 text-meta font-normal text-ink-faint">展示条件，不给无依据的概率</span>
      </div>
      <TimelineStrip items={items} />
      <ol className="mt-3 space-y-3 border-l border-line pl-4">
        {items.map((it, i) => (
          <li key={i} className="relative">
            <span className="absolute -left-[21px] top-1.5 h-2 w-2 rounded-full bg-ink-faint" />
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-sm font-medium text-ink">{it.event}</span>
              <span className={`rounded-full border px-1.5 py-0.5 text-[11px] ${badge(it.status)}`}>{label(it.status)}</span>
              {(it.window_start || it.window_end) && (
                <span className="font-mono dos-num text-meta text-ink-mute">
                  {it.window_start}{it.window_end ? ` → ${it.window_end}` : ""}
                </span>
              )}
            </div>
            {it.trigger_condition && (
              <div className="mt-0.5 text-meta text-ink-soft">触发条件：{it.trigger_condition}</div>
            )}
            {it.affected_judgment && (
              <div className="mt-0.5 text-meta text-ink-mute">影响判断：{it.affected_judgment}</div>
            )}
            <div className="mt-1 flex flex-wrap items-center gap-2">
              {(it.company_refs ?? []).map((c) => (
                <span key={c} className="rounded border border-line px-1 py-0.5 font-mono text-[11px] text-ink-soft">{c}</span>
              ))}
              <EvidenceChips refs={it.evidence_refs} onEvidenceClick={onEvidenceClick} view={view} />
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}

function CatalystsRisksModule({ snap, payload, onEvidenceClick, onResolved, view = "investor" }: ModuleProps) {
  const auditReadOnly = snap.context.mode !== "live" || snap.context.namespace !== "prod";
  const legacy = (payload.payload.legacy ?? []) as LegacyFactItem[];
  const claims = (payload.payload.claims ?? []) as ClaimItem[];
  const items = (payload.payload.items ?? []) as ValidationItem[];
  const notes = (payload.payload.notes ?? []) as string[];
  const limitations = (payload.payload.limitations ?? []) as string[];
  return (
    <div className="space-y-4">
      <div className="text-sm text-ink-mute">何时验证？什么情况下失效？</div>
      <ValidationTimeline items={items} onEvidenceClick={onEvidenceClick} view={view} />
      <ClaimsList claims={claims} onEvidenceClick={onEvidenceClick} />
      {limitations.length > 0 && (
        <ul className="space-y-0.5 text-meta text-warn">
          {limitations.map((l, i) => <li key={i}>· {l}</li>)}
        </ul>
      )}
      <LegacyFacts items={legacy} kind={snap.entity.kind} id={snap.entity.id}
                   onResolved={onResolved} readOnly={auditReadOnly} view={view} />
      <Notes notes={notes} />
    </div>
  );
}

function ResearchSourcesModule({ snap, payload, onEvidenceClick, onOpenArtifact, onResolved, view = "investor" }: ModuleProps) {
  const artifacts = (payload.payload.artifacts ?? []) as Record<string, any>[];
  const scenarios = (payload.payload.scenarios ?? []) as Record<string, any>[];
  const plans = (payload.payload.plans ?? []) as Record<string, any>[];
  const planNotes = (payload.payload.plan_notes ?? []) as string[];
  const claims = (payload.payload.claims ?? []) as ClaimItem[];
  const evidence = (payload.payload.evidence ?? []) as EvidenceItem[];
  const legacy = (payload.payload.legacy_facts ?? []) as LegacyFactItem[];
  const obsConflicts = (payload.payload.observation_conflicts ?? []) as Record<string, any>[];
  const assessment = payload.payload.assessment as Record<string, any> | null;
  const latestPlan = plans[0];
  // 历史/隔离视图：旧字段裁决入口只读（review #9）
  const auditReadOnly = snap.context.mode !== "live" || snap.context.namespace !== "prod";
  return (
    <div className="space-y-4">
      {artifacts.length > 0 && (
        <section>
          <h4 className="mb-1.5 text-[13px] font-semibold text-ink-soft">研究产物（冻结研报）</h4>
          <div className="space-y-1.5">
            {artifacts.map((a) => (
              <div key={a.artifact_id} className="flex flex-wrap items-center gap-2 rounded border border-line bg-white px-3 py-2 text-xs">
                <button onClick={() => onOpenArtifact(a.artifact_id)} className="font-semibold text-accent hover:underline">
                  {a.title || a.artifact_id}
                </button>
                <span className={`rounded-full border px-1.5 py-0.5 text-[11px] ${
                  a.status === "validated" ? "border-pos/40 text-pos" : "border-warn/40 text-warn"
                }`}>{a.status === "validated" ? "通过基础校验" : a.status}</span>
                <span className="rounded-full border border-line px-1.5 py-0.5 text-[11px] text-ink-mute">
                  充分度 {a.sufficiency}
                </span>
                <span className="font-mono text-[11px] text-ink-faint">{(a.created_at ?? "").slice(0, 10)}</span>
              </div>
            ))}
          </div>
        </section>
      )}
      {scenarios.length > 0 && (
        <section>
          <h4 className="mb-1.5 text-[13px] font-semibold text-ink-mute">
            用户情景（非发布版，不参与默认结论）
          </h4>
          <div className="space-y-1">
            {scenarios.map((s) => (
              <div key={s.artifact_id} className="flex items-center gap-2 rounded border border-line bg-paper px-3 py-1.5 text-xs">
                <button onClick={() => onOpenArtifact(s.artifact_id)} className="text-accent hover:underline">
                  {s.title || s.artifact_id}
                </button>
                <span className="font-mono text-[11px] text-ink-faint">{String(s.created_at ?? "").slice(0, 10)}</span>
              </div>
            ))}
          </div>
        </section>
      )}
      {latestPlan && (
        <section>
          <h4 className="mb-1.5 text-[13px] font-semibold text-ink-soft">
            研究计划问题队列
            <span className="ml-2 font-mono text-[11px] font-normal text-ink-faint">
              {latestPlan.mode} · {latestPlan.recipe_id}@{latestPlan.recipe_version} · {latestPlan.plan_id}
            </span>
          </h4>
          {planNotes.map((n, i) => (
            <div key={i} className="mb-1 rounded border border-accent/30 bg-accent-soft px-2 py-1 text-[11px] text-accent">
              {n}
            </div>
          ))}
          <div className="overflow-x-auto rounded border border-line bg-white">
            <table className="w-full border-collapse text-xs">
              <thead>
                <tr className="border-b border-line bg-paper text-left text-[11px] uppercase text-ink-faint">
                  <th className="px-2 py-1.5">问题</th><th className="px-2 py-1.5">优先级</th>
                  <th className="px-2 py-1.5">状态</th><th className="px-2 py-1.5">结论/原因</th>
                </tr>
              </thead>
              <tbody>
                {(latestPlan.questions ?? []).map((q: Record<string, any>) => (
                  <tr key={q.question_id} className="border-b border-line/60 align-top">
                    <td className="max-w-xs px-2 py-1.5">
                      {q.text}
                      <div className="font-mono text-[11px] text-ink-faint">{q.question_id}</div>
                    </td>
                    <td className="px-2 py-1.5">{q.priority}</td>
                    <td className="px-2 py-1.5">
                      <span className={`rounded-full border px-1.5 py-0.5 text-[11px] ${
                        q.status === "answered" ? "border-pos/40 text-pos"
                        : q.status === "disputed" ? "border-red-300 text-risk"
                        : q.status === "unavailable" ? "border-neutral-300 text-ink-mute"
                        : q.status === "historical_unknown" ? "border-accent/40 text-accent"
                        : "border-warn/40 text-warn"
                      }`} title={q.status === "historical_unknown" ? "历史投影不可分辨当时进展（不借用今日状态）" : undefined}>
                        {q.status === "historical_unknown" ? "历史不可分辨" : q.status}
                      </span>
                    </td>
                    <td className="max-w-sm px-2 py-1.5 text-ink-soft">
                      {q.conclusion ?? ""}
                      {(q.unresolved ?? []).map((u: string, i: number) => (
                        <div key={i} className="text-[11px] text-ink-faint">未解决：{u}</div>
                      ))}
                      {(q.attempts ?? []).map((a: string, i: number) => (
                        <div key={i} className="text-[11px] text-ink-faint">尝试：{a}</div>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
      {assessment && (
        <section className="rounded border border-line bg-white p-3 text-xs">
          <h4 className="mb-1 font-semibold text-ink-soft">充分度评估明细</h4>
          <div className="grid gap-2 md:grid-cols-2">
            {(assessment.integrity_checks ?? []).map((c: Record<string, any>) => (
              <div key={c.name} className="flex items-start gap-1.5">
                <span className={c.passed ? "text-green-600" : "text-red-600"}>{c.passed ? "✓" : "✗"}</span>
                <span className="font-mono text-[11px] text-ink-soft">{c.name}</span>
                <span className="text-[11px] text-ink-faint">{c.detail}</span>
              </div>
            ))}
          </div>
        </section>
      )}
      {claims.length > 0 && (
        <section>
          <h4 className="mb-1.5 text-[13px] font-semibold text-ink-soft">研究论断（{claims.length}）</h4>
          <ClaimsList claims={claims} onEvidenceClick={onEvidenceClick} />
        </section>
      )}
      {obsConflicts.length > 0 && (
        <section>
          <h4 className="mb-1.5 text-[13px] font-semibold text-risk">观测冲突（同语义键竞争值，待裁决）</h4>
          {obsConflicts.map((c) => (
            <details key={c.semantic_hash} className="mb-1 rounded border border-risk/40 bg-risk-soft/60 p-2 text-xs">
              <summary className="cursor-pointer font-mono text-[11px] text-risk">{c.semantic_hash}</summary>
              {(c.history ?? []).map((o: Record<string, any>, i: number) => (
                <div key={i} className="mt-1 font-mono text-[11px] text-ink-soft">
                  v? {o.value} {o.unit} · 可知 {String(o.knowledge_time).slice(0, 10)} · {o.observation_id}
                </div>
              ))}
            </details>
          ))}
        </section>
      )}
      <section>
        <h4 className="mb-1.5 text-[13px] font-semibold text-ink-soft">来源目录（{evidence.length}，点击看原文）</h4>
        {evidence.length ? (
          <div className="overflow-x-auto rounded border border-line bg-white">
            <table className="w-full border-collapse text-xs">
              <thead>
                <tr className="border-b border-line bg-paper text-left text-[11px] uppercase text-ink-faint">
                  <th className="px-2 py-1.5">证据</th><th className="px-2 py-1.5">摘录</th>
                  <th className="px-2 py-1.5">供应商</th><th className="px-2 py-1.5">可知</th>
                  <th className="px-2 py-1.5">PIT</th><th className="px-2 py-1.5">被引用</th>
                </tr>
              </thead>
              <tbody>
                {evidence.map((e) => (
                  <tr key={e.evidence_id} className="cursor-pointer border-b border-line/60 hover:bg-paper"
                      onClick={() => onEvidenceClick(e.evidence_id)}>
                    <td className="px-2 py-1.5 font-mono text-[11px] text-accent">{e.evidence_id}</td>
                    <td className="max-w-md px-2 py-1.5 text-ink">
                      「{e.verbatim_quote.length > 90 ? `${e.verbatim_quote.slice(0, 90)}…` : e.verbatim_quote}」
                    </td>
                    <td className="px-2 py-1.5 font-mono text-[11px]">{e.provider_id}</td>
                    <td className="px-2 py-1.5 font-mono text-[11px]">{e.available_at?.slice(0, 10) ?? "—"}</td>
                    <td className="px-2 py-1.5 font-mono text-[11px]">{e.pit_grade}</td>
                    <td className="px-2 py-1.5 text-[11px] text-ink-faint">{e.used_by.slice(0, 2).join(", ")}{e.used_by.length > 2 ? "…" : ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <div className="text-meta text-ink-faint">（该快照无引用来源）</div>}
      </section>
      <section>
        <h4 className="mb-1.5 text-[13px] font-semibold text-ink-soft">数据与审计（旧字段全量）</h4>
        <LegacyFacts items={legacy} kind={snap.entity.kind} id={snap.entity.id}
                     onResolved={onResolved} readOnly={auditReadOnly} view={view} />
      </section>
    </div>
  );
}

/** 固定 registry：module id → 渲染组件（§6.3：不返回任意执行逻辑）。 */
export const MODULE_COMPONENTS: Record<string, (props: ModuleProps) => JSX.Element> = {
  investment_snapshot: InvestmentSnapshotModule,
  business_engine: BusinessEngineModule,
  industry_chain: IndustryChainModule,
  candidate_pool: CandidatePoolModule,
  revenue_segments: RevenueSegmentsModule,
  key_kpi: KeyKpiModule,
  financial_quality: FinancialQualityModule,
  expectations: ExpectationsModule,
  valuation_lab: ValuationLabModule,
  peers: PeersModule,
  catalysts_risks: CatalystsRisksModule,
  research_sources: ResearchSourcesModule,
};

export const NATURE_LEGEND = NATURE_STYLE;

// 十模块渲染器（设计 §4.4）：每章回答一个主要问题；主图 + 短解释 + 可展开
// 数据/原文/假设；缺失降级给原因（不以空图宣称完成）。
// 页面组件由固定 registry 选择——模型不能注入组件名之外的执行逻辑。

import { useEffect, useRef, useState } from "react";

import { navigate } from "../../app/route";
import { dossierApi } from "./api";
import { MetricChart, NATURE_STYLE, SeriesTable, formatMetricValue } from "./charts";
import { ClaimCard } from "./components";
import { ConflictResolver, FactValue } from "./legacy";
import type {
  BusinessGraph, ClaimItem, DossierSnapshot, EvidenceItem, LegacyFactItem,
  MetricSeries, MetricSeriesSet, ModulePayload,
} from "./types";

export interface ModuleProps {
  snap: DossierSnapshot;
  payload: ModulePayload;
  onEvidenceClick: (evidenceId: string) => void;
  onOpenArtifact: (artifactId: string) => void;
  onResolved?: () => void;
}

// ---------------- 通用件 ----------------

function Notes({ notes }: { notes?: string[] }) {
  if (!notes?.length) return null;
  return (
    <ul className="mt-2 space-y-0.5 text-[11px] text-neutral-500">
      {notes.map((n, i) => <li key={i}>· {n}</li>)}
    </ul>
  );
}

function LegacyNeedsNormBadge() {
  return (
    <span className="rounded bg-amber-50 px-1.5 py-0.5 text-[10px] text-amber-700"
          title="旧文本含数字但单位/期间口径不明——不进图表（不猜数），待补研标准化">
      needs_normalization
    </span>
  );
}

function LegacyFacts({ items, kind, id, onResolved, readOnly = false }: {
  items: LegacyFactItem[]; kind: string; id: string; onResolved?: () => void;
  /** 历史/eval 视图只读（review #9）：不得从这里调用生产 v1 裁决入口 */
  readOnly?: boolean;
}) {
  if (!items.length) return null;
  return (
    <div className="mt-3 rounded border border-neutral-200 bg-neutral-50/50 p-3">
      <div className="mb-1.5 text-[11px] font-semibold text-neutral-500">
        旧字段（数据与审计）
        {readOnly && <span className="ml-2 font-normal text-indigo-600">历史/隔离视图只读</span>}
      </div>
      {items.map((f) => (
        <div key={f.field} className="border-b border-neutral-100 py-2 last:border-0">
          <div className="mb-0.5 flex flex-wrap items-baseline gap-2">
            <span className="font-mono text-xs font-semibold text-neutral-600">{f.field}</span>
            <span className="font-mono text-[10px] text-neutral-400">
              v{f.version} · 可知 {f.knowledge_time.slice(0, 10)}
            </span>
            {f.needs_normalization && <LegacyNeedsNormBadge />}
            {f.conflict && <span className="text-[10px] text-amber-600">⚠冲突</span>}
            {f.evidence_ids.map((e) => (
              <span key={e} className="font-mono text-[10px] text-neutral-400">{e}</span>
            ))}
          </div>
          <div className="text-xs leading-relaxed text-neutral-800">
            <FactValue value={f.value} />
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
  const statusCls = calc.status === "ok" ? "text-green-700"
    : calc.status === "not_meaningful" ? "text-neutral-500" : "text-red-600";
  return (
    <div className="rounded border border-neutral-200 bg-white p-2.5 text-xs">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="font-mono font-semibold text-neutral-700">
          {calc.formula_id}<span className="text-neutral-400">@v{calc.formula_version}</span>
        </span>
        <span className={`font-mono ${statusCls}`}>
          {calc.status === "ok"
            ? `= ${calc.result ?? "—"}${calc.unit ? ` ${calc.unit}` : ""}`
            : calc.status === "not_meaningful" ? "N/M" : "计算失败"}
        </span>
        <span className="font-mono text-[10px] text-neutral-400">{calc.calculation_id}</span>
        <button onClick={() => setOpen(!open)} className="ml-auto text-[11px] text-neutral-500 hover:underline">
          {open ? "收起" : "输入与假设"}
        </button>
      </div>
      {calc.error && <div className="mt-1 text-[11px] text-red-600">{calc.error}</div>}
      {(calc.warnings ?? []).map((w: string, i: number) => (
        <div key={i} className="mt-1 text-[11px] text-amber-700">⚠ {w}</div>
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
  if (!claims.length) return <div className="text-xs text-neutral-400">（尚无论断——补研后出现）</div>;
  return (
    <div className="space-y-2">
      {claims.map((c) => <ClaimCard key={c.claim_id} claim={c} onRefClick={onEvidenceClick} />)}
    </div>
  );
}

// ---------------- 模块 registry ----------------

function InvestmentSnapshotModule({ payload, onEvidenceClick }: ModuleProps) {
  const claims = (payload.payload.claims ?? []) as ClaimItem[];
  const assessment = payload.payload.assessment as Record<string, any> | null;
  return (
    <div className="space-y-3">
      {assessment && (
        <div className="rounded border border-neutral-200 bg-white p-3 text-xs">
          <div className="mb-1 font-semibold text-neutral-700">研究充分度评估（硬门禁由代码运行）</div>
          <div className="flex flex-wrap gap-3 font-mono text-[11px] text-neutral-600">
            <span>verdict: <b>{assessment.verdict}</b></span>
            <span>问题覆盖: {assessment.question_coverage?.answered}/{assessment.question_coverage?.applicable}</span>
            <span>硬门禁: {assessment.hard_gate_passed ? "✓ 通过" : "✗ 未过"}</span>
            {assessment.stop_reason && <span>stop: {assessment.stop_reason}</span>}
          </div>
          {(assessment.gaps ?? []).length > 0 && (
            <ul className="mt-1.5 space-y-0.5 text-[11px] text-amber-700">
              {assessment.gaps.slice(0, 5).map((g: string, i: number) => <li key={i}>· {g}</li>)}
            </ul>
          )}
          {(assessment.notes ?? []).map((n: string, i: number) => (
            <div key={i} className="mt-1 text-[11px] text-neutral-500">{n}</div>
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
        <div className="rounded-lg border border-neutral-200 bg-white p-4">
          <div className="mb-1 text-xs font-semibold text-neutral-500">谁付钱，公司怎样赚钱</div>
          <p className="whitespace-pre-wrap text-sm leading-relaxed text-neutral-800">{graph.narrative}</p>
          <div className="mt-2 flex flex-wrap gap-1 font-mono text-[10px]">
            {graph.narrative_refs.map((r) => (
              r.startsWith("ev-")
                ? <button key={r} onClick={() => onEvidenceClick(r)}
                          className="rounded border border-neutral-200 px-1 py-0.5 text-neutral-600 hover:border-neutral-400">{r}</button>
                : <span key={r} className="rounded border border-neutral-100 px-1 py-0.5 text-neutral-400">{r}</span>
            ))}
          </div>
        </div>
      ) : (
        <div className="text-xs text-neutral-400">（无业务描述——待补研 business-model 问题）</div>
      )}
      {graph.nodes.length > 0 && (
        <div className="rounded-lg border border-neutral-200 bg-white p-3">
          <div className="mb-2 text-xs font-semibold text-neutral-500">
            业务流（客户 → 产品 → 收费 → 成本 → 现金流）
          </div>
          <div className="flex flex-wrap items-center gap-1.5 text-xs">
            {graph.nodes.map((n, i) => (
              <span key={n.node_id} className="flex items-center gap-1.5">
                {i > 0 && <span className="text-neutral-300">→</span>}
                <span className="rounded border border-neutral-300 bg-neutral-50 px-2 py-1" title={n.note}>
                  {n.label}
                </span>
              </span>
            ))}
          </div>
          <div className="mt-1 text-[10px] text-neutral-400">
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
        : <div className="text-xs text-neutral-400">（无总收入 typed 观测）</div>}
      {segments.length > 0 && (
        <MetricChart series={segments} title="分部收入（同口径；合计与总额差异见未分配/抵销说明）" />
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
        ? <MetricChart series={set.series} title="什么领先指标决定未来？" />
        : <div className="text-xs text-neutral-400">（无 KPI typed 观测——未披露项保留缺口，不从文本猜数）</div>}
      <Notes notes={set?.notes} />
      {defs.length > 0 && (
        <details className="rounded border border-neutral-200 bg-white p-2 text-xs">
          <summary className="cursor-pointer font-semibold text-neutral-600">
            KPI 定义与口径（行业配方 {defs.length} 项）
          </summary>
          <table className="mt-2 w-full border-collapse text-[11px]">
            <thead>
              <tr className="border-b border-neutral-200 text-left text-neutral-400">
                <th className="px-1.5 py-1">键</th><th className="px-1.5 py-1">名称</th>
                <th className="px-1.5 py-1">必需</th><th className="px-1.5 py-1">定义</th>
              </tr>
            </thead>
            <tbody>
              {defs.map((d) => (
                <tr key={d.key} className="border-b border-neutral-100">
                  <td className="px-1.5 py-1 font-mono">{d.key}</td>
                  <td className="px-1.5 py-1">{d.label}</td>
                  <td className="px-1.5 py-1">{d.required ? "✓" : "—"}</td>
                  <td className="px-1.5 py-1 text-neutral-500">{d.definition || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </div>
  );
}

function FinancialQualityModule({ snap, payload, onResolved }: ModuleProps) {
  const [freq, setFreq] = useState<"fy" | "quarterly">("fy");
  const auditReadOnly = snap.context.mode !== "live" || snap.context.namespace !== "prod";
  const set = (freq === "fy" ? payload.payload.fy : payload.payload.quarterly) as MetricSeriesSet | undefined;
  const calcs = (payload.payload.calculations ?? []) as Record<string, any>[];
  const legacy = (payload.payload.legacy ?? []) as LegacyFactItem[];
  const notes = (payload.payload.notes ?? []) as string[];
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <span className="text-xs text-neutral-500">利润是否变成现金？</span>
        <div className="ml-auto flex gap-1">
          {(["fy", "quarterly"] as const).map((f) => (
            <button key={f} onClick={() => setFreq(f)}
                    className={`rounded border px-2 py-0.5 text-[11px] ${freq === f ? "border-neutral-800 bg-neutral-900 text-white" : "border-neutral-200 text-neutral-600"}`}>
              {f === "fy" ? "年度" : "季度"}
            </button>
          ))}
        </div>
      </div>
      {set?.series.length
        ? <MetricChart series={set.series} title={freq === "fy" ? "年度：收入 / 利润 / 现金流" : "季度序列（缺期保留断点）"} />
        : <div className="text-xs text-neutral-400">（无标准化报表观测——旧字段见下方审计区，缺期不补零）</div>}
      <Notes notes={notes} />
      {calcs.length > 0 && (
        <div className="space-y-2">
          <div className="text-xs font-semibold text-neutral-600">计算链（每个值可回指输入与公式版本）</div>
          {calcs.slice(0, 8).map((c) => <CalculationCard key={c.calculation_id} calc={c} />)}
        </div>
      )}
      <LegacyFacts items={legacy} kind={snap.entity.kind} id={snap.entity.id}
                   onResolved={onResolved} readOnly={auditReadOnly} />
    </div>
  );
}

function ExpectationsModule({ payload }: ModuleProps) {
  const gc = payload.payload.guidance_consensus as MetricSeriesSet | undefined;
  const actuals = payload.payload.actuals as MetricSeriesSet | undefined;
  const deltas = (payload.payload.guidance_delta ?? []) as Record<string, any>[];
  const notes = (payload.payload.notes ?? []) as string[];
  return (
    <div className="space-y-3">
      {gc?.series.length
        ? <MetricChart series={gc.series} title="公司表现与预期差在哪里？（指引/一致预期分层）" />
        : <div className="rounded border border-dashed border-neutral-300 bg-neutral-50 p-3 text-xs text-neutral-500">
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
    <div className="rounded-lg border border-neutral-200 bg-white p-4">
      <div className="mb-1 flex items-center gap-2">
        <h4 className="text-xs font-semibold text-neutral-700">研究假设实验：Reverse DCF（FCFF 模型）</h4>
        <span className="rounded bg-amber-50 px-1.5 py-0.5 text-[10px] text-amber-700">
          预览计算——滑动不写事实；保存后是模型 artifact，不是披露事实或建议
        </span>
      </div>
      <div className="grid gap-3 text-xs md:grid-cols-2">
        <div className="space-y-2">
          <label className="block">
            <span className="mb-0.5 block text-neutral-500">基期收入 revenue_0（十进制字符串，或填观测 ref）</span>
            <div className="flex gap-1.5">
              <input value={revenue0} onChange={(e) => { invalidate(); setRevenue0(e.target.value); setRevenueRef(""); }}
                     placeholder="如 1500000000" className="w-1/2 rounded border border-neutral-200 px-2 py-1 font-mono" />
              <input value={revenueRef} onChange={(e) => { invalidate(); setRevenueRef(e.target.value); setRevenue0(""); }}
                     placeholder="obs-…（引用观测，服务端解析）" className="w-1/2 rounded border border-neutral-200 px-2 py-1 font-mono" />
            </div>
          </label>
          <label className="block">
            <span className="mb-0.5 block text-neutral-500">目标 EV（经调整的市场企业价值）</span>
            <input value={targetEv} onChange={(e) => { invalidate(); setTargetEv(e.target.value); }}
                   placeholder="如 4000000000" className="w-full rounded border border-neutral-200 px-2 py-1 font-mono" />
          </label>
          <label className="block">
            <span className="mb-0.5 block text-neutral-500">预测年数</span>
            <input value={years} onChange={(e) => { invalidate(); setYears(e.target.value); }}
                   className="w-24 rounded border border-neutral-200 px-2 py-1 font-mono" />
          </label>
          <button
            onClick={() => {
              invalidate();
              setAssumptions(Object.fromEntries(DCF_ASSUMPTION_SPEC.map((a) => [a.key, a.def])));
              setYears("10");
            }}
            className="rounded border border-neutral-200 px-2 py-0.5 text-[11px] text-neutral-500 hover:border-neutral-400"
          >
            重置假设（恢复默认值）
          </button>
          <div className="text-[10px] text-neutral-400">
            净债务未知不默认零：未提供时不输出 Equity_model。买卖评级/目标价区间归 /decide。
          </div>
        </div>
        <div className="space-y-1.5">
          {DCF_ASSUMPTION_SPEC.map((a) => (
            <label key={a.key} className="flex items-center gap-2">
              <span className="w-24 shrink-0 text-[11px] text-neutral-600">{a.label}</span>
              <input type="range" min={a.min} max={a.max} step={a.step} value={num(assumptions[a.key])}
                     onChange={(e) => { invalidate(); setAssumptions((s) => ({ ...s, [a.key]: e.target.value })); }}
                     className="flex-1 accent-neutral-800" aria-label={a.label} />
              <input value={assumptions[a.key]}
                     onChange={(e) => { invalidate(); setAssumptions((s) => ({ ...s, [a.key]: e.target.value })); }}
                     className="w-16 rounded border border-neutral-200 px-1 py-0.5 text-right font-mono text-[11px]" />
            </label>
          ))}
        </div>
      </div>
      <div className="mt-3 border-t border-neutral-100 pt-2">
        {busy && <div className="text-xs text-neutral-400">计算中（后端确定性公式，非前端估算）…</div>}
        {error && <div className="text-xs text-red-600">{error}</div>}
        {result && result.status === "ok" && (
          <div className="flex flex-wrap items-center gap-3">
            <div className="font-mono text-xl font-semibold tabular-nums text-neutral-900">
              隐含 g = {(Number(result.result) * 100).toFixed(2)}%
            </div>
            <div className="text-[10px] text-neutral-400">
              <div>calculation {result.calculation_id} · assumption_hash {String(result.input_hash).slice(0, 20)}…</div>
              <div>求解区间 {JSON.stringify(result.extra?.solve_interval)} · 残差 {String(result.extra?.residual ?? "").slice(0, 12)}</div>
            </div>
            <button onClick={saveScenario}
                    className="ml-auto rounded border border-neutral-300 px-2.5 py-1 text-[11px] font-semibold hover:border-neutral-500">
              保存情景（建立模型 artifact）
            </button>
          </div>
        )}
        {result && result.status !== "ok" && (
          <div className="text-xs text-amber-700">
            {result.status === "not_meaningful" ? "N/M" : "计算失败"}：{result.error}
            {(result.warnings ?? []).map((w: string, i: number) => <div key={i}>⚠ {w}</div>)}
          </div>
        )}
        {result !== null && result.status === "ok" && (result.warnings ?? []).length > 0 && (
          <div className="mt-1 text-[10px] text-amber-700">
            {result.warnings.map((w: string, i: number) => <div key={i}>⚠ {w}</div>)}
          </div>
        )}
        {saved && (
          <div className="mt-2 rounded border border-green-200 bg-green-50 px-2 py-1.5 text-[11px] text-green-800">
            已保存情景 {saved.name}（不改变发布快照）
            <button onClick={() => navigate({ page: "research", artifactId: saved.artifact_id, params: {} })}
                    className="ml-2 text-blue-700 hover:underline">查看 →</button>
            <button onClick={() => setSaved(null)} className="ml-2 text-neutral-500 hover:underline">继续实验</button>
          </div>
        )}
      </div>
    </div>
  );
}

function ValuationLabModule({ snap, payload, onResolved }: ModuleProps) {
  const auditReadOnly = snap.context.mode !== "live" || snap.context.namespace !== "prod";
  const calcs = (payload.payload.calculations ?? []) as Record<string, any>[];
  const legacy = (payload.payload.legacy ?? []) as LegacyFactItem[];
  const notes = (payload.payload.notes ?? []) as string[];
  const dcf = calcs.find((c) => c.formula_id === "reverse_dcf");
  const dcfDisabled = snap.modules.valuation_lab?.status === "not_applicable";
  return (
    <div className="space-y-3">
      {dcf && dcf.status === "ok" && (
        <div className="rounded-lg border border-neutral-200 bg-white p-4">
          <div className="text-xs font-semibold text-neutral-600">价格要求怎样的经营表现？（反向求解）</div>
          <div className="mt-1 font-mono text-2xl font-semibold tabular-nums text-neutral-900">
            g = {(Number(dcf.result) * 100).toFixed(1)}%
            <span className="ml-2 text-xs font-normal text-neutral-400">隐含收入增速（在下列假设下）</span>
          </div>
          <div className="mt-1 text-[11px] text-neutral-500">
            这是「在这些条件下价格隐含的增长率」，不是从股价唯一反推增长/利润率/倍数三项。
          </div>
        </div>
      )}
      {!dcfDisabled && <ReverseDcfPanel snap={snap} />}
      {dcfDisabled && (
        <div className="rounded border border-neutral-200 bg-neutral-50 p-3 text-xs text-neutral-500">
          行业配方禁用通用 EV/FCFF 模型（未盈利/现金流不可建模）——不硬套，见配方说明。
        </div>
      )}
      {calcs.length
        ? calcs.map((c) => <CalculationCard key={c.calculation_id} calc={c} />)
        : !dcfDisabled && (
          <div className="text-[11px] text-neutral-400">
            尚无已登记的正式估值计算（上方实验为预览；保存后成为模型 artifact）。
          </div>
        )}
      <Notes notes={notes} />
      <LegacyFacts items={legacy} kind={snap.entity.kind} id={snap.entity.id}
                   onResolved={onResolved} readOnly={auditReadOnly} />
    </div>
  );
}

function PeersModule({ snap, payload, onResolved }: ModuleProps) {
  const auditReadOnly = snap.context.mode !== "live" || snap.context.namespace !== "prod";
  const legacy = (payload.payload.legacy ?? []) as LegacyFactItem[];
  const peerSeries = (payload.payload.peer_series ?? []) as Record<string, any>[];
  const notes = (payload.payload.notes ?? []) as string[];
  return (
    <div className="space-y-3">
      {peerSeries.length > 0 ? (
        <div className="overflow-x-auto rounded border border-neutral-200 bg-white">
          <table className="w-full border-collapse text-xs">
            <thead>
              <tr className="border-b border-neutral-200 bg-neutral-50 text-left text-[10px] uppercase text-neutral-500">
                <th className="px-2 py-1.5">实体</th><th className="px-2 py-1.5">指标</th>
                <th className="px-2 py-1.5">值</th><th className="px-2 py-1.5">期间</th>
              </tr>
            </thead>
            <tbody>
              {peerSeries.flatMap((p) => (p.metrics ?? []).map((m: Record<string, any>, i: number) => (
                <tr key={`${p.entity_id}-${i}`} className="border-b border-neutral-100">
                  <td className="px-2 py-1 font-mono">{p.entity_id}</td>
                  <td className="px-2 py-1 font-mono text-neutral-500">{m.metric_key}</td>
                  <td className="px-2 py-1 font-mono tabular-nums">{formatMetricValue(m.value, "", null)}</td>
                  <td className="px-2 py-1 font-mono text-[10px] text-neutral-400">{m.period_label}</td>
                </tr>
              )))}
            </tbody>
          </table>
        </div>
      ) : null}
      <Notes notes={notes} />
      <LegacyFacts items={legacy} kind={snap.entity.kind} id={snap.entity.id}
                   onResolved={onResolved} readOnly={auditReadOnly} />
    </div>
  );
}

function CatalystsRisksModule({ snap, payload, onEvidenceClick, onResolved }: ModuleProps) {
  const auditReadOnly = snap.context.mode !== "live" || snap.context.namespace !== "prod";
  const legacy = (payload.payload.legacy ?? []) as LegacyFactItem[];
  const claims = (payload.payload.claims ?? []) as ClaimItem[];
  return (
    <div className="space-y-3">
      <div className="text-xs text-neutral-500">何时验证？什么情况下失效？</div>
      <ClaimsList claims={claims} onEvidenceClick={onEvidenceClick} />
      <LegacyFacts items={legacy} kind={snap.entity.kind} id={snap.entity.id}
                   onResolved={onResolved} readOnly={auditReadOnly} />
    </div>
  );
}

function ResearchSourcesModule({ snap, payload, onEvidenceClick, onOpenArtifact, onResolved }: ModuleProps) {
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
          <h4 className="mb-1.5 text-xs font-semibold text-neutral-600">研究产物（冻结研报）</h4>
          <div className="space-y-1.5">
            {artifacts.map((a) => (
              <div key={a.artifact_id} className="flex flex-wrap items-center gap-2 rounded border border-neutral-200 bg-white px-3 py-2 text-xs">
                <button onClick={() => onOpenArtifact(a.artifact_id)} className="font-semibold text-blue-700 hover:underline">
                  {a.title || a.artifact_id}
                </button>
                <span className={`rounded-full border px-1.5 py-0.5 text-[10px] ${
                  a.status === "validated" ? "border-green-300 text-green-700" : "border-amber-300 text-amber-700"
                }`}>{a.status === "validated" ? "通过基础校验" : a.status}</span>
                <span className="rounded-full border border-neutral-200 px-1.5 py-0.5 text-[10px] text-neutral-500">
                  充分度 {a.sufficiency}
                </span>
                <span className="font-mono text-[10px] text-neutral-400">{(a.created_at ?? "").slice(0, 10)}</span>
              </div>
            ))}
          </div>
        </section>
      )}
      {scenarios.length > 0 && (
        <section>
          <h4 className="mb-1.5 text-xs font-semibold text-neutral-500">
            用户情景（非发布版，不参与默认结论）
          </h4>
          <div className="space-y-1">
            {scenarios.map((s) => (
              <div key={s.artifact_id} className="flex items-center gap-2 rounded border border-neutral-200 bg-neutral-50 px-3 py-1.5 text-xs">
                <button onClick={() => onOpenArtifact(s.artifact_id)} className="text-blue-700 hover:underline">
                  {s.title || s.artifact_id}
                </button>
                <span className="font-mono text-[10px] text-neutral-400">{String(s.created_at ?? "").slice(0, 10)}</span>
              </div>
            ))}
          </div>
        </section>
      )}
      {latestPlan && (
        <section>
          <h4 className="mb-1.5 text-xs font-semibold text-neutral-600">
            研究计划问题队列
            <span className="ml-2 font-mono text-[10px] font-normal text-neutral-400">
              {latestPlan.mode} · {latestPlan.recipe_id}@{latestPlan.recipe_version} · {latestPlan.plan_id}
            </span>
          </h4>
          {planNotes.map((n, i) => (
            <div key={i} className="mb-1 rounded border border-indigo-200 bg-indigo-50 px-2 py-1 text-[11px] text-indigo-700">
              {n}
            </div>
          ))}
          <div className="overflow-x-auto rounded border border-neutral-200 bg-white">
            <table className="w-full border-collapse text-xs">
              <thead>
                <tr className="border-b border-neutral-200 bg-neutral-50 text-left text-[10px] uppercase text-neutral-400">
                  <th className="px-2 py-1.5">问题</th><th className="px-2 py-1.5">优先级</th>
                  <th className="px-2 py-1.5">状态</th><th className="px-2 py-1.5">结论/原因</th>
                </tr>
              </thead>
              <tbody>
                {(latestPlan.questions ?? []).map((q: Record<string, any>) => (
                  <tr key={q.question_id} className="border-b border-neutral-100 align-top">
                    <td className="max-w-xs px-2 py-1.5">
                      {q.text}
                      <div className="font-mono text-[10px] text-neutral-400">{q.question_id}</div>
                    </td>
                    <td className="px-2 py-1.5">{q.priority}</td>
                    <td className="px-2 py-1.5">
                      <span className={`rounded-full border px-1.5 py-0.5 text-[10px] ${
                        q.status === "answered" ? "border-green-300 text-green-700"
                        : q.status === "disputed" ? "border-red-300 text-red-700"
                        : q.status === "unavailable" ? "border-neutral-300 text-neutral-500"
                        : q.status === "historical_unknown" ? "border-indigo-300 text-indigo-600"
                        : "border-amber-300 text-amber-700"
                      }`} title={q.status === "historical_unknown" ? "历史投影不可分辨当时进展（不借用今日状态）" : undefined}>
                        {q.status === "historical_unknown" ? "历史不可分辨" : q.status}
                      </span>
                    </td>
                    <td className="max-w-sm px-2 py-1.5 text-neutral-600">
                      {q.conclusion ?? ""}
                      {(q.unresolved ?? []).map((u: string, i: number) => (
                        <div key={i} className="text-[10px] text-neutral-400">未解决：{u}</div>
                      ))}
                      {(q.attempts ?? []).map((a: string, i: number) => (
                        <div key={i} className="text-[10px] text-neutral-400">尝试：{a}</div>
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
        <section className="rounded border border-neutral-200 bg-white p-3 text-xs">
          <h4 className="mb-1 font-semibold text-neutral-600">充分度评估明细</h4>
          <div className="grid gap-2 md:grid-cols-2">
            {(assessment.integrity_checks ?? []).map((c: Record<string, any>) => (
              <div key={c.name} className="flex items-start gap-1.5">
                <span className={c.passed ? "text-green-600" : "text-red-600"}>{c.passed ? "✓" : "✗"}</span>
                <span className="font-mono text-[11px] text-neutral-600">{c.name}</span>
                <span className="text-[11px] text-neutral-400">{c.detail}</span>
              </div>
            ))}
          </div>
        </section>
      )}
      {claims.length > 0 && (
        <section>
          <h4 className="mb-1.5 text-xs font-semibold text-neutral-600">研究论断（{claims.length}）</h4>
          <ClaimsList claims={claims} onEvidenceClick={onEvidenceClick} />
        </section>
      )}
      {obsConflicts.length > 0 && (
        <section>
          <h4 className="mb-1.5 text-xs font-semibold text-red-700">观测冲突（同语义键竞争值，待裁决）</h4>
          {obsConflicts.map((c) => (
            <details key={c.semantic_hash} className="mb-1 rounded border border-red-200 bg-red-50/40 p-2 text-xs">
              <summary className="cursor-pointer font-mono text-[11px] text-red-700">{c.semantic_hash}</summary>
              {(c.history ?? []).map((o: Record<string, any>, i: number) => (
                <div key={i} className="mt-1 font-mono text-[11px] text-neutral-600">
                  v? {o.value} {o.unit} · 可知 {String(o.knowledge_time).slice(0, 10)} · {o.observation_id}
                </div>
              ))}
            </details>
          ))}
        </section>
      )}
      <section>
        <h4 className="mb-1.5 text-xs font-semibold text-neutral-600">来源目录（{evidence.length}，点击看原文）</h4>
        {evidence.length ? (
          <div className="overflow-x-auto rounded border border-neutral-200 bg-white">
            <table className="w-full border-collapse text-xs">
              <thead>
                <tr className="border-b border-neutral-200 bg-neutral-50 text-left text-[10px] uppercase text-neutral-400">
                  <th className="px-2 py-1.5">证据</th><th className="px-2 py-1.5">摘录</th>
                  <th className="px-2 py-1.5">供应商</th><th className="px-2 py-1.5">可知</th>
                  <th className="px-2 py-1.5">PIT</th><th className="px-2 py-1.5">被引用</th>
                </tr>
              </thead>
              <tbody>
                {evidence.map((e) => (
                  <tr key={e.evidence_id} className="cursor-pointer border-b border-neutral-100 hover:bg-neutral-50"
                      onClick={() => onEvidenceClick(e.evidence_id)}>
                    <td className="px-2 py-1.5 font-mono text-[10px] text-blue-700">{e.evidence_id}</td>
                    <td className="max-w-md px-2 py-1.5 text-neutral-700">
                      「{e.verbatim_quote.length > 90 ? `${e.verbatim_quote.slice(0, 90)}…` : e.verbatim_quote}」
                    </td>
                    <td className="px-2 py-1.5 font-mono text-[10px]">{e.provider_id}</td>
                    <td className="px-2 py-1.5 font-mono text-[10px]">{e.available_at?.slice(0, 10) ?? "—"}</td>
                    <td className="px-2 py-1.5 font-mono text-[10px]">{e.pit_grade}</td>
                    <td className="px-2 py-1.5 text-[10px] text-neutral-400">{e.used_by.slice(0, 2).join(", ")}{e.used_by.length > 2 ? "…" : ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <div className="text-xs text-neutral-400">（该快照无引用来源）</div>}
      </section>
      <section>
        <h4 className="mb-1.5 text-xs font-semibold text-neutral-600">数据与审计（旧字段全量）</h4>
        <LegacyFacts items={legacy} kind={snap.entity.kind} id={snap.entity.id}
                     onResolved={onResolved} readOnly={auditReadOnly} />
      </section>
    </div>
  );
}

/** 固定 registry：module id → 渲染组件（§6.3：不返回任意执行逻辑）。 */
export const MODULE_COMPONENTS: Record<string, (props: ModuleProps) => JSX.Element> = {
  investment_snapshot: InvestmentSnapshotModule,
  business_engine: BusinessEngineModule,
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

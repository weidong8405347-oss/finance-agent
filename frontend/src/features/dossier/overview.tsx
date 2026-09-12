// Overview 撕页（优化方案 §6-§9/§47）：Tear Sheet + Investment Committee Summary。
// 首屏 10-15 秒回答六问：是什么 / 为什么是现在 / 什么最重要 / 钱在哪 / 谁赢 / 什么证伪。
//
// 密度硬规则（§9）：Thesis ≤3 · KPI 4-6 · Top 公司 ≤5 · 催化 ≤3 · 风险 ≤3 · 头部徽标 ≤3；
// 正文区最多 2 列；3-4 列只用于 KPI；无数据的分块整体隐藏（不编造、不留空壳）。

import type { ReactNode } from "react";

import { buildCitationIndex, CitationChips } from "./citations";
import { LongText, MetricValue } from "./components";
import type {
  BusinessGraph, CandidateItem, ChangeLogEntry, DossierSnapshot, IndustryMapNode,
  KeyMetric, ValidationItem,
} from "./types";

// ---------------- 纯函数（可测） ----------------

/** 指标键 → 展示名：配方外指标的 label 是 raw key（"discovery preclinical cost"），
 *  已知键映射中文，未知 snake_case 人性化（表现层职责，不改数据）。 */
const METRIC_LABEL_CN: Record<string, string> = {
  market_size: "市场规模", growth_rate: "增速", capacity_supply: "供给/产能",
  revenue: "收入", gross_margin: "毛利率", net_income: "净利润", cfo: "经营现金流",
  capex: "资本开支", fcf: "自由现金流", ebitda: "EBITDA", net_debt: "净债务",
  orders: "订单", firm_backlog: "在手订单", capacity: "产能", deliveries: "交付量",
  pipeline: "管线", cash_runway: "现金跑道", quarterly_burn: "季度烧钱",
  r_and_d: "研发投入", market_cap: "市值", enterprise_value: "企业价值",
  share_price: "股价", market_share: "市场份额", share_dilution: "股权稀释",
  gross_profit: "毛利润", operating_income: "营业利润",
  cash_and_equivalents: "现金及等价物",
  discovery_preclinical_cost: "发现+临床前成本",
  discovery_preclinical_timeline: "发现+临床前周期",
  pricing_dispersion_ratio: "定价模式价差",
};

export function metricDisplayLabel(metricKey: string, label: string): string {
  if (METRIC_LABEL_CN[metricKey]) return METRIC_LABEL_CN[metricKey];
  if (/[\u4e00-\u9fff]/.test(label)) return label; // 已含中文直接用
  // snake_case → Title Case（仅表现层人性化）
  return label.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/** 研究质量圆点（§18）：正文不铺 refs_resolvable/facts_checked 细节，
 *  头部只给 ●●●○ 四级；hover 展开明细，点击进「研究与来源」。 */
export function qualityDots(snap: DossierSnapshot): {
  filled: 0 | 1 | 2 | 3 | 4;
  label: string;
  detail: string[];
} {
  const cred = (snap.summary.credibility ?? {}) as Record<string, string>;
  const level = cred.level || cred.sufficiency || snap.research.verdict || "";
  const filled = level === "sufficient" ? 4 : level === "partial" ? 2 : level ? 1 : 0;
  const label = level === "sufficient" ? "充分" : level === "partial" ? "部分" : level === "blocked" ? "受阻" : "未评估";
  const detail: string[] = [];
  if (cred.refs_resolvable) detail.push(`引用可解析：${cred.refs_resolvable}`);
  if (cred.facts_checked) detail.push(`事实已核对：${cred.facts_checked}`);
  if (cred.analysis_reviewed) detail.push(`分析复核：${cred.analysis_reviewed}`);
  if (cred.note) detail.push(cred.note);
  if (snap.research.required > 0) {
    detail.push(`关键问题覆盖 ${snap.research.answered}/${snap.research.required}`);
  }
  return { filled: filled as 0 | 1 | 2 | 3 | 4, label, detail };
}

/** Top 公司（§9 上限 5）：tier 序 included→watchlist→needs_review→excluded，层内保持原序。 */
const TIER_RANK: Record<string, number> = { included: 0, watchlist: 1, needs_review: 2, excluded: 3 };

export function topCompanies(candidates: CandidateItem[], limit = 5): CandidateItem[] {
  return [...candidates]
    .map((c, i) => ({ c, i }))
    .sort((a, b) => (TIER_RANK[a.c.tier] ?? 9) - (TIER_RANK[b.c.tier] ?? 9) || a.i - b.i)
    .slice(0, limit)
    .map((x) => x.c);
}

/** What Changed 分类（§11/§32）：把「本轮进展」改写为投资语义的变化条目。
 *  [module] 前缀提炼为标签；反证/风险 → !；候选变动 → +；其余 → ↑。 */
export interface ChangeEntry {
  icon: "up" | "risk" | "new";
  tag: string | null;
  text: string;
}

const CHANGE_TAG_LABELS: Record<string, string> = {
  policy: "政策", "candidate-pool": "候选池", "counter-evidence": "反证",
  "value-chain": "价值链", "demand-supply": "供需", bottleneck: "瓶颈",
  objective: "目标", "business-engine": "商业引擎", "key-kpi": "KPI",
  "financial-quality": "财务", "industry-chain": "产业链",
};

export function classifyChange(raw: string): ChangeEntry {
  const m = /^\[([^\]]+)\]\s*([\s\S]*)$/.exec(raw);
  // targeted-xxx 是问题 id（研究过程语言），不作标签展示（§10）
  const rawTag = m ? m[1] : null;
  const tag = rawTag
    ? (CHANGE_TAG_LABELS[rawTag] ?? (rawTag.startsWith("targeted-") ? "定向补研" : rawTag))
    : null;
  const text = (m ? m[2] : raw).trim();
  if (tag === "反证" || /反证|证伪|风险|矛盾|未裁决|冲突/.test(text.slice(0, 60))) {
    return { icon: "risk", tag, text };
  }
  if (/新增|加入|入选|移入|升级|迁移|移出|淘汰/.test(text.slice(0, 60))) {
    return { icon: "new", tag, text };
  }
  return { icon: "up", tag, text };
}

/** 最近催化剂（§29）：expected 验证项按窗口起点排序，取前 limit。 */
export function nextCatalysts(items: ValidationItem[], limit = 3): ValidationItem[] {
  return items
    .filter((i) => i.status === "expected" && (i.window_start || i.window_end))
    .sort((a, b) => (a.window_start || a.window_end).localeCompare(b.window_start || b.window_end))
    .slice(0, limit);
}

/** 公司 → 产业链环节（Best Expressions 的 Position 列；无归属则不显示该列值）。 */
export function companyPositionMap(graph: BusinessGraph | null | undefined): Map<string, string> {
  const map = new Map<string, string>();
  for (const n of graph?.nodes ?? []) {
    for (const c of n.company_refs ?? []) {
      const key = c.toUpperCase();
      if (!map.has(key)) map.set(key, n.label);
    }
  }
  return map;
}

/** 瓶颈漏斗（§7 KEY BOTTLENECK）：按 layers 顺序纵向流，瓶颈节点红标。
 *  无结构化产业链 → null（调用方回退到瓶颈文本列表）。 */
export interface FunnelStage {
  key: string;
  label: string;
  nodes: { id: string; label: string; bottleneck: boolean }[];
}

export function funnelFromIndustryMap(graph: BusinessGraph | null | undefined): FunnelStage[] | null {
  if (!graph || !graph.nodes?.length) return null;
  const layerKeys = graph.layers?.length
    ? graph.layers
    : Array.from(new Set(graph.nodes.map((n) => n.layer).filter(Boolean)));
  const stages: FunnelStage[] = [];
  for (const key of layerKeys) {
    if (!key) continue;
    const nodes = graph.nodes
      .filter((n) => (n.layer || "") === key)
      .map((n) => ({ id: n.node_id, label: n.label, bottleneck: Boolean(n.bottleneck) }));
    if (nodes.length) {
      stages.push({ key, label: graph.layer_labels?.[key] ?? LAYER_FALLBACK[key] ?? key, nodes });
    }
  }
  return stages.length ? stages : null;
}

const LAYER_FALLBACK: Record<string, string> = {
  upstream: "上游", midstream: "中游", downstream: "下游", demand: "需求端",
  platform: "平台", application: "应用", infrastructure: "基础设施",
};

const TIER_LABEL: Record<string, string> = {
  included: "核心", watchlist: "观察", excluded: "淘汰", needs_review: "待核实",
};

/** 阶段 token → 中文（离散分类的人性化，不是评分；未知阶段保留原文）。
 *  与后端 positioning.STAGE_LABELS_CN 同源（技术轴 + 商业轴 canonical 全覆盖）。 */
export const STAGE_LABEL_CN: Record<string, string> = {
  early: "早期", preclinical: "临床前", clinical: "临床", commercial: "商业化", mature: "成熟",
  none: "未商业化", pilot: "试点", early_revenue: "早期收入", scaling: "规模化", profitable: "盈利",
};

/** 展示文本中的 raw ID 提取（§10/§16）：后端叙述文本里内嵌的 ev-x/claim-x 是审计信息——
 *  正文剥离这些 token，ev-x 转为 [n] 引用芯片；纯展示层变换，不改数据。 */
export const REF_TOKEN_RE = /\b(?:ev|obs|claim|calc)-[0-9a-z-]{3,}\b/g;

export function splitTextRefs(text: string): { clean: string; refs: string[] } {
  if (!text) return { clean: "", refs: [] };
  const refs = text.match(REF_TOKEN_RE) ?? [];
  const clean = text
    .replace(REF_TOKEN_RE, "")
    .replace(/[（(][\s、，,;；·]*[）)]/g, "") // 抽空出来/仅剩分隔符的圆括号（（、）类残留）
    .replace(/\[[\s、，,;；·]*\]/g, "")       // 抽空出来/仅剩分隔符的方括号（[ev-x] → [] 残留）
    .replace(/([，。；、：])\s*[\])）]/g, "$1") // 「, ]」类孤立闭括号（剥掉引用后仅剩括号）
    .replace(/\s{2,}/g, " ")            // 收缩多余空白
    .replace(/\s+([，。；、：）\]])/g, "$1") // 标点前不留空格
    .trim();
  return { clean, refs };
}

/** 递归剥离子（旧字段的 value 可能是嵌套 list/dict）：投资者视图的 FactValue 用。 */
export function stripRefsDeep(value: unknown): unknown {
  if (typeof value === "string") return splitTextRefs(value).clean;
  if (Array.isArray(value)) return value.map(stripRefsDeep);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([k, v]) => [k, stripRefsDeep(v)]),
    );
  }
  return value;
}

/** 候选行的「验证阶段」列：技术 + 商业阶段（中文映射优先，未知原文保留）。 */
export function validationText(c: CandidateItem): string {
  return [c.technology_stage, c.commercial_stage]
    .filter(Boolean)
    .map((s) => STAGE_LABEL_CN[s] ?? s)
    .join(" · ");
}
const TIER_DOT: Record<string, string> = {
  included: "bg-pos", watchlist: "bg-accent", excluded: "bg-ink-faint", needs_review: "bg-warn",
};

// ---------------- 展示组件 ----------------

/** 研究质量圆点（头部唯一质量表达；明细只在 hover title）。 */
export function QualityDots({ snap }: { snap: DossierSnapshot }) {
  const q = qualityDots(snap);
  return (
    <span className="inline-flex items-center gap-1.5" title={q.detail.join("\n") || "尚无质量明细"}>
      <span className="text-meta text-ink-mute">研究质量</span>
      <span aria-label={`研究质量 ${q.filled}/4（${q.label}）`} className="tracking-[0.1em]">
        {[0, 1, 2, 3].map((i) => (
          <span key={i} className={i < q.filled ? "text-accent" : "text-line"}>●</span>
        ))}
      </span>
      <span className="text-meta text-ink-mute">{q.label}</span>
    </span>
  );
}

/** Kicker + 内容的分块容器（有内容才渲染）。 */
export function Block({ kicker, children, className = "" }: {
  kicker: string; children: ReactNode; className?: string;
}) {
  return (
    <section className={className}>
      <div className="mb-2 border-b border-line pb-1.5">
        <span className="dos-kicker">{kicker}</span>
      </div>
      {children}
    </section>
  );
}

/** INVESTMENT VIEW（§7/§8）：结论以 16px/1.85 排版，阅读宽度 ≤76ch；
 *  来源用 [n] 编号，raw ID 不进正文。 */
export function InvestmentView({ snap, citeIndex, onCite }: {
  snap: DossierSnapshot;
  citeIndex: Map<string, number>;
  onCite: (id: string) => void;
}) {
  const s = snap.summary;
  const kindLabel = s.thesis_kind === "claim" ? "通过基础校验的研究论断"
    : s.thesis_kind === "draft" ? "研究草稿（未完成校验）"
    : s.thesis_kind === "legacy_analysis" ? "历史档案论点" : null;
  return (
    <Block kicker="Investment View · 投资观点">
      {s.thesis ? (
        <p className="max-w-[76ch] text-[15px] leading-[1.85] text-ink">
          {s.thesis}
          <CitationChips refs={s.thesis_refs} index={citeIndex} onCite={onCite} />
        </p>
      ) : (
        <p className="text-sm text-ink-faint">
          尚无研究结论——点击右上「补研」发起问题驱动研究。
        </p>
      )}
      {kindLabel && s.thesis && (
        <div className="mt-2 text-meta text-ink-faint">{kindLabel}</div>
      )}
    </Block>
  );
}

/** KEY METRICS（§9：4-6 个；28px 数字；缺口如实但不占首屏）。 */
export function KeyMetricGrid({ snap, citeIndex, onCite }: {
  snap: DossierSnapshot;
  citeIndex: Map<string, number>;
  onCite: (id: string) => void;
}) {
  const ok = snap.summary.key_metrics.filter((m) => m.status !== "missing").slice(0, 6);
  const missing = snap.summary.key_metrics.filter((m) => m.status === "missing");
  if (!ok.length) {
    return (
      <Block kicker="Key Metrics · 关键指标">
        <div className="rounded-card border border-dashed border-line px-4 py-3 text-sm text-ink-faint">
          关键指标缺口：{missing.map((m) => metricDisplayLabel(m.metric_key, m.label)).join("、") || "未定义"}
          ——不以零或旧估计补位，可在「补研」中定向补齐。
        </div>
      </Block>
    );
  }
  const UNIT_CN: Record<string, string> = { months: "个月", years: "年", days: "天", percent: "%" };
  const unitLabel = (m: KeyMetric) =>
    m.unit && m.unit !== "ratio" && m.unit !== m.currency ? (UNIT_CN[m.unit] ?? m.unit) : "";
  return (
    <Block kicker="Key Metrics · 关键指标">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
        {ok.map((m) => (
          <button
            key={m.metric_key}
            onClick={() => {
              const firstEv = (m.evidence_refs ?? []).find((r) => r.startsWith("ev-"));
              if (firstEv) onCite(firstEv);
            }}
            disabled={!(m.evidence_refs ?? []).some((r) => r.startsWith("ev-"))}
            title={`${m.period_label || "期间未注明"} · ${m.nature || "—"}` +
              `${m.raw_text ? ` · 披露原文「${m.raw_text}」` : ""}` +
              `${m.as_of_note ? ` · ${m.as_of_note}` : ""}（点击查看来源）`}
            className={`rounded-card border bg-white px-3.5 py-3 text-left transition-colors ${
              m.status === "conflicted" ? "border-risk/40" : m.status === "stale" ? "border-warn/40" : "border-line"
            } hover:border-accent/50`}
          >
            <div className="truncate text-meta text-ink-mute" title={metricDisplayLabel(m.metric_key, m.label)}>
              {metricDisplayLabel(m.metric_key, m.label)}
              {m.status === "stale" && <span className="ml-1 text-warn">·陈旧</span>}
              {m.status === "conflicted" && <span className="ml-1 text-risk">·冲突</span>}
            </div>
            <div className="dos-num mt-1 text-[26px] font-semibold leading-tight text-ink">
              <MetricValue value={m.value} unit={m.unit} currency={m.currency} rawText={m.raw_text} />
              {unitLabel(m) && <span className="ml-0.5 text-meta font-normal text-ink-faint">{unitLabel(m)}</span>}
            </div>
            <div className="dos-num mt-0.5 text-meta text-ink-faint">{m.period_label || "—"}</div>
          </button>
        ))}
      </div>
      {missing.length > 0 && (
        <div className="mt-2 text-meta text-ink-faint">
          缺口：{missing.map((m) => metricDisplayLabel(m.metric_key, m.label)).join("、")}
        </div>
      )}
    </Block>
  );
}

/** WHY NOW（有内容才渲染）。文本内嵌的 ev-x/obs-x 引用剥离子为 [n] 芯片（§16）。 */
export function WhyNow({ items, citeIndex, onCite }: {
  items?: string[];
  citeIndex?: Map<string, number>;
  onCite?: (id: string) => void;
}) {
  if (!items?.length) return null;
  return (
    <Block kicker="Why Now · 为什么是现在">
      <ul className="space-y-1.5">
        {items.slice(0, 5).map((w, i) => {
          const { clean, refs } = splitTextRefs(w);
          return (
            <li key={i} className="flex gap-2 text-sm leading-relaxed text-ink-soft">
              <span className="dos-num shrink-0 text-ink-faint">{i + 1}</span>
              <span>
                {clean}
                {citeIndex && onCite && (
                  <CitationChips refs={refs} index={citeIndex} onCite={onCite} maxVisible={2} />
                )}
              </span>
            </li>
          );
        })}
      </ul>
    </Block>
  );
}

/** KEY BOTTLENECK：结构化漏斗（产业链 layers）或瓶颈文本列表。
 *  纪律：全部环节都被标瓶颈 = 没有环节被标（红色失效）——只在 0<瓶颈数<总数 时红标；
 *  节点名与层名重复时不重复显示。 */
export function Bottleneck({ snap, citeIndex, onCite }: {
  snap: DossierSnapshot;
  citeIndex?: Map<string, number>;
  onCite?: (id: string) => void;
}) {
  const graph = (snap.structures?.industry_map ?? null) as BusinessGraph | null;
  const funnel = funnelFromIndustryMap(graph);
  const bottlenecks = snap.summary.bottlenecks ?? [];
  if (!funnel && !bottlenecks.length) return null;
  const bottleneckStages = funnel?.filter((s) => s.nodes.some((n) => n.bottleneck)).length ?? 0;
  const discriminate = funnel ? bottleneckStages > 0 && bottleneckStages < funnel.length : false;
  return (
    <Block kicker="Key Bottleneck · 关键瓶颈">
      {funnel ? (
        <ol className="space-y-0.5">
          {funnel.map((stage, i) => {
            const hasBottleneck = discriminate && stage.nodes.some((n) => n.bottleneck);
            // 节点名与层名重复时只显示层名（数据常以同名单节点占一层）
            const distinct = stage.nodes.map((n) => n.label).filter((l) => l !== stage.label);
            return (
              <li key={stage.key}>
                {i > 0 && <div className="pl-3 leading-none text-ink-faint">↓</div>}
                <div className={`flex items-center gap-2 rounded-md border px-3 py-1.5 ${
                  hasBottleneck ? "border-risk/40 bg-risk-soft" : "border-line bg-white"
                }`}>
                  <span className="text-sm font-medium text-ink">{stage.label}</span>
                  {distinct.length > 0 && (
                    <span className="text-meta text-ink-mute">{distinct.join(" / ")}</span>
                  )}
                  {hasBottleneck && (
                    <span className="ml-auto shrink-0 rounded bg-risk px-1.5 py-0.5 text-[11px] font-semibold text-white">
                      瓶颈
                    </span>
                  )}
                </div>
              </li>
            );
          })}
        </ol>
      ) : null}
      {bottlenecks.length > 0 && (
        <ul className={`space-y-1 ${funnel ? "mt-2" : ""}`}>
          {bottlenecks.slice(0, 3).map((b, i) => {
            const { clean, refs } = splitTextRefs(b);
            return (
              <li key={i} className="flex gap-1.5 text-[13px] leading-relaxed text-ink-soft">
                <span className="shrink-0 text-warn">▸</span>
                <span>
                  {clean}
                  {citeIndex && onCite && (
                    <CitationChips refs={refs} index={citeIndex} onCite={onCite} maxVisible={2} />
                  )}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </Block>
  );
}

/** VALUE CAPTURE（一句话：钱在哪）。内嵌 raw ID 剥离子为 [n]（§16）。 */
export function ValueCapture({ text, citeIndex, onCite }: {
  text?: string;
  citeIndex?: Map<string, number>;
  onCite?: (id: string) => void;
}) {
  if (!text) return null;
  const { clean, refs } = splitTextRefs(text);
  return (
    <Block kicker="Value Capture · 价值捕获">
      <p className="border-l-2 border-accent/60 pl-3 text-sm leading-[1.8] text-ink-soft">
        {clean}
        {citeIndex && onCite && (
          <CitationChips refs={refs} index={citeIndex} onCite={onCite} maxVisible={3} />
        )}
      </p>
    </Block>
  );
}

/** BEST PUBLIC EXPRESSIONS（§7/§19：≤5 行结构化小表 + View all）。 */
export function BestExpressions({ snap, citeIndex, onCite, onViewAll }: {
  snap: DossierSnapshot;
  citeIndex: Map<string, number>;
  onCite: (id: string) => void;
  onViewAll?: () => void;
}) {
  const ca = snap.structures?.candidate_assessment as { candidates?: CandidateItem[] } | undefined;
  const candidates = ca?.candidates ?? [];
  if (!candidates.length) return null;
  const positions = companyPositionMap(
    (snap.structures?.industry_map ?? null) as BusinessGraph | null,
  );
  const top = topCompanies(candidates, 5);
  return (
    <Block kicker="Best Public Expressions · 最佳上市表达">
      <div className="overflow-hidden rounded-card border border-line bg-white">
        <table className="w-full text-[13px]">
          <thead>
            <tr className="border-b border-line bg-paper text-left text-meta text-ink-mute">
              <th className="px-3 py-2 font-medium">公司</th>
              <th className="hidden px-3 py-2 font-medium md:table-cell">环节</th>
              <th className="hidden px-3 py-2 font-medium lg:table-cell">验证阶段</th>
              <th className="px-3 py-2 font-medium">状态</th>
              <th className="w-10 px-3 py-2" />
            </tr>
          </thead>
          <tbody>
            {top.map((c) => {
              const pos = positions.get(c.entity_id.toUpperCase())
                ?? positions.get((c.name || "").toUpperCase()) ?? "";
              const validation = [c.technology_stage, c.commercial_stage].filter(Boolean).join(" · ");
              return (
                <tr key={c.entity_id} className="border-b border-line/60 align-top last:border-0 hover:bg-paper/60">
                  <td className="px-3 py-2.5">
                    <div className="font-medium text-ink">{c.name || c.entity_id}</div>
                    <div className="dos-num text-meta text-ink-faint">
                      {c.entity_id}
                      {c.listing_status === "listed" && c.market ? ` · ${c.market}` : ""}
                      {c.listing_status === "private" ? " · 未上市" : ""}
                    </div>
                  </td>
                  <td className="hidden px-3 py-2.5 text-ink-soft md:table-cell">{pos || "—"}</td>
                  <td className="hidden max-w-[220px] px-3 py-2.5 text-ink-soft lg:table-cell">
                    <span className="line-clamp-2">{validationText(c) || "—"}</span>
                  </td>
                  <td className="px-3 py-2.5">
                    <span className="inline-flex items-center gap-1 text-meta text-ink-soft">
                      <i className={`inline-block h-2 w-2 rounded-full ${TIER_DOT[c.tier] ?? TIER_DOT.needs_review}`} />
                      {TIER_LABEL[c.tier] ?? c.tier}
                    </span>
                  </td>
                  <td className="px-3 py-2.5 text-right">
                    <CitationChips refs={c.evidence_refs} index={citeIndex} onCite={onCite} maxVisible={2} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {candidates.length > top.length && onViewAll && (
          <button onClick={onViewAll}
                  className="block w-full border-t border-line bg-paper/60 px-3 py-2 text-center text-meta font-medium text-accent hover:bg-accent-soft">
            查看全部 {candidates.length} 家 →
          </button>
        )}
      </div>
    </Block>
  );
}

/** CATALYSTS（rail，≤3）。 */
export function Catalysts({ snap, onCite }: {
  snap: DossierSnapshot;
  onCite: (id: string) => void;
}) {
  const vt = snap.structures?.validation_timeline as { items?: ValidationItem[] } | undefined;
  const items = nextCatalysts(vt?.items ?? [], 3);
  if (!items.length) return null;
  return (
    <Block kicker="Catalysts · 催化剂">
      <ol className="space-y-2.5">
        {items.map((it, i) => (
          <li key={i} className="flex gap-2.5">
            <span className="dos-num shrink-0 self-start rounded border border-line bg-paper px-1.5 py-0.5 text-meta font-medium text-accent">
              {(it.window_start || "").slice(0, 7) || "待定"}
            </span>
            <div className="min-w-0">
              <div className="text-[13px] leading-snug text-ink-soft">{it.event}</div>
              {(it.evidence_refs ?? []).length > 0 && (
                <button onClick={() => onCite(it.evidence_refs.find((r) => r.startsWith("ev-")) ?? "")}
                        className="mt-0.5 text-meta text-accent hover:underline">
                  来源 →
                </button>
              )}
            </div>
          </li>
        ))}
      </ol>
    </Block>
  );
}

/** THESIS BREAKERS（§30：什么情况推翻结论；rail，≤3，红色克制使用）。
 *  内嵌 raw ID 剥离子为 [n]（§16）。 */
export function ThesisBreakers({ items, citeIndex, onCite }: {
  items?: string[];
  citeIndex?: Map<string, number>;
  onCite?: (id: string) => void;
}) {
  if (!items?.length) return null;
  return (
    <Block kicker="Thesis Breakers · 证伪条件">
      <ul className="space-y-1.5">
        {items.slice(0, 3).map((t, i) => {
          const { clean, refs } = splitTextRefs(t);
          return (
            <li key={i} className="flex gap-2 text-[13px] leading-relaxed text-ink-soft">
              <span className="mt-0.5 h-1.5 w-1.5 shrink-0 rounded-full bg-risk" />
              <span>
                {clean}
                {citeIndex && onCite && (
                  <CitationChips refs={refs} index={citeIndex} onCite={onCite} maxVisible={2} />
                )}
              </span>
            </li>
          );
        })}
      </ul>
    </Block>
  );
}

/** WHAT CHANGED（§11/§32：研究更新对投资判断的影响，不是 agent 内部操作日志）。
 *  优先消费快照级 change_log（发布时服务端冻结的结构化 diff，§32）；
  *  无 diff（首次发布/旧快照）→ 回退研究轮次的 key_changes 文本分类（现状行为）。 */
export function WhatChanged({ entries, fallback, citeIndex, onCite }: {
  entries?: ChangeLogEntry[];
  fallback?: string[];
  citeIndex?: Map<string, number>;
  onCite?: (id: string) => void;
}) {
  const structured = (entries ?? []).slice(0, 6);
  const legacy = (fallback ?? []).map(classifyChange).slice(0, 5);
  if (!structured.length && !legacy.length) return null;
  const ICON: Record<string, { s: string; cls: string }> = {
    up: { s: "↑", cls: "text-pos" },
    risk: { s: "!", cls: "text-warn" },
    new: { s: "+", cls: "text-accent" },
  };
  return (
    <Block kicker="What Changed · 自上次更新">
      <ul className="space-y-2">
        {structured.length > 0
          ? structured.map((e, i) => (
            <li key={i} className="flex gap-2.5 text-sm leading-relaxed">
              <span className={`dos-num w-4 shrink-0 font-semibold ${(ICON[e.icon] ?? ICON.up).cls}`}>
                {(ICON[e.icon] ?? ICON.up).s}
              </span>
              <div className="min-w-0">
                {e.tag && (
                  <span className="mr-1.5 rounded border border-line bg-paper px-1 py-px text-[11px] text-ink-mute">
                    {e.tag}
                  </span>
                )}
                {/* 文本内嵌 raw ID 剥离子展示（§16）；剥离出的引用与结构化 refs 合并进 chips */}
                {(() => {
                  const { clean, refs } = splitTextRefs(e.text);
                  const allRefs = [...refs, ...(e.refs ?? [])];
                  return (
                    <>
                      <span className="text-ink-soft"><LongText text={clean} maxPx={66} /></span>
                      {citeIndex && onCite && (
                        <CitationChips refs={allRefs} index={citeIndex} onCite={onCite} maxVisible={2} />
                      )}
                    </>
                  );
                })()}
              </div>
            </li>
          ))
          : legacy.map((e, i) => {
            // 回退路径（key_changes 研究结论文本）同样剥离内嵌 raw ID（§16）
            const { clean, refs } = splitTextRefs(e.text);
            return (
              <li key={i} className="flex gap-2.5 text-sm leading-relaxed">
                <span className={`dos-num w-4 shrink-0 font-semibold ${ICON[e.icon].cls}`}>{ICON[e.icon].s}</span>
                <div className="min-w-0">
                  {e.tag && (
                    <span className="mr-1.5 rounded border border-line bg-paper px-1 py-px text-[11px] text-ink-mute">
                      {e.tag}
                    </span>
                  )}
                  <span className="text-ink-soft"><LongText text={clean} maxPx={66} /></span>
                  {citeIndex && onCite && (
                    <CitationChips refs={refs} index={citeIndex} onCite={onCite} maxVisible={2} />
                  )}
                </div>
              </li>
            );
          })}
      </ul>
    </Block>
  );
}

/** KEY DEBATE（最大分歧 + 最大反证；§14 反方义务）。 */
export function KeyDebate({ snap, citeIndex, onCite }: {
  snap: DossierSnapshot;
  citeIndex: Map<string, number>;
  onCite: (id: string) => void;
}) {
  const debate = snap.summary.biggest_disagreement;
  const counter = snap.summary.counter_evidence;
  if (!debate && !counter) return null;
  // 内嵌 raw ID 剥离子为 [n]（§16；剥离出的引用并入 chips，不丢证据链）
  const debateClean = debate ? splitTextRefs(debate) : null;
  const counterClean = counter ? splitTextRefs(counter) : null;
  return (
    <Block kicker="Key Debate · 关键分歧">
      {debateClean && (
        <p className="max-w-[76ch] text-sm leading-[1.8] text-ink">
          {debateClean.clean}
          <CitationChips refs={debateClean.refs} index={citeIndex} onCite={onCite} />
        </p>
      )}
      {counterClean && (
        <div className="mt-2.5 rounded-card border border-warn/30 bg-warn-soft/70 p-3.5">
          <div className="mb-1 text-meta font-semibold text-warn">最大反证（反方义务：主动寻找的反面证据）</div>
          <p className="text-[13px] leading-[1.8] text-ink-soft">
            <LongText text={counterClean.clean} maxPx={88} />
            <CitationChips refs={[...counterClean.refs, ...snap.summary.counter_refs]}
                           index={citeIndex} onCite={onCite} />
          </p>
        </div>
      )}
    </Block>
  );
}

// ---------------- Overview 整页组合（investment_snapshot 模块的渲染器） ----------------

/** 行业/股票档案的 L0+L1 默认视图（§4 股票/行业导航的 Overview）。
 *  布局：主列 2/3 + 右栏 1/3（§48.2：不固定 340px 侧栏，产业链等深读模块独立布局）。 */
export function OverviewPage({ snap, citeRefs, onCite, onViewAllCompanies }: {
  snap: DossierSnapshot;
  /** 参与页面级编号的引用列表（按希望编号的顺序传入） */
  citeRefs: (readonly string[] | undefined)[];
  onCite: (id: string) => void;
  onViewAllCompanies?: () => void;
}) {
  const s = snap.summary;
  // 页面级引用编号（§16：同一页 [n] 一致可追溯）：论文 → 反证 → 指标 → 候选 → 时间线
  const candidates = ((snap.structures?.candidate_assessment as any)?.candidates ?? []) as CandidateItem[];
  const timelineItems = ((snap.structures?.validation_timeline as any)?.items ?? []) as ValidationItem[];
  const citeIndex = buildCitationIndex([
    ...citeRefs,
    s.thesis_refs,
    s.counter_refs,
    ...s.key_metrics.map((m) => m.evidence_refs ?? []),
    ...topCompanies(candidates, 5).map((c) => c.evidence_refs ?? []),
    ...nextCatalysts(timelineItems, 3).map((i) => i.evidence_refs ?? []),
    ...(snap.change_log ?? []).map((e) => e.refs ?? []),
    ...(snap.change_log ?? []).map((e) => splitTextRefs(e.text ?? "").refs),
    // 文本字段内嵌引用的剥离去向（§16：剥离不丢链）
    ...(s.why_now ?? []).map((w) => splitTextRefs(w).refs),
    ...(s.value_capture ? [splitTextRefs(s.value_capture).refs] : []),
    ...(s.thesis_breakers ?? []).map((t) => splitTextRefs(t).refs),
    ...(s.drivers ?? []).slice(0, 5).map((d) => splitTextRefs(d).refs),
    ...(s.biggest_disagreement ? [splitTextRefs(s.biggest_disagreement).refs] : []),
    ...(s.counter_evidence ? [splitTextRefs(s.counter_evidence).refs] : []),
  ]);
  const hasWhyNow = Boolean(s.why_now?.length);
  const graph = (snap.structures?.industry_map ?? null) as BusinessGraph | null;
  const hasBottleneck = Boolean(funnelFromIndustryMap(graph) || s.bottlenecks?.length);

  return (
    <div className="grid gap-6 xl:grid-cols-3">
      {/* ---- 主列（2/3）：投资判断 ---- */}
      <div className="space-y-6 xl:col-span-2">
        <InvestmentView snap={snap} citeIndex={citeIndex} onCite={onCite} />
        <KeyMetricGrid snap={snap} citeIndex={citeIndex} onCite={onCite} />
        {(hasWhyNow || hasBottleneck) && (
          <div className={`grid gap-6 ${hasWhyNow && hasBottleneck ? "md:grid-cols-2" : ""}`}>
            <WhyNow items={s.why_now} citeIndex={citeIndex} onCite={onCite} />
            <Bottleneck snap={snap} citeIndex={citeIndex} onCite={onCite} />
          </div>
        )}
        <ValueCapture text={s.value_capture} citeIndex={citeIndex} onCite={onCite} />
        <BestExpressions snap={snap} citeIndex={citeIndex} onCite={onCite} onViewAll={onViewAllCompanies} />
        <WhatChanged entries={snap.change_log} fallback={s.key_changes}
                     citeIndex={citeIndex} onCite={onCite} />
        <KeyDebate snap={snap} citeIndex={citeIndex} onCite={onCite} />
        {/* 核心依据（原三列长文本之一）：单列 ≤5 条，阅读宽度受限；
            内嵌 raw ID 剥离子为 [n]（§16） */}
        {(s.drivers?.length ?? 0) > 0 && (
          <Block kicker="Supporting Evidence · 核心依据">
            <ul className="max-w-[76ch] space-y-1.5">
              {s.drivers.slice(0, 5).map((d, i) => {
                const { clean, refs } = splitTextRefs(d);
                return (
                  <li key={i} className="flex gap-2 text-sm leading-relaxed text-ink-soft">
                    <span className="shrink-0 text-ink-faint">→</span>
                    <span>
                      <LongText text={clean} maxPx={66} />
                      <CitationChips refs={refs} index={citeIndex} onCite={onCite} maxVisible={2} />
                    </span>
                  </li>
                );
              })}
            </ul>
          </Block>
        )}
      </div>
      {/* ---- 右栏（1/3）：催化 / 证伪 / 缺口 ---- */}
      <div className="space-y-6">
        <Catalysts snap={snap} onCite={onCite} />
        <ThesisBreakers items={s.thesis_breakers} citeIndex={citeIndex} onCite={onCite} />
        {(() => {
          const limits = s.limitations ?? [];
          if (!limits.length) return null;
          return (
            <Block kicker="Open Questions · 未决缺口">
              <ul className="space-y-1.5">
                {limits.slice(0, 4).map((l, i) => (
                  <li key={i} className="flex gap-2 text-[13px] leading-relaxed text-ink-mute">
                    <span className="shrink-0 text-ink-faint">·</span><span>{l}</span>
                  </li>
                ))}
                {limits.length > 4 && (
                  <li className="text-meta text-ink-faint">另 {limits.length - 4} 条见「研究与来源」</li>
                )}
              </ul>
            </Block>
          );
        })()}
      </div>
    </div>
  );
}

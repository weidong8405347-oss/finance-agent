// 可视化语法组件（升级方案 §9/§27/§28/§30）：产业链流图 / 验证时间线 /
// 阶段阶梯 / 分层条 / 排序条形。全部零依赖手绘 SVG/HTML（与 MiniChart 同纪律），
// 布局函数为纯函数（vitest 可测），组件只做渲染与交互。
//
// 数值纪律：
// - 图上数值只来自服务端 typed 数据（comparison_numerics 由冻结观测解析），
//   不从展示字符串（"106,303" "+287.2%"）猜数；
// - 时间线只给可解析日期的事件定位（YYYY / YYYY-MM / YYYY-MM-DD），
//   无日期事件进「未排期」区——不编造位置；
// - 阶段阶梯/分层条只用离散分类（tier/stage 原文），不造精确坐标；
// - 混单位/混币种的列不出图（诚实降级为表格）。

import type {
  CandidateItem, IndustryMapEdge, IndustryMapNode, ProfitPool, QuadrantPayload,
  QuadrantPoint, ValidationItem,
} from "./types";

// ---------------- 产业链流图（§27：Value Chain 从文字变成图） ----------------

export interface ChainNodePos {
  node: IndustryMapNode;
  x: number; y: number; w: number; h: number;
  column: string;
}

export interface ChainEdgePath {
  edge: IndustryMapEdge;
  d: string;
  labelX: number; labelY: number;
  /** 高亮态：无选中节点时全亮；选中后只亮邻接边 */
  lit: (activeNode: string | null) => boolean;
}

export interface ChainLayout {
  columns: { key: string; label: string; x: number; width: number }[];
  nodes: ChainNodePos[];
  edges: ChainEdgePath[];
  width: number;
  height: number;
}

export const CHAIN_NODE_W = 172;
export const CHAIN_NODE_H = 62;
const CHAIN_COL_GAP = 92;
const CHAIN_ROW_GAP = 16;
const CHAIN_PAD = 10;
const CHAIN_HEADER_H = 26;

/** 分层 DAG 布局（确定性）：layers 定列序，节点按出现序落列，边为三次贝塞尔。 */
export function layoutChain(
  nodes: IndustryMapNode[],
  edges: IndustryMapEdge[],
  layers: string[],
  layerLabels?: Record<string, string>,
): ChainLayout {
  const seen: string[] = [];
  for (const n of nodes) {
    const k = n.layer || "";
    if (!seen.includes(k)) seen.push(k);
  }
  // 列序 = 声明的 layers（只含有节点的）+ 节点用到但未声明的层（否则节点不可见）
  const order = [...layers.filter((l) => seen.includes(l)),
                 ...seen.filter((k) => !layers.includes(k))];
  const columns = order.map((key, i) => ({
    key,
    label: (layerLabels && layerLabels[key]) || LAYER_LABELS[key] || key || "未分层",
    x: CHAIN_PAD + i * (CHAIN_NODE_W + CHAIN_COL_GAP),
    width: CHAIN_NODE_W,
  }));
  const pos = new Map<string, ChainNodePos>();
  const placed: ChainNodePos[] = [];
  let height = CHAIN_HEADER_H + CHAIN_PAD * 2;
  for (const col of columns) {
    let y = CHAIN_HEADER_H + CHAIN_PAD;
    for (const n of nodes.filter((x) => (x.layer || "") === col.key)) {
      const p = { node: n, x: col.x, y, w: CHAIN_NODE_W, h: CHAIN_NODE_H, column: col.key };
      pos.set(n.node_id, p);
      placed.push(p);
      y += CHAIN_NODE_H + CHAIN_ROW_GAP;
    }
    height = Math.max(height, y - CHAIN_ROW_GAP + CHAIN_PAD);
  }
  const width = columns.length
    ? CHAIN_PAD * 2 + columns.length * CHAIN_NODE_W + (columns.length - 1) * CHAIN_COL_GAP
    : CHAIN_PAD * 2;
  const paths: ChainEdgePath[] = [];
  for (const edge of edges) {
    const s = pos.get(edge.source);
    const t = pos.get(edge.target);
    if (!s || !t) continue; // 悬空边不画（校验层已拒；防御性跳过）
    const sx = s.x + s.w;
    const sy = s.y + s.h / 2;
    const tx = t.x;
    const ty = t.y + t.h / 2;
    const dx = Math.max(48, Math.abs(tx - sx) / 2);
    const d = tx > sx
      ? `M ${sx} ${sy} C ${sx + dx} ${sy}, ${tx - dx} ${ty}, ${tx} ${ty}`
      // 同列/回边：从源底部绕到目标底部（确定性绕行，不穿卡片）
      : `M ${s.x + s.w / 2} ${s.y + s.h} C ${s.x + s.w / 2} ${s.y + s.h + 40}, `
        + `${t.x + t.w / 2} ${t.y + t.h + 40}, ${t.x + t.w / 2} ${t.y + t.h}`;
    paths.push({
      edge, d,
      labelX: (sx + tx) / 2, labelY: (sy + ty) / 2 - 6,
      lit: (active) => !active || active === edge.source || active === edge.target,
    });
  }
  return { columns, nodes: placed, edges: paths, width, height };
}

export const LAYER_LABELS: Record<string, string> = {
  upstream: "上游", midstream: "中游", downstream: "下游",
  platform: "平台", application: "应用", infrastructure: "基础设施",
  demand: "需求端",
};

export const RELATION_LABELS: Record<string, string> = {
  supplies: "供给", competes: "竞争", substitutes: "替代",
  depends_on: "依赖", enables: "支撑", value_flow: "价值流",
};

/** 关系 → 边样式（规范词有着色；未知关系灰实线，标签显示原文）。 */
export const RELATION_STYLE: Record<string, { color: string; dash?: string }> = {
  supplies: { color: "#52525b" },
  competes: { color: "#dc2626", dash: "5 3" },
  substitutes: { color: "#ea580c", dash: "5 3" },
  depends_on: { color: "#2563eb" },
  enables: { color: "#0d9488" },
  value_flow: { color: "#7c3aed" },
};

export function relationLabel(relation: string): string {
  return RELATION_LABELS[relation] ?? relation;
}

interface ChainGraphProps {
  nodes: IndustryMapNode[];
  edges: IndustryMapEdge[];
  layers: string[];
  layerLabels?: Record<string, string>;
  activeNode: string | null;
  onNodeClick: (nodeId: string) => void;
  onCompanyClick: (companyId: string) => void;
  onEvidenceClick: (evidenceId: string) => void;
  highlightCompany?: string;
}

/** 产业链流图：SVG 边层 + HTML 节点卡（chips/键盘可达）。选中节点 → 详情面板。 */
export function ValueChainGraph({
  nodes, edges, layers, layerLabels, activeNode, onNodeClick,
  onCompanyClick, onEvidenceClick, highlightCompany,
}: ChainGraphProps) {
  const layout = layoutChain(nodes, edges, layers, layerLabels);
  const selected = nodes.find((n) => n.node_id === activeNode) ?? null;
  const hl = (highlightCompany ?? "").toUpperCase();
  const hasCompany = (n: IndustryMapNode) =>
    (n.company_refs ?? []).some((c) => c.toUpperCase() === hl);
  // 瓶颈红标只在可分辨时渲染：全部节点都是瓶颈 = 没有瓶颈（红色失效为噪音），
  // 此时瓶颈语义由 bottlenecks 文本列表承担（不隐藏数据，只避免全红）
  const bottleneckCount = nodes.filter((n) => n.bottleneck).length;
  const discriminate = bottleneckCount > 0 && bottleneckCount < nodes.length;
  const showBottleneck = (n: IndustryMapNode) => discriminate && n.bottleneck;
  return (
    <div>
      <div className="overflow-x-auto rounded border border-neutral-200 bg-neutral-50/40">
        <div className="relative" style={{ width: layout.width, height: layout.height, minWidth: "100%" }}>
          <svg
            className="absolute inset-0" width={layout.width} height={layout.height}
            aria-hidden="true" focusable="false"
          >
            <defs>
              <marker id="chain-arrow" viewBox="0 0 8 8" refX="7" refY="4"
                      markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                <path d="M 0 0 L 8 4 L 0 8 z" fill="#71717a" />
              </marker>
            </defs>
            {layout.edges.map((e, i) => {
              const style = RELATION_STYLE[e.edge.relation] ?? { color: "#a1a1aa" };
              const lit = e.lit(activeNode);
              return (
                <g key={i} opacity={lit ? 1 : 0.18}>
                  <path d={e.d} fill="none" stroke={style.color} strokeWidth={1.6}
                        strokeDasharray={style.dash} markerEnd="url(#chain-arrow)" />
                  <text x={e.labelX} y={e.labelY} textAnchor="middle" fontSize="10"
                        fill={style.color} stroke="#fafafa" strokeWidth="3"
                        paintOrder="stroke" style={{ pointerEvents: "none" }}>
                    {relationLabel(e.edge.relation)}
                  </text>
                </g>
              );
            })}
          </svg>
          {layout.columns.map((c) => (
            <div key={c.key} className="absolute top-1 text-[11px] font-semibold text-neutral-500"
                 style={{ left: c.x, width: c.width }}>
              {c.label}
            </div>
          ))}
          {layout.nodes.map((np) => {
            const n = np.node;
            const isActive = activeNode === n.node_id;
            return (
              <button
                key={n.node_id}
                onClick={() => onNodeClick(isActive ? "" : n.node_id)}
                aria-pressed={isActive}
                title={n.note || n.label}
                className={`absolute rounded-lg border bg-white p-2 text-left shadow-sm transition-shadow ${
                  hasCompany(n) ? "border-blue-400 ring-1 ring-blue-300"
                  : showBottleneck(n) ? "border-red-300"
                  : "border-neutral-300"
                } ${isActive ? "ring-2 ring-neutral-800" : "hover:border-neutral-500"}`}
                style={{ left: np.x, top: np.y, width: np.w, height: np.h }}
              >
                <div className="flex items-start gap-1">
                  <span className="line-clamp-2 text-xs font-medium leading-tight text-neutral-800">
                    {n.label}
                  </span>
                  {showBottleneck(n) && (
                    <span className="mt-px shrink-0 rounded bg-red-100 px-1 text-[9px] text-red-700">瓶颈</span>
                  )}
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-1 font-mono text-[9px] text-neutral-400">
                  {(n.company_refs ?? []).length > 0 && <span>{n.company_refs.length} 家公司</span>}
                  {(n.evidence_refs ?? []).length > 0 && <span>{n.evidence_refs.length} 来源</span>}
                  {hasCompany(n) && <span className="rounded bg-blue-100 px-1 text-blue-700">已定位</span>}
                </div>
              </button>
            );
          })}
        </div>
      </div>
      {selected && (
        <div className="mt-2 rounded-lg border border-neutral-200 bg-white p-3">
          <div className="mb-1 flex flex-wrap items-center gap-2">
            <span className="text-xs font-semibold text-neutral-800">{selected.label}</span>
            {showBottleneck(selected) && (
              <span className="rounded bg-red-50 px-1.5 py-0.5 text-[10px] text-red-700">瓶颈环节</span>
            )}
            <button onClick={() => onNodeClick("")}
                    className="ml-auto text-[11px] text-neutral-400 hover:text-neutral-600">
              取消选中 ✕
            </button>
          </div>
          {selected.note && <div className="mb-1.5 text-[11px] text-neutral-600">{selected.note}</div>}
          {(selected.company_refs ?? []).length > 0 && (
            <div className="mb-1.5 flex flex-wrap gap-1">
              {selected.company_refs.map((c) => (
                <button key={c} onClick={() => onCompanyClick(c)}
                        title="在「公司与护城河」里定位这一行"
                        className="rounded border border-neutral-200 px-1.5 py-0.5 font-mono text-[10px] text-neutral-600 hover:border-blue-300 hover:bg-blue-50 hover:text-blue-700">
                  {c}
                </button>
              ))}
            </div>
          )}
          <EvidenceChipsRow refs={selected.evidence_refs} onEvidenceClick={onEvidenceClick} />
        </div>
      )}
      <div className="mt-1 text-[10px] text-neutral-400">
        边等宽 = 流量未知（不做假 Sankey）；虚线 = 竞争/替代关系。节点可点击聚焦邻接边，
        键盘 Tab 可达；完整节点/关系清单见下方数据表。
      </div>
    </div>
  );
}

function EvidenceChipsRow({ refs, onEvidenceClick }: {
  refs?: string[]; onEvidenceClick: (id: string) => void;
}) {
  const list = (refs ?? []).filter(Boolean);
  if (!list.length) return null;
  return (
    <span className="flex flex-wrap gap-1 font-mono text-[10px]">
      {list.map((r) => (
        r.startsWith("ev-")
          ? <button key={r} onClick={() => onEvidenceClick(r)}
                    className="rounded border border-neutral-200 px-1 py-0.5 text-neutral-600 hover:border-neutral-400">{r}</button>
          : <span key={r} className="rounded border border-neutral-100 px-1 py-0.5 text-neutral-400">{r}</span>
      ))}
    </span>
  );
}

// ---------------- 验证时间线（§9：Catalysts → Timeline） ----------------

export interface TimelineRow {
  item: ValidationItem;
  x1: number;
  x2: number;
  y: number;
}

export interface TimelineScale {
  dated: TimelineRow[];
  undated: ValidationItem[];
  ticks: { x: number; label: string }[];
  todayX: number | null;
  width: number;
  height: number;
}

const TL_LABEL_W = 210;
const TL_ROW_H = 30;
const TL_AXIS_H = 26;
const TL_PAD_R = 16;

/** 部分日期解析（确定性）：YYYY / YYYY-MM / YYYY-MM-DD → UTC 毫秒；其余 null。 */
export function parseWindowDate(s: string | null | undefined, endOfMonth = false): number | null {
  if (!s) return null;
  const t = s.trim();
  let m = /^(\d{4})$/.exec(t);
  if (m) {
    const y = Number(m[1]);
    return Date.UTC(y, endOfMonth ? 11 : 0, endOfMonth ? 31 : 1);
  }
  m = /^(\d{4})-(\d{2})$/.exec(t);
  if (m) {
    const y = Number(m[1]);
    const mo = Number(m[2]);
    if (mo < 1 || mo > 12) return null;
    const lastDay = new Date(Date.UTC(y, mo, 0)).getUTCDate();
    return Date.UTC(y, mo - 1, endOfMonth ? lastDay : 1);
  }
  m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(t);
  if (m) {
    const d = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    return Number.isNaN(d) ? null : d;
  }
  return null; // 其它形态不猜（进未排期区）
}

/** 时间线布局：可解析日期的事件上轴（每事件一道），其余进 undated（不编造位置）。 */
export function timelineScale(items: ValidationItem[], width: number, now = Date.now()): TimelineScale {
  const parsed = items.map((item) => {
    const start = parseWindowDate(item.window_start);
    const end = parseWindowDate(item.window_end, true) ?? start;
    return { item, start, end: end ?? start };
  });
  const dated = parsed.filter((p) => p.start !== null);
  const undated = parsed.filter((p) => p.start === null).map((p) => p.item);
  if (!dated.length) {
    return { dated: [], undated, ticks: [], todayX: null, width, height: TL_AXIS_H };
  }
  let minT = Math.min(...dated.map((p) => p.start!));
  let maxT = Math.max(...dated.map((p) => p.end!));
  // 「今天」只加标记线不改域：域外不强行拉伸（避免事件挤在一角）
  if (minT === maxT) maxT = minT + 86400000 * 365;
  const pad = (maxT - minT) * 0.03;
  const lo = minT - pad;
  const hi = maxT + pad;
  const plotW = Math.max(120, width - TL_LABEL_W - TL_PAD_R);
  const x = (t: number) => TL_LABEL_W + ((t - lo) / (hi - lo)) * plotW;
  const rows: TimelineRow[] = dated.map((p, i) => ({
    item: p.item,
    x1: x(p.start!),
    x2: x(Math.max(p.end!, p.start!)),
    y: i * TL_ROW_H + TL_ROW_H / 2,
  }));
  // 年刻度（确定性：域内每年 1 月 1 日）
  const ticks: { x: number; label: string }[] = [];
  const y0 = new Date(lo).getUTCFullYear();
  const y1 = new Date(hi).getUTCFullYear();
  for (let y = y0; y <= y1; y++) {
    const t = Date.UTC(y, 0, 1);
    if (t >= lo && t <= hi) ticks.push({ x: x(t), label: String(y) });
  }
  const todayX = now >= lo && now <= hi ? x(now) : null;
  return {
    dated: rows, undated, ticks, todayX,
    width, height: rows.length * TL_ROW_H + TL_AXIS_H,
  };
}

const STATUS_COLOR: Record<string, string> = {
  occurred: "#16a34a", expected: "#2563eb", unknown: "#a1a1aa",
};

export function TimelineStrip({ items, width = 860 }: { items: ValidationItem[]; width?: number }) {
  const scale = timelineScale(items, width);
  if (!scale.dated.length) return null; // 全无可解析日期 → 只用列表（不编造位置）
  return (
    <div className="overflow-x-auto rounded-lg border border-neutral-200 bg-white p-2">
      <svg width={scale.width} height={scale.height} role="img"
           aria-label={`验证时间线（${scale.dated.length} 项已排期，${scale.undated.length} 项未排期见下方清单）`}
           style={{ minWidth: 640 }}>
        {scale.ticks.map((t) => (
          <g key={t.label}>
            <line x1={t.x} y1={4} x2={t.x} y2={scale.height - TL_AXIS_H + 4}
                  stroke="#f4f4f5" strokeWidth={1} />
            <text x={t.x} y={scale.height - 8} fontSize="10" fill="#a1a1aa"
                  textAnchor="middle" fontFamily="monospace">{t.label}</text>
          </g>
        ))}
        {scale.todayX !== null && (
          <g>
            <line x1={scale.todayX} y1={4} x2={scale.todayX} y2={scale.height - TL_AXIS_H + 4}
                  stroke="#171717" strokeWidth={1} strokeDasharray="3 3" />
            <text x={scale.todayX + 3} y={12} fontSize="9" fill="#171717">今天</text>
          </g>
        )}
        {scale.dated.map((r, i) => {
          const color = STATUS_COLOR[r.item.status] ?? STATUS_COLOR.unknown;
          const single = Math.abs(r.x2 - r.x1) < 3;
          const label = r.item.event.length > 17 ? `${r.item.event.slice(0, 17)}…` : r.item.event;
          const window = `${r.item.window_start || "?"} → ${r.item.window_end || "?"}`;
          return (
            <g key={i}>
              <title>{`${r.item.event}（${window}）\n触发条件：${r.item.trigger_condition}`}</title>
              <text x={TL_LABEL_W - 8} y={r.y + 3} fontSize="10.5" fill="#404040" textAnchor="end">
                {label}
              </text>
              {single ? (
                <rect x={r.x1 - 4} y={r.y - 4} width={8} height={8} rx={1.5}
                      fill={color} transform={`rotate(45 ${r.x1} ${r.y})`} />
              ) : (
                <rect x={r.x1} y={r.y - 5} width={r.x2 - r.x1} height={10} rx={5}
                      fill={color} opacity={0.75} />
              )}
            </g>
          );
        })}
      </svg>
      <div className="mt-1 flex flex-wrap gap-3 px-1 text-[10px] text-neutral-500">
        <span><i className="mr-1 inline-block h-2 w-2 rounded-full bg-green-600" />已发生</span>
        <span><i className="mr-1 inline-block h-2 w-2 rounded-full bg-blue-600" />预计（窗口）</span>
        <span><i className="mr-1 inline-block h-2 w-2 rounded-full bg-neutral-400" />时间未知</span>
        {scale.undated.length > 0 && <span>未排期 {scale.undated.length} 项（见下方清单，不编造位置）</span>}
      </div>
    </div>
  );
}

// ---------------- 候选分层条 + 阶段阶梯（§16/§28 的诚实版） ----------------

export const TIER_ORDER = ["included", "watchlist", "needs_review", "excluded"] as const;

export const TIER_META: Record<string, { label: string; chip: string; bar: string }> = {
  included: { label: "入选", chip: "bg-green-50 text-green-700 border-green-200", bar: "bg-green-600" },
  watchlist: { label: "观察", chip: "bg-blue-50 text-blue-700 border-blue-200", bar: "bg-blue-500" },
  needs_review: { label: "待核实", chip: "bg-amber-50 text-amber-700 border-amber-200", bar: "bg-amber-400" },
  excluded: { label: "淘汰", chip: "bg-neutral-100 text-neutral-500 border-neutral-200", bar: "bg-neutral-300" },
};

export function tierCounts(candidates: CandidateItem[]): { tier: string; count: number }[] {
  const counts = new Map<string, number>();
  for (const c of candidates) counts.set(c.tier, (counts.get(c.tier) ?? 0) + 1);
  const known = TIER_ORDER.filter((t) => counts.has(t)).map((t) => ({ tier: t as string, count: counts.get(t as string)! }));
  const unknown = [...counts.entries()].filter(([t]) => !(TIER_ORDER as readonly string[]).includes(t))
    .map(([tier, count]) => ({ tier, count }));
  return [...known, ...unknown];
}

/** 分层占比条：宽度 = 数量占比（确定性，可点击过滤）。 */
export function TierStrip({ candidates, active, onSelect }: {
  candidates: CandidateItem[];
  active: string;
  onSelect: (tier: string) => void;
}) {
  const counts = tierCounts(candidates);
  const total = candidates.length || 1;
  if (!counts.length) return null;
  return (
    <div>
      <div className="flex h-2.5 w-full overflow-hidden rounded-full border border-neutral-200">
        {counts.map(({ tier, count }) => (
          <button key={tier} onClick={() => onSelect(active === tier ? "all" : tier)}
                  title={`${TIER_META[tier]?.label ?? tier} ${count} 家（点击过滤）`}
                  className={TIER_META[tier]?.bar ?? "bg-neutral-400"}
                  style={{ width: `${(count / total) * 100}%` }}
                  aria-label={`${TIER_META[tier]?.label ?? tier} ${count} 家`} />
        ))}
      </div>
      <div className="mt-1 flex flex-wrap gap-2 text-[10px] text-neutral-500">
        {counts.map(({ tier, count }) => (
          <button key={tier} onClick={() => onSelect(active === tier ? "all" : tier)}
                  className={`flex items-center gap-1 hover:text-neutral-800 ${active === tier ? "font-semibold text-neutral-800" : ""}`}>
            <i className={`inline-block h-2 w-2 rounded-sm ${TIER_META[tier]?.bar ?? "bg-neutral-400"}`} />
            {TIER_META[tier]?.label ?? tier} {count}
          </button>
        ))}
      </div>
    </div>
  );
}

/** 技术验证阶段的规范序（离散桶，不是评分）；未知阶段按出现序排后。 */
export const CANONICAL_STAGE_ORDER = ["early", "preclinical", "clinical", "commercial", "mature"];

export function stageGroups(candidates: CandidateItem[]): {
  stage: string; items: CandidateItem[];
}[] {
  const byStage = new Map<string, CandidateItem[]>();
  for (const c of candidates) {
    const s = (c.technology_stage || "").trim();
    if (!s) continue;
    byStage.set(s, [...(byStage.get(s) ?? []), c]);
  }
  const ordered = [
    ...CANONICAL_STAGE_ORDER.filter((s) => byStage.has(s)),
    ...[...byStage.keys()].filter((s) => !CANONICAL_STAGE_ORDER.includes(s)),
  ];
  return ordered.map((stage) => ({ stage, items: byStage.get(stage)! }));
}

/** 阶段阶梯：离散列 + tier 着色 chips（不造连续坐标，§28 的诚实版）。 */
export function StageLadder({ candidates, onLocate }: {
  candidates: CandidateItem[];
  onLocate: (entityId: string) => void;
}) {
  const groups = stageGroups(candidates);
  if (groups.length < 2) return null; // 单阶段无阶梯意义
  return (
    <div className="rounded-lg border border-neutral-200 bg-white p-3">
      <div className="mb-2 text-xs font-semibold text-neutral-500">
        技术验证阶梯（离散阶段，不是评分；点击公司在下表定位）
      </div>
      <div className="flex flex-wrap items-stretch gap-1">
        {groups.map((g, i) => (
          <div key={g.stage} className="flex items-stretch gap-1">
            {i > 0 && <div className="self-center text-neutral-300">→</div>}
            <div className="min-w-[128px] flex-1 rounded border border-neutral-100 bg-neutral-50/60 p-2">
              <div className="mb-1 flex items-baseline justify-between">
                <span className="font-mono text-[11px] font-semibold text-neutral-600">{g.stage}</span>
                <span className="text-[10px] text-neutral-400">{g.items.length}</span>
              </div>
              <div className="flex flex-wrap gap-1">
                {g.items.map((c) => (
                  <button key={c.entity_id} onClick={() => onLocate(c.entity_id)}
                          title={`${c.name || c.entity_id}（${TIER_META[c.tier]?.label ?? c.tier}）`}
                          className={`rounded-full border px-1.5 py-0.5 text-[10px] ${TIER_META[c.tier]?.chip ?? TIER_META.needs_review.chip}`}>
                    {c.name || c.entity_id}
                  </button>
                ))}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------- 对照矩阵排序条形（§9：Peer Comparison → Ranked Bar） ----------------

export interface NumericCell {
  value: string;
  unit: string;
  currency: string | null;
  observation_id: string;
  raw_text?: string;
}

export interface RankedBarColumn {
  colId: string;
  title: string;
  unit: string;
  items: { label: string; cell: NumericCell; value: number }[];
}

/** 十进制字符串 → 绘图 number（与 charts.toChartNumber 同纪律，独立实现避免循环依赖）。 */
export function toBarNumber(value: string | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  if (!/^-?\d+(\.\d+)?$/.test(value.trim())) return null;
  const n = Number(value);
  return Number.isFinite(n) && Math.abs(n) <= Number.MAX_SAFE_INTEGER ? n : null;
}

/** 可绘图列判定（诚实护栏）：矩阵 chartable、行可比、≥2 个数值、
 *  全列同 unit+currency、且列声明单位与观测单位一致——任一不满足 → 只给表。 */
export function rankedBarColumns(
  columns: Record<string, any>[],
  rows: Record<string, any>[],
  numerics: { label: string; cells: Record<string, NumericCell> }[],
  chartable: boolean,
): RankedBarColumn[] {
  if (!chartable) return [];
  const out: RankedBarColumn[] = [];
  for (const col of columns) {
    const colId = String(col?.id ?? "");
    if (!colId) continue;
    const items: { label: string; cell: NumericCell; value: number }[] = [];
    let uniformUnit = "";
    let uniformCur: string | null = null;
    let unitOk = true;
    rows.forEach((row, i) => {
      if (!row || row.comparable === false) return;
      const cell = numerics[i]?.cells?.[colId];
      if (!cell) return;
      const v = toBarNumber(cell.value);
      if (v === null) return;
      if (!uniformUnit) { uniformUnit = cell.unit; uniformCur = cell.currency ?? null; }
      if (cell.unit !== uniformUnit || (cell.currency ?? null) !== uniformCur) unitOk = false;
      items.push({ label: String(row.label ?? numerics[i]?.label ?? ""), cell, value: v });
    });
    const colUnit = String(col?.unit ?? "").trim();
    const colUnitMatches = !colUnit || colUnit === "-" || colUnit === uniformUnit;
    if (items.length >= 2 && unitOk && colUnitMatches) {
      out.push({
        colId,
        title: `${String(col?.label ?? colId)}${uniformUnit ? `（${uniformUnit}${uniformCur ? `·${uniformCur}` : ""}）` : ""}`,
        unit: uniformUnit,
        items: [...items].sort((a, b) => b.value - a.value),
      });
    }
  }
  return out;
}

export function RankedBars({ cols, formatValue }: {
  cols: RankedBarColumn[];
  formatValue: (value: string, unit: string, currency?: string | null, rawText?: string) => string;
}) {
  if (!cols.length) return null;
  return (
    <div className="grid gap-3 md:grid-cols-2">
      {cols.map((col) => {
        const max = Math.max(...col.items.map((i) => Math.abs(i.value))) || 1;
        return (
          <div key={col.colId} className="rounded-lg border border-neutral-200 bg-white p-3">
            <div className="mb-2 text-xs font-semibold text-neutral-600">{col.title}</div>
            <div className="space-y-1.5">
              {col.items.map((it) => (
                <div key={it.label} className="flex items-center gap-2 text-[11px]">
                  <span className="w-40 shrink-0 truncate text-neutral-600" title={it.label}>
                    {it.label}
                  </span>
                  <span className="h-3 flex-1 overflow-hidden rounded-sm bg-neutral-100">
                    <span className={`block h-full rounded-sm ${it.value < 0 ? "bg-red-400" : "bg-neutral-700"}`}
                          style={{ width: `${(Math.abs(it.value) / max) * 100}%` }} />
                  </span>
                  <span className="w-24 shrink-0 text-right font-mono tabular-nums text-neutral-800"
                        title={`观测 ${it.cell.observation_id}${it.cell.raw_text ? ` · 原文「${it.cell.raw_text}」` : ""}`}>
                    {formatValue(it.cell.value, it.cell.unit, it.cell.currency, it.cell.raw_text)}
                  </span>
                </div>
              ))}
            </div>
            <div className="mt-1.5 text-[10px] text-neutral-400">
              数值来自各行引用的 typed 观测（悬停看 observation_id 与披露原文）；同列同单位同币种才出图。
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ---------------- 候选四象限（§20：Y=商业验证 × X=技术/护城河，离散网格散点） ----------------

export interface QuadrantCell {
  x: number;
  y: number;
  points: QuadrantPoint[];
}

/** 网格布局（纯函数）：points → 5×5 单元格；同格多家公司共存（不聚合不 jitter）。
 *  返回按 y 降序（商业验证高者在上）× x 升序排列的单元格行。 */
export function quadrantCells(payload: QuadrantPayload): QuadrantCell[][] {
  const nx = payload.x_axis.order.length;
  const ny = payload.y_axis.order.length;
  const grid = new Map<string, QuadrantPoint[]>();
  for (const p of payload.points) {
    const key = `${p.x},${p.y}`;
    grid.set(key, [...(grid.get(key) ?? []), p]);
  }
  const rows: QuadrantCell[][] = [];
  for (let y = ny - 1; y >= 0; y--) {
    const row: QuadrantCell[] = [];
    for (let x = 0; x < nx; x++) {
      row.push({ x, y, points: grid.get(`${x},${y}`) ?? [] });
    }
    rows.push(row);
  }
  return rows;
}

/** 候选四象限散点（§20）：离散序网格，不是连续坐标——一眼区分「好公司」与
 *  「好股票」（技术验证靠右 ≠ 商业验证靠上）。气泡按 tier 着色；点击定位候选表行。 */
export function CandidateQuadrant({ payload, onLocate }: {
  payload: QuadrantPayload;
  onLocate: (entityId: string) => void;
}) {
  const rows = quadrantCells(payload);
  const { x_axis: xa, y_axis: ya } = payload;
  if (!payload.points.length) return null;
  return (
    <div className="rounded-lg border border-neutral-200 bg-white p-3">
      <div className="mb-2 flex flex-wrap items-baseline gap-2">
        <span className="text-xs font-semibold text-neutral-500">
          候选四象限：{ya.title}（纵）× {xa.title}（横）
        </span>
        <span className="text-[10px] text-neutral-400">
          离散阶段序，不是评分；点公司芯片在下方表中定位
        </span>
      </div>
      <div className="overflow-x-auto">
        <div className="min-w-[640px]">
          {rows.map((row) => {
            const y = row[0]?.y ?? 0;
            const stageKey = ya.order[y] ?? "";
            return (
              <div key={y} className="flex items-stretch gap-1 border-b border-neutral-100 last:border-0">
                <div className="flex w-24 shrink-0 flex-col justify-center py-1 pr-1 text-right">
                  <span className="text-[10px] font-medium text-neutral-500">
                    {ya.labels[stageKey] ?? stageKey}
                  </span>
                  <span className="font-mono text-[9px] text-neutral-300">{stageKey}</span>
                </div>
                {row.map((cell) => (
                  <div key={cell.x}
                       className={`min-h-[44px] flex-1 rounded-sm border border-neutral-100 p-1 ${
                         cell.points.length ? "bg-neutral-50/70" : "bg-white"
                       }`}>
                    <div className="flex flex-wrap gap-1">
                      {cell.points.map((p) => (
                        <button key={p.entity_id} onClick={() => onLocate(p.entity_id)}
                                title={`${p.name || p.entity_id}（${TIER_META[p.tier]?.label ?? p.tier}）\n`
                                  + `技术=${p.x_stage} · 商业=${p.y_stage} · 证据 ${p.evidence_count} 条`}
                                className={`rounded-full border px-1.5 py-0.5 text-[10px] ${
                                  TIER_META[p.tier]?.chip ?? TIER_META.needs_review.chip
                                }`}>
                          {p.name || p.entity_id}
                        </button>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            );
          })}
          <div className="mt-1 flex gap-1">
            <div className="w-24 shrink-0" />
            {xa.order.map((k, x) => (
              <div key={k} className="flex-1 text-center">
                <div className="text-[10px] font-medium text-neutral-500">{xa.labels[k] ?? k}</div>
                <div className="font-mono text-[9px] text-neutral-300">{x}</div>
              </div>
            ))}
          </div>
        </div>
      </div>
      {payload.unpositioned.length > 0 && (
        <div className="mt-2 rounded border border-dashed border-neutral-200 bg-neutral-50/60 p-2">
          <div className="mb-1 text-[10px] font-semibold text-neutral-500">
            未定位 {payload.unpositioned.length} 家（阶段未判定/无法映射规范序——不塞进图里）
          </div>
          <div className="flex flex-wrap gap-1">
            {payload.unpositioned.map((u) => (
              <button key={u.entity_id} onClick={() => onLocate(u.entity_id)} title={u.reason}
                      className="rounded-full border border-dashed border-neutral-300 px-1.5 py-0.5 text-[10px] text-neutral-500 hover:border-neutral-400">
                {u.name || u.entity_id}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------- Profit Pool 堆叠条（§22/§48.4：定量份额才画，单源标 Estimated） ----------------

const POOL_COLORS = ["#1d4ed8", "#0d9488", "#7c3aed", "#b45309", "#52525b", "#b91c1c"];

/** 份额字符串 → 绘图 number（十进制契约，只在绘图边界转换）。 */
export function shareToNumber(share: string): number | null {
  if (!/^\d+(\.\d+)?$/.test(share.trim())) return null;
  const n = Number(share);
  return Number.isFinite(n) && n >= 0 ? n : null;
}

export function ProfitPoolBar({ pool, onEvidenceClick }: {
  pool: ProfitPool;
  onEvidenceClick?: (id: string) => void;
}) {
  const entries = pool.entries
    .map((e) => ({ ...e, n: shareToNumber(e.share) }))
    .filter((e): e is typeof e & { n: number } => e.n !== null);
  if (!entries.length) return null;
  // 宽度基准 = 全池 100%（不归一化到合计）：合计 <100% 时右侧留空 = 「未覆盖」可见；
  // 合计 >100%（口径重叠）时超出部分被裁——合计偏离已在 notes 显式说明，不改写原始数字
  const total = entries.reduce((s, e) => s + e.n, 0);
  return (
    <div className="rounded-lg border border-neutral-200 bg-white p-3">
      <div className="mb-2 flex flex-wrap items-baseline gap-2">
        <span className="text-xs font-semibold text-neutral-600">利润池份额（客户支出流向）</span>
        <span className="text-[10px] text-neutral-400">
          份额为来源原文百分数（全池=100%）；单一来源标 Estimated / Directional（§48.4）
        </span>
      </div>
      <div className="flex h-8 w-full overflow-hidden rounded-md border border-neutral-200 bg-neutral-50">
        {entries.map((e, i) => (
          <div key={e.node}
                 className={`relative flex items-center justify-center ${e.estimated ? "opacity-70" : ""}`}
                 style={{
                   width: `${Math.min(e.n, 100)}%`,  // 份额 = 全池百分数（不归一化）
                   backgroundColor: POOL_COLORS[i % POOL_COLORS.length],
                   // Estimated：斜纹纹理（视觉不确定性，不画成精确份额）
                   backgroundImage: e.estimated
                     ? "repeating-linear-gradient(45deg, transparent, transparent 4px, rgba(255,255,255,0.35) 4px, rgba(255,255,255,0.35) 8px)"
                     : undefined,
                 }}
                 title={`${e.label}：${e.share}%${e.estimated ? "（Estimated / Directional）" : ""}${e.note ? `\n${e.note}` : ""}`}>
            {e.n >= 7 && (
              <span className="px-1 text-[10px] font-medium text-white">
                {e.share}%{e.estimated ? " ᴱ" : ""}
              </span>
            )}
          </div>
        ))}
        {total < 100 && (
          <div className="flex flex-1 items-center justify-center text-[10px] text-neutral-400"
                 title={`份额合计 ${pool.total_share}%，其余 ${(100 - total).toFixed(1)}% 未见定量证据——不虚构「其他」段`}>
            未覆盖 {(100 - total).toFixed(0)}%
          </div>
        )}
      </div>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-neutral-600">
        {entries.map((e, i) => (
          <span key={e.node} className="inline-flex items-center gap-1.5">
            <i className="inline-block h-2.5 w-2.5 rounded-sm"
               style={{ backgroundColor: POOL_COLORS[i % POOL_COLORS.length] }} />
            {e.label} <span className="font-mono tabular-nums">{e.share}%</span>
            {e.estimated && <span className="text-amber-700" title="单一来源份额">Est.</span>}
            {onEvidenceClick && e.evidence_refs.length > 0 && (
              <button onClick={() => onEvidenceClick(e.evidence_refs[0])}
                      className="text-blue-700 hover:underline">
                来源 →
              </button>
            )}
          </span>
        ))}
      </div>
      {pool.notes.map((n, i) => (
        <div key={i} className="mt-1 text-[10px] text-amber-800">· {n}</div>
      ))}
    </div>
  );
}

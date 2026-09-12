// 可视化语法纯函数纪律（升级方案 §9/§27/§28/§30）：
// - 时间线只给可解析日期的事件定位（不编造位置）；
// - 排序条形只画同单位同币种的可比列（混口径 → 只给表）；
// - 产业链布局确定性（列序=layers，缺层补齐，悬空边不画）；
// - 阶段阶梯/分层条只用离散分类原文（不造连续坐标）。
import { describe, expect, it } from "vitest";

import {
  CANONICAL_STAGE_ORDER, layoutChain, parseWindowDate, rankedBarColumns,
  stageGroups, tierCounts, timelineScale, toBarNumber,
} from "../viz";
import type { CandidateItem, IndustryMapEdge, IndustryMapNode, ValidationItem } from "../types";

const node = (id: string, layer: string, over: Partial<IndustryMapNode> = {}): IndustryMapNode => ({
  node_id: id, label: id, layer, company_refs: [], bottleneck: false,
  note: "", evidence_refs: [], ...over,
});
const edge = (s: string, t: string, over: Partial<IndustryMapEdge> = {}): IndustryMapEdge => ({
  source: s, target: t, relation: "supplies", flow_known: false, flow_value: null,
  note: "", evidence_refs: [], ...over,
});
const cand = (id: string, over: Partial<CandidateItem> = {}): CandidateItem => ({
  entity_id: id, name: id, listing_status: "unknown", market: "", security_relation: "",
  tier: "needs_review", technology_stage: "", commercial_stage: "",
  moat_evidence: [], commercial_evidence: [], sustainability_evidence: [],
  counter_evidence: [], reason: "", next_validation: "", evidence_refs: [],
  investable: null, ...over,
});
const vitem = (event: string, over: Partial<ValidationItem> = {}): ValidationItem => ({
  event, window_start: "", window_end: "", status: "unknown",
  trigger_condition: "", affected_judgment: "", company_refs: [], evidence_refs: [], ...over,
});

describe("layoutChain（产业链流图布局）", () => {
  it("列序按 layers，节点落各自列，坐标确定性", () => {
    const l = layoutChain(
      [node("a", "upstream"), node("b", "midstream"), node("c", "upstream")],
      [edge("a", "b")],
      ["upstream", "midstream"],
    );
    expect(l.columns.map((c) => c.key)).toEqual(["upstream", "midstream"]);
    const a = l.nodes.find((n) => n.node.node_id === "a")!;
    const c = l.nodes.find((n) => n.node.node_id === "c")!;
    expect(a.x).toBe(c.x);          // 同列同 x
    expect(a.y).not.toBe(c.y);      // 列内堆叠
    expect(l.edges).toHaveLength(1);
    expect(l.edges[0].d).toMatch(/^M .* C /); // 贝塞尔路径
  });

  it("节点用了未声明的层 → 补列（否则节点不可见）", () => {
    const l = layoutChain([node("d", "demand")], [], ["upstream"]);
    expect(l.columns.map((c) => c.key)).toContain("demand");
    expect(l.nodes).toHaveLength(1);
  });

  it("layer_labels 优先做列头显示名", () => {
    const l = layoutChain([node("a", "upstream")], [], ["upstream"],
      { upstream: "上游算力与基础设施" });
    expect(l.columns[0].label).toBe("上游算力与基础设施");
  });

  it("悬空边不画（防御：校验层已拒，渲染层不崩）", () => {
    const l = layoutChain([node("a", "upstream")], [edge("a", "ghost")], ["upstream"]);
    expect(l.edges).toHaveLength(0);
  });

  it("邻接高亮谓词：无选中全亮，选中只亮邻接边", () => {
    const l = layoutChain(
      [node("a", "upstream"), node("b", "midstream"), node("c", "downstream")],
      [edge("a", "b"), edge("b", "c")],
      ["upstream", "midstream", "downstream"],
    );
    expect(l.edges.every((e) => e.lit(null))).toBe(true);
    const lit = l.edges.filter((e) => e.lit("b"));
    expect(lit).toHaveLength(2);
    const litA = l.edges.filter((e) => e.lit("a"));
    expect(litA).toHaveLength(1);
  });
});

describe("parseWindowDate（部分日期确定性解析）", () => {
  it("YYYY / YYYY-MM / YYYY-MM-DD 都可解析（end 取月末）", () => {
    expect(parseWindowDate("2026")).toBe(Date.UTC(2026, 0, 1));
    expect(parseWindowDate("2026-03")).toBe(Date.UTC(2026, 2, 1));
    expect(parseWindowDate("2026-03", true)).toBe(Date.UTC(2026, 2, 31));
    expect(parseWindowDate("2026-02", true)).toBe(Date.UTC(2026, 1, 28));
    expect(parseWindowDate("2026-12-31")).toBe(Date.UTC(2026, 11, 31));
  });
  it("不可解析形态 → null（不猜）", () => {
    expect(parseWindowDate("2026年")).toBeNull();
    expect(parseWindowDate("Q3 2026")).toBeNull();
    expect(parseWindowDate("")).toBeNull();
    expect(parseWindowDate(null)).toBeNull();
    expect(parseWindowDate("2026-13")).toBeNull();
  });
});

describe("timelineScale（时间线布局）", () => {
  it("可解析日期的事件上轴，其余进未排期区（不编造位置）", () => {
    const items = [
      vitem("A", { window_start: "2026-01-01", window_end: "2026-12-31", status: "expected" }),
      vitem("B", { window_start: "2029-01", window_end: "2029-12", status: "expected" }),
      vitem("C", { status: "unknown" }),
    ];
    const s = timelineScale(items, 860, Date.UTC(2026, 8, 1));
    expect(s.dated).toHaveLength(2);
    expect(s.undated.map((i) => i.event)).toEqual(["C"]);
    expect(s.dated[0].x1).toBeLessThan(s.dated[1].x1);   // 时间序 → x 序
    expect(s.todayX).not.toBeNull();                      // 今天在域内 → 标记线
    expect(s.ticks.map((t) => t.label)).toContain("2027");
  });
  it("全部无日期 → 空轴（组件不渲染，回退列表）", () => {
    const s = timelineScale([vitem("X")], 860);
    expect(s.dated).toHaveLength(0);
    expect(s.undated).toHaveLength(1);
  });
  it("单日事件 x1==x2（渲染为菱形点）", () => {
    const s = timelineScale(
      [vitem("D", { window_start: "2026-05-01" }), vitem("E", { window_start: "2027-01-01" })],
      860,
    );
    const d = s.dated.find((r) => r.item.event === "D")!;
    expect(d.x1).toBe(d.x2);
  });
});

describe("tierCounts / stageGroups（离散分类，不造坐标）", () => {
  it("tier 计数按规范序，未知 tier 排后", () => {
    const counts = tierCounts([
      cand("a", { tier: "included" }), cand("b", { tier: "watchlist" }),
      cand("c", { tier: "included" }), cand("d", { tier: "custom_tier" }),
    ]);
    expect(counts[0]).toEqual({ tier: "included", count: 2 });
    expect(counts[1]).toEqual({ tier: "watchlist", count: 1 });
    expect(counts[counts.length - 1]).toEqual({ tier: "custom_tier", count: 1 });
  });
  it("阶段列按规范序（early→…→mature），未知阶段按出现序排后，空阶段不入列", () => {
    const groups = stageGroups([
      cand("a", { technology_stage: "clinical" }),
      cand("b", { technology_stage: "early" }),
      cand("c", { technology_stage: "pivotal" }),   // 未知阶段
      cand("d", { technology_stage: "" }),           // 无阶段 → 不入列
      cand("e", { technology_stage: "clinical" }),
    ]);
    expect(groups.map((g) => g.stage)).toEqual(["early", "clinical", "pivotal"]);
    expect(groups[1].items.map((c) => c.entity_id)).toEqual(["a", "e"]);
    expect(CANONICAL_STAGE_ORDER).toContain("preclinical");
  });
});

describe("rankedBarColumns（诚实护栏）", () => {
  const cols = [
    { id: "rev", label: "收入", unit: "USD" },
    { id: "yoy", label: "增速", unit: "ratio" },
  ];
  const rows = [{ label: "A" }, { label: "B" }, { label: "C" }];
  const num = (v: string, unit: string, cur: string | null) =>
    ({ value: v, unit, currency: cur, observation_id: "obs-1" });

  it("同单位同币种 ≥2 值 → 出图并按值降序", () => {
    const out = rankedBarColumns(cols, rows, [
      { label: "A", cells: { rev: num("100", "USD", "USD") } },
      { label: "B", cells: { rev: num("300", "USD", "USD") } },
      { label: "C", cells: { rev: num("200", "USD", "USD") } },
    ], true);
    expect(out).toHaveLength(1);
    expect(out[0].items.map((i) => i.value)).toEqual([300, 200, 100]);
  });

  it("混币种不出图（真实数据：USD千 vs CNY千 同列）", () => {
    const out = rankedBarColumns(cols, rows, [
      { label: "A", cells: { rev: num("100", "USD", "USD") } },
      { label: "B", cells: { rev: num("300", "CNY", "CNY") } },
    ], true);
    expect(out).toHaveLength(0);
  });

  it("列声明单位与观测单位不一致 → 不出图（yoy 列引用了基期收入观测的事故形态）", () => {
    const out = rankedBarColumns(cols, rows, [
      { label: "A", cells: { yoy: num("27456", "USD", "USD") } },
      { label: "B", cells: { yoy: num("81864", "USD", "USD") } },
    ], true);
    expect(out).toHaveLength(0);
  });

  it("chartable=false / 不可比行 / 展示字符串 → 不出图", () => {
    const numerics = [
      { label: "A", cells: { rev: num("100", "USD", "USD") } },
      { label: "B", cells: { rev: num("300", "USD", "USD") } },
    ];
    expect(rankedBarColumns(cols, rows, numerics, false)).toHaveLength(0);
    const rowsIncomparable = [{ label: "A" }, { label: "B", comparable: false }];
    expect(rankedBarColumns(cols, rowsIncomparable, numerics, true)).toHaveLength(0);
  });

  it("toBarNumber 与全站数值纪律一致（十进制字符串才转）", () => {
    expect(toBarNumber("106303")).toBe(106303);
    expect(toBarNumber("106,303")).toBeNull();   // 千分位展示串不猜
    expect(toBarNumber("+287.2%")).toBeNull();
    expect(toBarNumber(null)).toBeNull();
  });
});

// ---------------- 候选四象限（§20：离散序网格，不造连续坐标） ----------------

import { quadrantCells, shareToNumber } from "../viz";
import type { ProfitPool, QuadrantPayload } from "../types";

const qPoint = (id: string, x: number, y: number, tier = "included") => ({
  entity_id: id, name: id, tier, x, y, x_stage: "clinical", y_stage: "pilot",
  evidence_count: 1,
});

const quadrantPayload = (points: ReturnType<typeof qPoint>[]): QuadrantPayload => ({
  x_axis: { key: "technology_stage", title: "技术/护城河验证",
            order: ["early", "preclinical", "clinical", "commercial", "mature"],
            labels: { early: "早期", clinical: "临床" } },
  y_axis: { key: "commercial_stage", title: "商业验证",
            order: ["none", "pilot", "early_revenue", "scaling", "profitable"],
            labels: { none: "未商业化", pilot: "试点" } },
  points,
  unpositioned: [],
});

describe("quadrantCells", () => {
  it("把点放进正确的网格单元格，y 降序（商业验证高者在上）", () => {
    const rows = quadrantCells(quadrantPayload([
      qPoint("a", 2, 3), qPoint("b", 2, 3), qPoint("c", 0, 0),
    ]));
    expect(rows.length).toBe(5);            // 5 个 y 层
    expect(rows[0][0].y).toBe(4);           // 顶行 = y=4
    const cell23 = rows.flat().find((c) => c.x === 2 && c.y === 3);
    expect(cell23?.points.map((p) => p.entity_id)).toEqual(["a", "b"]); // 同格共存
    const cell00 = rows.flat().find((c) => c.x === 0 && c.y === 0);
    expect(cell00?.points.map((p) => p.entity_id)).toEqual(["c"]);
    expect(rows.flat().filter((c) => c.points.length === 0).length).toBe(25 - 2);
  });

  it("空点集 → 全空格", () => {
    const rows = quadrantCells(quadrantPayload([]));
    expect(rows.flat().every((c) => c.points.length === 0)).toBe(true);
  });
});

// ---------------- Profit Pool（§22/§48.4） ----------------

describe("shareToNumber", () => {
  it("只接受十进制百分数字符串", () => {
    expect(shareToNumber("45")).toBe(45);
    expect(shareToNumber("45.5")).toBe(45.5);
    expect(shareToNumber("0")).toBe(0);
    expect(shareToNumber("45%")).toBeNull();
    expect(shareToNumber("")).toBeNull();
    expect(shareToNumber("abc")).toBeNull();
    expect(shareToNumber("-3")).toBeNull(); // 份额为负不是合法输入
  });
});

const poolEntry = (node: string, share: string, estimated = false) => ({
  node, label: node, layer: "midstream", share, estimated, evidence_refs: [], note: "",
});

describe("ProfitPoolBar 数据契约", () => {
  it("份额为十进制字符串；estimated 标记透传（单源 §48.4）", () => {
    const pool: ProfitPool = {
      kind: "stacked_bar", unit: "percent",
      entries: [poolEntry("a", "55"), poolEntry("b", "45", true)],
      total_share: "100", notes: [],
    };
    expect(pool.entries[0].share).toBe("55");
    expect(pool.entries[1].estimated).toBe(true);
    expect(pool.entries.every((e) => shareToNumber(e.share) !== null)).toBe(true);
  });
});

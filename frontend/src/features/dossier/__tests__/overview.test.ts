// Overview 撕页纯函数纪律（dossier 优化方案）：
// - 引用编号按首次出现顺序、跨列表稳定（§16）；
// - 质量圆点只映射服务端分层（sufficient/partial/blocked），不造分数（§18）；
// - Top 公司按 tier 序（核心→观察→待核实→淘汰），层内保持原序（§9/§19）；
// - What Changed 分类只从文本语义推导，不编造方向（§11）；
// - 文本内嵌 raw ID 只剥离展示、证据引用不丢失（§10/§48.3）；
// - 瓶颈漏斗只按 layers 组织；全标瓶颈时由调用方判定不可分辨（红色不失效为噪音）。
import { describe, expect, it } from "vitest";

import { buildCitationIndex } from "../citations";
import {
  classifyChange, funnelFromIndustryMap, metricDisplayLabel, nextCatalysts,
  qualityDots, splitTextRefs, topCompanies, validationText,
} from "../overview";
import type { BusinessGraph, CandidateItem, DossierSnapshot, ValidationItem } from "../types";

const cand = (id: string, tier: string, over: Partial<CandidateItem> = {}): CandidateItem => ({
  entity_id: id, name: id, listing_status: "unknown", market: "", security_relation: "",
  tier, technology_stage: "", commercial_stage: "",
  moat_evidence: [], commercial_evidence: [], sustainability_evidence: [],
  counter_evidence: [], reason: "", next_validation: "", evidence_refs: [],
  investable: null, ...over,
});

const snapWith = (credibility: Record<string, string>, verdict: string | null = null) =>
  ({
    summary: { credibility, key_metrics: [], key_changes: [], drivers: [] },
    research: { answered: 0, required: 0, verdict },
  }) as unknown as DossierSnapshot;

describe("buildCitationIndex（引用编号）", () => {
  it("按首次出现顺序编号，跨列表共享同一编号", () => {
    const idx = buildCitationIndex([
      ["ev-a", "ev-b"],
      ["ev-b", "ev-c"],
      ["ev-a"],
    ]);
    expect(idx.get("ev-a")).toBe(1);
    expect(idx.get("ev-b")).toBe(2);
    expect(idx.get("ev-c")).toBe(3);
  });

  it("非证据引用（claim-/obs-）不进编号", () => {
    const idx = buildCitationIndex([["claim-x", "ev-a", "obs-y"]]);
    expect([...idx.keys()]).toEqual(["ev-a"]);
  });

  it("空输入 → 空表", () => {
    expect(buildCitationIndex([undefined, []]).size).toBe(0);
  });
});

describe("metricDisplayLabel（指标展示名）", () => {
  it("已知键映射中文", () => {
    expect(metricDisplayLabel("market_size", "市场规模")).toBe("市场规模");
    expect(metricDisplayLabel("discovery_preclinical_cost", "discovery preclinical cost")).toBe("发现+临床前成本");
  });
  it("中文 label 原样保留", () => {
    expect(metricDisplayLabel("unknown_key", "渗透率")).toBe("渗透率");
  });
  it("未知 snake_case 人性化（不改数据，仅表现层）", () => {
    expect(metricDisplayLabel("foo_bar", "foo bar")).toBe("Foo Bar");
  });
});

describe("qualityDots（研究质量圆点）", () => {
  it("sufficient=4 / partial=2 / blocked=1 / 无=0", () => {
    expect(qualityDots(snapWith({ level: "sufficient" })).filled).toBe(4);
    expect(qualityDots(snapWith({ level: "partial" })).filled).toBe(2);
    expect(qualityDots(snapWith({ level: "blocked" })).filled).toBe(1);
    expect(qualityDots(snapWith({})).filled).toBe(0);
  });
  it("credibility.level 缺失时回退 research.verdict", () => {
    expect(qualityDots(snapWith({}, "partial")).filled).toBe(2);
  });
  it("明细包含问题覆盖，不编造", () => {
    const q = qualityDots(snapWith({ level: "partial", facts_checked: "27 项" }));
    expect(q.detail.join(" ")).toContain("27 项");
  });
});

describe("topCompanies（Top 公司选择）", () => {
  it("tier 序优先，层内保持原序，截断到 limit", () => {
    const list = [
      cand("e1", "excluded"), cand("w1", "watchlist"), cand("i1", "included"),
      cand("w2", "watchlist"), cand("i2", "included"), cand("n1", "needs_review"),
    ];
    const top = topCompanies(list, 4);
    expect(top.map((c) => c.entity_id)).toEqual(["i1", "i2", "w1", "w2"]);
  });
  it("不足 limit 全返回", () => {
    expect(topCompanies([cand("a", "included")], 5)).toHaveLength(1);
  });
});

describe("classifyChange（What Changed 分类）", () => {
  it("[module] 前缀提炼为标签，反证/冲突 → risk", () => {
    const e = classifyChange("[counter-evidence] II 期成功率两源矛盾未裁决");
    expect(e.icon).toBe("risk");
    expect(e.tag).toBe("反证");
    expect(e.text).toContain("II 期成功率");
  });
  it("候选变动 → new", () => {
    expect(classifyChange("[candidate-pool] 某公司入选核心池").icon).toBe("new");
  });
  it("无前缀纯文本 → 无标签；按内容语义分类", () => {
    const up = classifyChange("证据支持成本下降，论断增强");
    expect(up.icon).toBe("up");
    expect(up.tag).toBeNull();
    // 「新增/入选」是候选变动语义 → new（内容驱动，不依赖前缀）
    const add = classifyChange("新增证据覆盖两家公司");
    expect(add.icon).toBe("new");
    expect(add.tag).toBeNull();
  });
});

describe("nextCatalysts（催化剂排序）", () => {
  const item = (event: string, status: string, ws = "", we = ""): ValidationItem => ({
    event, window_start: ws, window_end: we, status,
    trigger_condition: "", affected_judgment: "", company_refs: [], evidence_refs: [],
  });
  it("只要 expected 且有窗口，按起点升序，截断 limit", () => {
    const items = [
      item("b", "expected", "2027-01-01"),
      item("a", "expected", "2026-01-01"),
      item("done", "occurred", "2025-01-01"),
      item("unknown", "expected"),
    ];
    const next = nextCatalysts(items, 3);
    expect(next.map((i) => i.event)).toEqual(["a", "b"]);
  });
});

describe("splitTextRefs（文本内嵌 ID 剥离）", () => {
  it("剥离 ev-/claim- token，证据引用保留", () => {
    const { clean, refs } = splitTextRefs(
      "平台捕获 55-65%（Sustainability Atlas 转引 ev-985fbd0f5e41）；IIM 口径（claim-dda6af6adc12）",
    );
    expect(refs).toEqual(["ev-985fbd0f5e41", "claim-dda6af6adc12"]);
    expect(clean).not.toContain("ev-985fbd0f5e41");
    expect(clean).not.toContain("claim-dda6af6adc12");
    expect(clean).toContain("55-65%");
    expect(clean).not.toContain("（）");
  });
  it("无 ID 文本原样", () => {
    const { clean, refs } = splitTextRefs("临床验证缺口");
    expect(clean).toBe("临床验证缺口");
    expect(refs).toEqual([]);
  });
  it("数字与正常连字符不误伤（-$6M/18个月 保留）", () => {
    const { clean, refs } = splitTextRefs("压缩至 ~$6M/18个月，价差 100x");
    expect(refs).toEqual([]);
    expect(clean).toContain("$6M/18个月");
  });
});

describe("funnelFromIndustryMap（瓶颈漏斗）", () => {
  it("按 layers 组织，缺层跳过，节点名保留", () => {
    const graph: BusinessGraph = {
      nodes: [
        { node_id: "a", label: "算力", kind: "", note: "", layer: "upstream", bottleneck: false },
        { node_id: "b", label: "平台", kind: "", note: "", layer: "midstream", bottleneck: true },
      ],
      edges: [], narrative: "", narrative_refs: [],
      layers: ["upstream", "midstream", "downstream"],
    };
    const funnel = funnelFromIndustryMap(graph)!;
    expect(funnel.map((s) => s.key)).toEqual(["upstream", "midstream"]);
    expect(funnel[1].nodes[0].bottleneck).toBe(true);
  });
  it("无节点 → null（调用方回退文本列表）", () => {
    expect(funnelFromIndustryMap({ nodes: [], edges: [], narrative: "", narrative_refs: [] })).toBeNull();
    expect(funnelFromIndustryMap(null)).toBeNull();
  });
});

describe("validationText（验证阶段列）", () => {
  it("阶段 token 中文化，未知原文保留", () => {
    expect(validationText(cand("a", "included", { technology_stage: "clinical", commercial_stage: "commercial" })))
      .toBe("临床 · 商业化");
    expect(validationText(cand("b", "included", { technology_stage: "II期", commercial_stage: "" }))).toBe("II期");
    expect(validationText(cand("c", "included"))).toBe("");
  });
});

describe("splitTextRefs 括号残留清理", () => {
  it("剥离引用后空括号/分隔符括号/孤立闭括号全部清除", () => {
    expect(splitTextRefs("价值 600 万美元（obs-34d086583406、obs-fffe42334dca），对照 1-2 亿").clean)
      .toBe("价值 600 万美元，对照 1-2 亿");
    expect(splitTextRefs("获批数为零 [ev-57cea2e53eee]").clean).toBe("获批数为零");
    expect(splitTextRefs("覆盖 Top20 (claim-1ad65e10cfae)、获批为零").clean).toBe("覆盖 Top20、获批为零");
  });
});

describe("classifyChange targeted 标签", () => {
  it("targeted- 问题 id 不当标签（研究过程语言 §10）", () => {
    const e = classifyChange("[targeted-fcffe98a] 行业 tear-sheet 四要素已补齐");
    expect(e.tag).toBe("定向补研");
    expect(e.text).toBe("行业 tear-sheet 四要素已补齐");
  });
});

// 图表数值边界纪律（设计 §6.2/§13.1 前端组）：十进制字符串只在绘图边界转 number；
// 转换失败或超出安全范围 → null（调用方退回表格，不静默截断、不从文本猜数）。
import { describe, expect, it } from "vitest";

import { formatMetricValue, toChartNumber } from "../charts";

describe("toChartNumber", () => {
  it("十进制字符串 → number", () => {
    expect(toChartNumber("1500000000")).toBe(1500000000);
    expect(toChartNumber("-1234.56")).toBe(-1234.56);
    expect(toChartNumber("0.125")).toBe(0.125);
  });

  it("缺失值 → null（缺失不是 0）", () => {
    expect(toChartNumber(null)).toBeNull();
    expect(toChartNumber(undefined)).toBeNull();
    expect(toChartNumber("")).toBeNull();
  });

  it("非十进制字符串不猜数（旧 toNumber 取首个数字的行为被废除）", () => {
    expect(toChartNumber("1.2 billion")).toBeNull(); // 规模词必须走服务端换算链
    expect(toChartNumber("FY2025")).toBeNull(); // 年份不是指标值
    expect(toChartNumber("$1,200")).toBeNull(); // 千分位/货币符应在服务端标准化
    expect(toChartNumber("约 300")).toBeNull();
    expect(toChartNumber("1e5")).toBeNull(); // 科学计数法不是登记的十进制形态
  });

  it("超出安全范围 → null（不静默截断精度）", () => {
    expect(toChartNumber(String(Number.MAX_SAFE_INTEGER + 2))).toBeNull();
    expect(toChartNumber("99999999999999999999999999")).toBeNull();
  });
});

describe("formatMetricValue", () => {
  it("金额缩写 + 币种", () => {
    expect(formatMetricValue("1500000000", "USD", "USD")).toBe("1.50B USD");
    expect(formatMetricValue("300000000", "USD", "USD")).toBe("300.0M USD");
    expect(formatMetricValue("-1234", "USD", "USD")).toBe("-1.2K USD");
  });

  it("ratio 显示百分比", () => {
    expect(formatMetricValue("0.125", "ratio")).toBe("12.5%");
  });

  it("不可绘图值显示原值（不伪装成数字）", () => {
    expect(formatMetricValue("N/M", "USD")).toBe("N/M（不可绘图）");
    expect(formatMetricValue(null, "USD")).toBe("—");
  });
});

// review #17：先固定完整时间轴再排列各序列——不同期间集不得错位
import { buildOption } from "../charts";
import type { MetricPoint, MetricSeries } from "../types";

function pt(label: string, end: string, value: string | null, nature = "reported"): MetricPoint {
  return {
    period_label: label, period_end: end, period_start: null, value,
    nature, basis: "GAAP", unit: "USD", currency: "USD",
    observation_id: `obs-${label}`, status: "ok",
    knowledge_time: "2025-01-01T00:00:00+00:00", conflict: false,
  };
}

function ser(key: string, points: MetricPoint[], frequency = "FY"): MetricSeries {
  return {
    metric_key: key, label: key, unit: "USD", currency: "USD", frequency,
    dimensions: {}, basis: "GAAP", nature: "reported",
    points, status: "ready", issues: [],
  };
}

describe("buildOption 时间轴对齐", () => {
  it("后加入序列的期间不会错位先前序列（review #17 复现）", () => {
    // 收入仅 FY2024；现金流有 FY2023+FY2024 —— 旧实现会把收入画到 FY2023
    const { option, plottable } = buildOption([
      ser("revenue", [pt("FY2024", "2024-12-31", "100")]),
      ser("cfo", [pt("FY2023", "2023-12-31", "10"), pt("FY2024", "2024-12-31", "20")]),
    ]);
    const xAxis = (option as any).xAxis.data as string[];
    expect(xAxis).toEqual(["FY2023", "FY2024"]); // 按 period_end 排序
    const series = (option as any).series as { name: string; data: (number | null)[] }[];
    const revenue = series.find((s) => s.name === "revenue")!;
    const cfo = series.find((s) => s.name === "cfo")!;
    expect(revenue.data).toEqual([null, 100]); // FY2023 断点，不位移
    expect(cfo.data).toEqual([10, 20]);
    expect(plottable.map((s) => s.metric_key)).toEqual(["revenue", "cfo"]);
  });

  it("期间标签按 period_end 排序而非字典序", () => {
    const { option } = buildOption([
      ser("revenue", [
        pt("2024Q4", "2024-12-31", "4"),
        pt("2023Q4", "2023-12-31", "3"),
        pt("2025Q1", "2025-03-31", "5"),
      ], "Q"),
    ]);
    expect((option as any).xAxis.data).toEqual(["2023Q4", "2024Q4", "2025Q1"]);
  });

  it("全部值不可绘图 → plottable 为空（调用方退回表格）", () => {
    const { plottable, dropped } = buildOption([
      ser("revenue", [pt("FY2024", "2024-12-31", "1.2 billion")]),
    ]);
    expect(plottable).toEqual([]);
    expect(dropped.length).toBeGreaterThan(0);
  });
});

// ---------------- ratio 口径修复（profile 内容质量升级 §3） ----------------
// 事故：unit=ratio 的存量值是百分点（"85%"→85、"136.4%"→136.4），旧逻辑一律
// ×100 → 首屏显示 8500.0%/13640.0%。新纪律：披露原文锚点逐字优先；无锚点时
// |v|≤1 按分数、>1 按百分点——不从十进制反猜口径。
import { formatMetricValue as fmt, formatRatioDisplay, groupByUnit } from "../charts";
// MetricSeries 已在上方导入（同文件复用，不重复声明）

describe("ratio 显示口径", () => {
  it("披露原文锚点逐字优先（含区间与倍数）", () => {
    expect(fmt("80", "ratio", null, "80-90%")).toBe("80-90%");
    expect(fmt("100", "ratio", null, "100x")).toBe("100x");
    expect(fmt("85", "ratio", null, "85%")).toBe("85%");
  });

  it("无锚点：|v|≤1 按分数 ×100，>1 按百分点原样（不再 8500%）", () => {
    expect(fmt("0.125", "ratio")).toBe("12.5%");
    expect(fmt("85", "ratio")).toBe("85%");
    expect(fmt("136.4", "ratio")).toBe("136.4%");
    expect(fmt("-23.9", "ratio")).toBe("-23.9%");
    expect(fmt("1", "ratio")).toBe("100.0%");
  });

  it("formatRatioDisplay 与 formatMetricValue 同一口径（KPI 卡与 tooltip 不分叉）", () => {
    expect(formatRatioDisplay(85)).toBe("85%");
    expect(formatRatioDisplay(0.85)).toBe("85.0%");
    expect(formatRatioDisplay(85, "85%")).toBe("85%");
  });

  it("金额格式不受影响", () => {
    expect(fmt("159100000000", "USD", "USD")).toBe("159.10B USD");
    expect(fmt("6000000", "USD", "USD")).toBe("6.0M USD");
  });
});

describe("groupByUnit（KPI small multiples，方案 §9）", () => {
  const series = (key: string, unit: string, currency: string | null): MetricSeries => ({
    metric_key: key, label: key, unit, currency, frequency: "FY", dimensions: {},
    basis: "GAAP", nature: "reported", points: [], status: "ready", issues: [],
  });

  it("按 unit+currency 分组；真实数据形态（金额/比率/月数）不再同轴", () => {
    const groups = groupByUnit([
      series("rd_spend", "USD", "USD"),
      series("revenue", "CNY", "CNY"),
      series("phase1_rate", "ratio", null),
      series("milestone", "USD", "USD"),
    ]);
    expect(groups.map((g) => g.key)).toEqual(["USD|USD", "CNY|CNY", "ratio|"]);
    expect(groups[0].series.map((s) => s.metric_key)).toEqual(["rd_spend", "milestone"]);
    expect(groups[2].label).toBe("比例/倍数");
  });

  it("单一分组保持一组（调用方退回单图）", () => {
    expect(groupByUnit([series("a", "USD", "USD"), series("b", "USD", "USD")])).toHaveLength(1);
  });
});

// ---------------- Price vs EPS Revision（§26） ----------------

import { buildPriceRevisionOption, revisionDateKey } from "../charts";
import type { PricePoint, RevisionSeries } from "../types";

const revPoint = (at: string, value: string) => ({
  at, value, observation_id: `obs-${at}-${value}`, evidence_refs: [],
});
const pricePoint = (at: string, value: string): PricePoint => ({
  at, value, currency: "USD", observation_id: `obs-px-${at}`, evidence_refs: [],
});

describe("revisionDateKey", () => {
  it("ISO 时间戳 → yyyy-mm-dd；不可解析 → null", () => {
    expect(revisionDateKey("2025-03-01T10:00:00+00:00")).toBe("2025-03-01");
    expect(revisionDateKey("2025-03-01")).toBe("2025-03-01");
    expect(revisionDateKey("")).toBeNull();
    expect(revisionDateKey("2025-03")).toBeNull();
  });
});

describe("buildPriceRevisionOption", () => {
  it("价格左腿 + 修订右腿，双轴；点按日期升序", () => {
    const revision: RevisionSeries[] = [{
      metric_key: "consensus_eps", period_label: "FY2026", unit: "USD", currency: "USD",
      points: [revPoint("2025-03-01T00:00:00Z", "8.4"), revPoint("2025-01-15T00:00:00Z", "8.1")],
    }];
    const price = [pricePoint("2025-03-01", "95.5"), pricePoint("2025-01-02", "88.2")];
    const { option, revisionKeys, dropped } = buildPriceRevisionOption(revision, price);
    expect(dropped).toEqual([]);
    expect(revisionKeys.length).toBe(1);
    const series = (option as any).series;
    expect(series.length).toBe(2);
    const priceLine = series.find((s: any) => s.name === "股价");
    expect(priceLine.yAxisIndex).toBe(0);
    expect(priceLine.data).toEqual([["2025-01-02", 88.2], ["2025-03-01", 95.5]]);
    const revLine = series.find((s: any) => s.name !== "股价");
    expect(revLine.yAxisIndex).toBe(1);
    expect(revLine.data).toEqual([["2025-01-15", 8.1], ["2025-03-01", 8.4]]);
    expect(((option as any).yAxis as any[]).length).toBe(2);
  });

  it("不可绘图值被记录且不影响其余点", () => {
    const revision: RevisionSeries[] = [{
      metric_key: "consensus_eps", period_label: "FY2026", unit: "", currency: null,
      points: [revPoint("2025-03-01", "8.4"), revPoint("bad-date", "8.1"),
               revPoint("2025-02-01", "not-a-number")],
    }];
    const { option, dropped } = buildPriceRevisionOption(revision, []);
    expect(dropped.length).toBe(2);
    const series = (option as any).series;
    expect(series.length).toBe(1); // 价格无数据 → 不出价格线
    expect(series[0].data).toEqual([["2025-03-01", 8.4]]);
  });

  it("全不可绘图的修订序列被剔除并记录", () => {
    const revision: RevisionSeries[] = [{
      metric_key: "consensus_eps", period_label: "FY2026", unit: "", currency: null,
      points: [revPoint("", "x")],
    }];
    const { revisionKeys, dropped } = buildPriceRevisionOption(revision, []);
    expect(revisionKeys).toEqual([]);
    expect(dropped.some((d) => d.includes("全部数值不可绘图"))).toBe(true);
  });
});

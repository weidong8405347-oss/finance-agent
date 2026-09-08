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

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

// 图表适配器（设计 §4.5/§9.3）：ECharts 按需导入，统一单位/图例/tooltip/数据表。
//
// 数值纪律：
// - 后端值是十进制字符串，只在绘图边界转 number；转换失败或超出安全范围 →
//   退回数据表（不静默截断、不从文本猜数）；
// - 缺期保留断点（connectNulls: false）；
// - nature 颜色语义：reported 中性实线 / guidance 紫 / consensus 蓝 / model 虚线；
// - 每个图都有单位、期间、类别、图例、来源入口和数据表（键盘可达）。

import { useEffect, useMemo, useRef, useState } from "react";
import * as echarts from "echarts/core";
import { BarChart, LineChart } from "echarts/charts";
import { GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

import type { MetricPoint, MetricSeries } from "./types";

// 按需注册（§9.3）：只导入使用的图表与组件，控制依赖体积
echarts.use([LineChart, BarChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer]);

/** 十进制字符串 → 绘图 number（边界转换；失败/超安全范围 → null，调用方退回表格）。 */
export function toChartNumber(value: string | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  if (!/^-?\d+(\.\d+)?$/.test(value.trim())) return null;
  const n = Number(value);
  if (!Number.isFinite(n)) return null;
  if (Math.abs(n) > Number.MAX_SAFE_INTEGER) return null;
  return n;
}

/** nature → 颜色/线型语义（§4.5 规则 9）。 */
export const NATURE_STYLE: Record<string, { color: string; dash?: boolean; label: string }> = {
  reported: { color: "#3f3f46", label: "披露值" },
  calculated: { color: "#0d9488", label: "计算值" },
  guidance: { color: "#7c3aed", label: "公司指引" },
  consensus: { color: "#2563eb", label: "一致预期" },
  model_estimate: { color: "#9ca3af", dash: true, label: "模型估计" },
};

/** 展示格式化：大数缩写 + 单位/币种（全站一致的金额/比例格式，§13.3）。 */
export function formatMetricValue(
  value: string | null, unit: string, currency?: string | null,
): string {
  const n = toChartNumber(value);
  if (n === null) return value === null ? "—" : `${value}（不可绘图）`;
  const abs = Math.abs(n);
  let text: string;
  if (unit === "ratio") text = `${(n * 100).toFixed(1)}%`;
  else if (abs >= 1e12) text = `${(n / 1e12).toFixed(2)}T`;
  else if (abs >= 1e9) text = `${(n / 1e9).toFixed(2)}B`;
  else if (abs >= 1e6) text = `${(n / 1e6).toFixed(1)}M`;
  else if (abs >= 1e3) text = `${(n / 1e3).toFixed(1)}K`;
  else text = String(n);
  const cur = currency && unit !== "ratio" ? ` ${currency}` : "";
  return `${text}${cur}`;
}

export interface PointClickInfo {
  series: MetricSeries;
  point: MetricPoint;
}

interface ChartProps {
  series: MetricSeries[];
  height?: number;
  onPointClick?: (info: PointClickInfo) => void;
  title?: string;
}

/** 折线/柱状（按 frequency）：一个 MetricSeries 一条线；tooltip 带口径与来源。 */
export function MetricChart({ series, height = 260, onPointClick, title }: ChartProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);
  const [showTable, setShowTable] = useState(false);
  const [unplottable, setUnplottable] = useState<string[]>([]);

  // 依赖完整序列化（review #18）：值变化（点数不变）/切换历史快照都必须重建配置
  const seriesFingerprint = JSON.stringify(
    series.map((s) => [s.metric_key, s.unit, s.frequency,
      s.points.map((p) => [p.period_label, p.value, p.nature, p.conflict])]),
  );
  const { option, plottable, dropped, clickHandler } = useMemo(
    () => buildOption(series, onPointClick),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [seriesFingerprint, onPointClick],
  );

  useEffect(() => {
    setUnplottable(dropped);
  }, [dropped]);

  useEffect(() => {
    if (showTable || ref.current === null) return;
    const chart = echarts.init(ref.current);
    chartRef.current = chart;
    chart.setOption(option);
    if (clickHandler) chart.on("click", clickHandler);  // 点数据点 → 来源抽屉
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(ref.current);
    return () => { ro.disconnect(); chart.dispose(); chartRef.current = null; };
  }, [option, showTable, clickHandler]);

  if (plottable.length === 0) {
    return (
      <div className="rounded border border-dashed border-neutral-300 bg-neutral-50 p-3 text-xs text-neutral-500">
        {title && <div className="mb-1 font-semibold text-neutral-600">{title}</div>}
        无可绘图数值（缺失保留断点，不从文本猜数）。
        {series.length > 0 && <SeriesTable series={series} />}
      </div>
    );
  }

  return (
    <div className="rounded border border-neutral-200 bg-white">
      <div className="flex items-center justify-between border-b border-neutral-100 px-3 py-1.5">
        <div className="text-xs font-semibold text-neutral-700">
          {title}
          <span className="ml-2 font-normal text-neutral-400">
            {plottable.map((s) => `${s.label}${s.unit ? `（${s.unit}${s.currency ? `·${s.currency}` : ""}）` : ""}`).join("、")}
          </span>
        </div>
        <button
          onClick={() => setShowTable(!showTable)}
          className="rounded border border-neutral-200 px-2 py-0.5 text-[11px] text-neutral-600 hover:border-neutral-400"
          aria-pressed={showTable}
        >
          {showTable ? "看图表" : "数据表"}
        </button>
      </div>
      {showTable ? (
        <SeriesTable series={plottable} />
      ) : (
        <div ref={ref} style={{ height }} role="img"
             aria-label={title ?? "指标时序图（可用数据表查看同等信息）"} />
      )}
      {unplottable.length > 0 && (
        <div className="border-t border-amber-100 bg-amber-50 px-3 py-1 text-[11px] text-amber-800">
          {unplottable.length} 个数值不可安全绘图（超出范围/非十进制），已在数据表中列出原值
        </div>
      )}
    </div>
  );
}

export function buildOption(
  series: MetricSeries[],
  onPointClick?: (info: PointClickInfo) => void,
): {
  option: echarts.EChartsCoreOption;
  plottable: MetricSeries[];
  dropped: string[];
  clickHandler: ((params: any) => void) | null;
} {
  const dropped: string[] = [];
  const plottable: MetricSeries[] = [];
  const legends: string[] = [];
  const echartsSeries: any[] = [];

  // 先固定完整时间轴再排列各序列（review #17）：否则各序列按当时已知的
  // 类别子集对齐，后加入的期间会使先构建的序列错位
  const labelEnd = new Map<string, string>();
  for (const s of series) {
    for (const p of s.points) {
      const prev = labelEnd.get(p.period_label);
      if (!prev || p.period_end > prev) labelEnd.set(p.period_label, p.period_end);
    }
  }
  const categories = [...labelEnd.keys()].sort(
    (a, b) => (labelEnd.get(a)! === labelEnd.get(b)! ? a.localeCompare(b) : labelEnd.get(a)! < labelEnd.get(b)! ? -1 : 1),
  );

  for (const s of series) {
    const points: (number | null)[] = [];
    let any = false;
    for (const label of categories) {
      const p = s.points.find((x) => x.period_label === label);
      if (!p) { points.push(null); continue; }
      const n = toChartNumber(p.value);
      if (n === null) {
        if (p.value !== null) dropped.push(`${s.label} ${label}: ${p.value}`);
        points.push(null);
      } else {
        points.push(n);
        any = true;
      }
    }
    if (!any) {
      if (s.points.length) dropped.push(`${s.label}: 全部数值不可绘图`);
      continue;
    }
    plottable.push(s);
    legends.push(s.label);
    const style = NATURE_STYLE[s.points.find((p) => p.value !== null)?.nature ?? "reported"]
      ?? NATURE_STYLE.reported;
    echartsSeries.push({
      name: s.label,
      type: s.frequency === "Q" || s.frequency === "FY" ? "line" : "bar",
      data: points,
      connectNulls: false, // 缺期保留断点（不补零、不插值）
      symbolSize: 7,
      lineStyle: { color: style.color, type: style.dash ? "dashed" : "solid", width: 2 },
      itemStyle: { color: style.color },
    });
  }

  const option: echarts.EChartsCoreOption = {
    animation: false, // 默认不自动播放动画（§4.5：尊重 prefers-reduced-motion 的保守默认）
    grid: { left: 56, right: 16, top: 28, bottom: 28 },
    legend: {
      data: legends, top: 0, textStyle: { fontSize: 11, color: "#52525b" },
      itemWidth: 14, itemHeight: 8,
    },
    tooltip: {
      trigger: "axis",
      textStyle: { fontSize: 11 },
      formatter: (params: any) => {
        const list = Array.isArray(params) ? params : [params];
        const lines = [`<b>${list[0]?.axisValue ?? ""}</b>`];
        for (const item of list) {
          const s = plottable.find((x) => x.label === item.seriesName);
          const p = s?.points.find((x) => x.period_label === item.axisValue);
          if (!s || !p) continue;
          const style = NATURE_STYLE[p.nature] ?? NATURE_STYLE.reported;
          lines.push(
            `${item.marker} ${s.label}: <b>${formatMetricValue(p.value, p.unit ?? s.unit, p.currency ?? s.currency)}</b>`
            + `<br/><span style="color:#a1a1aa;font-size:10px">`
            + `${style.label} · ${p.basis}${p.conflict ? " · ⚠冲突" : ""} · 可知 ${p.knowledge_time.slice(0, 10)}`
            + `</span>`,
          );
        }
        return lines.join("<br/>");
      },
    },
    xAxis: {
      type: "category", data: categories,
      axisLabel: { fontSize: 10, color: "#71717a" },
      axisLine: { lineStyle: { color: "#e4e4e7" } },
    },
    yAxis: {
      type: "value",
      axisLabel: {
        fontSize: 10, color: "#71717a",
        formatter: (v: number) => {
          const abs = Math.abs(v);
          if (abs >= 1e9) return `${(v / 1e9).toFixed(1)}B`;
          if (abs >= 1e6) return `${(v / 1e6).toFixed(0)}M`;
          if (abs >= 1e3) return `${(v / 1e3).toFixed(0)}K`;
          return String(v);
        },
      },
      splitLine: { lineStyle: { color: "#f4f4f5" } },
    },
    series: echartsSeries,
  };
  // 点击数据点 → 打开来源抽屉（证据一次点击，§4.5 规则 4）
  const clickHandler = onPointClick
    ? (params: any) => {
        const s = plottable.find((x) => x.label === params.seriesName);
        const p = s?.points.find((x) => x.period_label === params.name);
        if (s && p) onPointClick({ series: s, point: p });
      }
    : null;
  return { option, plottable, dropped, clickHandler };
}

/** 键盘可达的同等信息数据表（图表的可访问替代，§4.5 规则 10）。 */
export function SeriesTable({ series }: { series: MetricSeries[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-xs">
        <thead>
          <tr className="border-b border-neutral-200 bg-neutral-50 text-left text-[10px] uppercase text-neutral-500">
            <th className="px-2 py-1">期间</th>
            {series.map((s) => (
              <th key={s.metric_key} className="px-2 py-1">
                {s.label}
                <span className="ml-1 normal-case text-neutral-400">
                  {s.unit}{s.currency ? `·${s.currency}` : ""}
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {allLabels(series).map((label) => (
            <tr key={label} className="border-b border-neutral-100">
              <td className="px-2 py-1 font-mono text-[11px] text-neutral-600">{label}</td>
              {series.map((s) => {
                const p = s.points.find((x) => x.period_label === label);
                return (
                  <td key={s.metric_key} className="px-2 py-1 font-mono tabular-nums">
                    {p ? (
                      <span title={`${NATURE_STYLE[p.nature]?.label ?? p.nature} · ${p.basis} · 可知 ${p.knowledge_time.slice(0, 10)} · ${p.observation_id}`}>
                        {p.value ?? "—"}
                        {p.conflict && <span className="ml-1 text-amber-600">⚠</span>}
                      </span>
                    ) : "—"}
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

function allLabels(series: MetricSeries[]): string[] {
  const set = new Set<string>();
  for (const s of series) for (const p of s.points) set.add(p.period_label);
  return [...set].sort();
}

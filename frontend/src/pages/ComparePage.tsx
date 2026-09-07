// 比较工作台（#/knowledge/compare?entities=stock:BE,stock:PLUG&metric=revenue，§4.1）。
// 同一截止时点、同口径才可比；不满足显示 exclusions 与原因（不硬比）。

import { useEffect, useState } from "react";

import { navigate, withParams, type Route } from "../app/route";
import { dossierApi } from "../features/dossier/api";
import { formatMetricValue } from "../features/dossier/charts";

interface CompareResult {
  metric: string;
  as_of: string;
  items: Record<string, any>[];
  exclusions: { entity: string; reason: string }[];
  notes: string[];
  comparable: boolean;
}

export default function ComparePage({ params }: { params: Record<string, string> }) {
  const [entities, setEntities] = useState(params.entities ?? "");
  const [metric, setMetric] = useState(params.metric ?? "revenue");
  const [asOf, setAsOf] = useState((params.as_of ?? "").slice(0, 10));
  const [result, setResult] = useState<CompareResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!entities || !metric) { setResult(null); return; }
    let cancelled = false;
    setError(null);
    dossierApi.compare({
      entities, metric,
      as_of: asOf ? `${asOf}T23:59:59+00:00` : undefined,
      namespace: params.namespace,
    })
      .then((r) => { if (!cancelled) setResult(r); })
      .catch((e) => {
        if (cancelled) return;
        setResult(null);
        setError(e instanceof Error ? e.message : String(e));
      });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick]);

  const routeOf = (): Route => ({ page: "compare", params });

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold text-neutral-500">同口径比较工作台</h2>
        <button onClick={() => navigate({ page: "knowledge", params: {} })}
                className="ml-auto rounded border border-neutral-200 px-2 py-0.5 text-xs hover:border-neutral-400">
          ← 档案库
        </button>
      </div>
      <div className="mb-4 flex flex-wrap items-end gap-3 rounded-lg border border-neutral-200 bg-white p-3 text-xs">
        <label className="block">
          <span className="mb-0.5 block text-neutral-500">实体（kind:id，逗号分隔）</span>
          <input value={entities} onChange={(e) => setEntities(e.target.value)}
                 placeholder="stock:BE,stock:PLUG"
                 className="w-72 rounded border border-neutral-200 px-2 py-1 font-mono" />
        </label>
        <label className="block">
          <span className="mb-0.5 block text-neutral-500">指标键（typed 观测）</span>
          <input value={metric} onChange={(e) => setMetric(e.target.value)}
                 className="w-40 rounded border border-neutral-200 px-2 py-1 font-mono" />
        </label>
        <label className="block">
          <span className="mb-0.5 block text-neutral-500">截止时点</span>
          <input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)}
                 className="rounded border border-neutral-200 px-2 py-1 font-mono" />
        </label>
        <button
          onClick={() => {
            navigate(withParams(routeOf(), {
              entities, metric, as_of: asOf ? `${asOf}T23:59:59+00:00` : null,
            }));
            setTick((t) => t + 1);
          }}
          className="rounded bg-neutral-900 px-3 py-1.5 font-semibold text-white hover:bg-neutral-700"
        >
          比较
        </button>
      </div>

      {error && (
        <div className="rounded border border-red-200 bg-red-50 p-3 text-xs text-red-700">
          比较失败：{error}
          <button onClick={() => setTick((t) => t + 1)} className="ml-2 underline">重试</button>
        </div>
      )}
      {result && (
        <div className="space-y-3">
          {!result.comparable && (
            <div className="rounded border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
              当前选择不可比：{result.notes.join("；") || "可比实体不足两个"}——下表仅供参考，不作为同业结论。
            </div>
          )}
          {result.items.length > 0 && (
            <div className="overflow-x-auto rounded-lg border border-neutral-200 bg-white">
              <table className="w-full border-collapse text-sm">
                <thead>
                  <tr className="border-b border-neutral-200 bg-neutral-50 text-left text-[11px] text-neutral-500">
                    <th className="px-3 py-2">实体</th><th className="px-3 py-2">{result.metric}</th>
                    <th className="px-3 py-2">期间</th><th className="px-3 py-2">口径</th>
                    <th className="px-3 py-2">性质</th><th className="px-3 py-2" />
                  </tr>
                </thead>
                <tbody>
                  {result.items.map((it) => (
                    <tr key={String(it.entity)} className="border-b border-neutral-100">
                      <td className="px-3 py-2 font-mono text-xs font-semibold">{String(it.entity)}</td>
                      <td className="px-3 py-2 font-mono tabular-nums">
                        {formatMetricValue(it.value as string | null, "", null)}
                        <span className="ml-1 text-[10px] text-neutral-400">{String(it.currency ?? "")}</span>
                      </td>
                      <td className="px-3 py-2 font-mono text-xs text-neutral-500">{String(it.period_label)}</td>
                      <td className="px-3 py-2 text-xs">{String(it.basis)}</td>
                      <td className="px-3 py-2 text-xs text-neutral-500">{String(it.nature)}</td>
                      <td className="px-3 py-2">
                        <button
                          onClick={() => {
                            const [kind, id] = String(it.entity).split(":");
                            navigate({ page: "knowledge", kind: kind as "stock" | "industry", id, params: {} });
                          }}
                          className="text-xs text-blue-700 hover:underline"
                        >
                          档案 →
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {result.exclusions.length > 0 && (
            <div className="rounded border border-neutral-200 bg-neutral-50 p-3 text-xs text-neutral-500">
              <div className="mb-1 font-semibold text-neutral-600">未纳入（原因保留，不与「研究否定」混淆）</div>
              {result.exclusions.map((x, i) => (
                <div key={i}>· {x.entity}：{x.reason}</div>
              ))}
            </div>
          )}
          <div className="font-mono text-[10px] text-neutral-400">同一截止时点 {result.as_of}</div>
        </div>
      )}
      {!result && !error && (
        <div className="rounded-lg border border-dashed border-neutral-300 py-10 text-center text-xs text-neutral-400">
          输入实体与指标开始比较（只消费 typed 观测——同截止时点、同口径才可比）
        </div>
      )}
    </div>
  );
}

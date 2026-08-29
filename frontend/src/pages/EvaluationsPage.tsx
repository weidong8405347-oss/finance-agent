import { useEffect, useState } from "react";
import { api, EvalSummary } from "../api";

export default function EvaluationsPage() {
  const [runs, setRuns] = useState<EvalSummary[]>([]);

  useEffect(() => {
    api.evaluations().then(setRuns).catch(() => setRuns([]));
  }, []);

  return (
    <div>
      <h2 className="mb-3 text-sm font-semibold text-neutral-500">评估运行</h2>
      <div className="overflow-hidden rounded border border-neutral-200 bg-white">
        <table className="w-full text-left text-xs">
          <thead className="bg-neutral-50 font-mono text-[11px] text-neutral-500">
            <tr>
              <th className="px-3 py-2">run</th>
              <th className="px-3 py-2">配置</th>
              <th className="px-3 py-2">判定</th>
              <th className="px-3 py-2">泄漏事件</th>
              <th className="px-3 py-2">平均净收益</th>
              <th className="px-3 py-2">KB 增量</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.eval_run_id} className="border-t border-neutral-100">
                <td className="px-3 py-2 font-mono">{r.eval_run_id}</td>
                <td className="px-3 py-2 font-mono">{r.config_name}</td>
                <td className="px-3 py-2">
                  <span
                    className={`rounded px-1.5 py-0.5 font-mono text-[11px] ${
                      r.verdict === "clean"
                        ? "bg-green-100 text-green-800"
                        : "bg-red-100 text-red-800"
                    }`}
                  >
                    {r.verdict}
                  </span>
                </td>
                <td className="px-3 py-2 font-mono">{r.leakage_events}</td>
                <td className="px-3 py-2 font-mono">
                  {(r.mean_net_return * 100).toFixed(2)}%
                </td>
                <td className="px-3 py-2 font-mono">{(r.kb_delta * 100).toFixed(2)}%</td>
              </tr>
            ))}
            {runs.length === 0 && (
              <tr>
                <td colSpan={6} className="px-3 py-6 text-center text-neutral-400">
                  暂无评估运行
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

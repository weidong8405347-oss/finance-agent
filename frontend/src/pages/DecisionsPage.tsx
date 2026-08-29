import { useEffect, useState } from "react";
import { api, DecisionCardJson } from "../api";

const ACTION_STYLE: Record<string, string> = {
  buy: "bg-green-100 text-green-800",
  sell: "bg-red-100 text-red-800",
  hold: "bg-blue-100 text-blue-800",
  avoid: "bg-neutral-200 text-neutral-700",
  watch: "bg-amber-100 text-amber-800",
};

export default function DecisionsPage() {
  const [cards, setCards] = useState<DecisionCardJson[]>([]);

  useEffect(() => {
    api.decisions().then(setCards).catch(() => setCards([]));
  }, []);

  return (
    <div>
      <h2 className="mb-3 text-sm font-semibold text-neutral-500">决策卡</h2>
      <div className="space-y-3">
        {cards.map((c) => (
          <div key={c.card_id} className="rounded border border-neutral-200 bg-white p-4">
            <div className="flex items-center gap-3">
              <span
                className={`rounded px-2 py-0.5 font-mono text-xs font-semibold ${ACTION_STYLE[c.action] ?? "bg-neutral-100"}`}
              >
                {c.action.toUpperCase()}
              </span>
              <span className="font-mono text-sm">{c.subject.id}</span>
              <span className="text-xs text-neutral-500">
                信心 {"●".repeat(c.conviction)}
                {"○".repeat(5 - c.conviction)} · {c.horizon}
              </span>
              <span className="ml-auto font-mono text-[11px] text-neutral-400">
                {c.created_at.slice(0, 10)}
              </span>
            </div>
            {c.position && (
              <div className="mt-2 text-xs text-neutral-600">
                仓位 {(c.position.sizing_pct * 100).toFixed(0)}% · 最大亏损预算{" "}
                {(c.position.max_loss_pct * 100).toFixed(0)}%
              </div>
            )}
            <div className="mt-2">
              <div className="text-xs font-medium text-neutral-500">失效条件</div>
              <ul className="mt-1 list-inside list-disc text-xs text-neutral-700">
                {c.invalidation.map((inv, i) => (
                  <li key={i}>{inv}</li>
                ))}
              </ul>
            </div>
            <div className="mt-2 flex flex-wrap gap-1">
              {c.rationale.map((eid) => (
                <span
                  key={eid}
                  className="rounded bg-neutral-100 px-1.5 py-0.5 font-mono text-[11px] text-neutral-600"
                  title="证据 id（钻取原文见 Knowledge 页）"
                >
                  {eid}
                </span>
              ))}
            </div>
            <div className="mt-2 font-mono text-[10px] text-neutral-400">
              kb:{c.kb_snapshot_id.slice(0, 19)}… · {c.card_id}
            </div>
          </div>
        ))}
        {cards.length === 0 && <p className="text-sm text-neutral-400">暂无决策卡</p>}
      </div>
    </div>
  );
}

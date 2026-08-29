import { useEffect, useState } from "react";
import { api, EntityProfile, EntityRow } from "../api";

// Knowledge：实体档案 + as_of 时光机（本项目差异化页面）
export default function KnowledgePage() {
  const [entities, setEntities] = useState<EntityRow[]>([]);
  const [selected, setSelected] = useState<EntityRow | null>(null);
  const [profile, setProfile] = useState<EntityProfile | null>(null);
  const [asOf, setAsOf] = useState<string>(""); // 空 = 最新

  useEffect(() => {
    api.entities().then(setEntities).catch(() => setEntities([]));
  }, []);

  useEffect(() => {
    if (selected) {
      api.entityProfile(selected.kind, selected.id, asOf || undefined).then(setProfile);
    }
  }, [selected, asOf]);

  return (
    <div className="grid grid-cols-3 gap-6">
      <section className="col-span-1">
        <h2 className="mb-3 text-sm font-semibold text-neutral-500">实体</h2>
        <ul className="space-y-1">
          {entities.map((e) => (
            <li key={`${e.kind}:${e.id}`}>
              <button
                onClick={() => setSelected(e)}
                className={`w-full rounded border px-3 py-2 text-left font-mono text-xs ${
                  selected?.id === e.id
                    ? "border-neutral-900 bg-neutral-900 text-white"
                    : "border-neutral-200 bg-white hover:border-neutral-400"
                }`}
              >
                {e.kind}:{e.id}
                <span className="ml-2 opacity-70">{e.field_count} fields</span>
              </button>
            </li>
          ))}
          {entities.length === 0 && <li className="text-sm text-neutral-400">暂无档案</li>}
        </ul>
      </section>
      <section className="col-span-2">
        {selected && (
          <>
            <div className="mb-4 flex items-center gap-3">
              <h2 className="text-sm font-semibold text-neutral-500">
                {selected.kind}:{selected.id}
              </h2>
              <label className="ml-auto flex items-center gap-2 text-xs text-neutral-500">
                as_of 时光机
                <input
                  type="datetime-local"
                  value={asOf}
                  onChange={(e) => setAsOf(e.target.value)}
                  className="rounded border border-neutral-300 px-2 py-1 font-mono text-xs"
                />
                {asOf && (
                  <button onClick={() => setAsOf("")} className="text-blue-600 underline">
                    回最新
                  </button>
                )}
              </label>
            </div>
            <div className="space-y-2">
              {profile &&
                Object.entries(profile.facts).map(([field, fact]) => (
                  <div
                    key={field}
                    className="rounded border border-neutral-200 bg-white px-4 py-3"
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-mono text-sm font-medium">{field}</span>
                      <span className="font-mono text-[11px] text-neutral-400">
                        v{fact.version} · 可知于 {fact.knowledge_time.slice(0, 10)}
                        {fact.conflict && (
                          <span className="ml-2 rounded bg-red-100 px-1.5 py-0.5 text-red-700">
                            冲突
                          </span>
                        )}
                      </span>
                    </div>
                    <div className="mt-1 text-sm">{JSON.stringify(fact.value)}</div>
                    <div className="mt-2 space-y-1">
                      {fact.evidence.map((ev) => (
                        <div
                          key={ev.evidence_id}
                          className="rounded bg-neutral-50 px-2 py-1 font-mono text-[11px] text-neutral-600"
                          title={ev.verbatim_quote}
                        >
                          [{ev.pit_grade ?? "?"}] {ev.source_id ?? ev.evidence_id} ·{" "}
                          {ev.available_at?.slice(0, 10) ?? "无PIT"}
                          {ev.url && (
                            <a
                              href={ev.url}
                              target="_blank"
                              rel="noreferrer"
                              className="ml-2 text-blue-600 underline"
                            >
                              原文
                            </a>
                          )}
                          {ev.verbatim_quote && (
                            <div className="mt-0.5 italic opacity-80">
                              “{ev.verbatim_quote}”
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              {profile && Object.keys(profile.facts).length === 0 && (
                <p className="text-sm text-neutral-400">
                  该时点无任何可知事实（as_of 过滤生效）
                </p>
              )}
            </div>
          </>
        )}
        {!selected && <p className="text-sm text-neutral-400">选择左侧实体查看档案</p>}
      </section>
    </div>
  );
}

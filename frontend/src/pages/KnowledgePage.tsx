// Knowledge 页（R3 重做）：全部档案列表（点击选中 → 摘要联动）+ 详情页
// （facts 证据锚点 / thesis / as_of 时光机 / HTML 存档版本查看器）。
import { useEffect, useState } from "react";
import { api, ArchiveRow, EntityProfile, EntityRow } from "../api";

function fmtDate(iso: string | null): string {
  return iso ? iso.slice(0, 10) : "—";
}

export default function KnowledgePage() {
  const [entities, setEntities] = useState<EntityRow[]>([]);
  const [selected, setSelected] = useState<EntityRow | null>(null);
  const [detail, setDetail] = useState<EntityRow | null>(null);  // 非空 = 详情视图

  useEffect(() => {
    api.entities().then(setEntities).catch(() => setEntities([]));
  }, []);

  if (detail) {
    return <EntityDetail kind={detail.kind} id={detail.id} onBack={() => setDetail(null)} />;
  }

  return (
    <div>
      <h2 className="mb-3 text-sm font-semibold text-neutral-500">全部档案</h2>
      <div className="overflow-hidden rounded-lg border border-neutral-200 bg-white">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-neutral-200 bg-neutral-50 text-left text-[11px] text-neutral-500">
              <th className="px-3 py-2">实体</th><th className="px-3 py-2">类型</th>
              <th className="px-3 py-2">完整度</th><th className="px-3 py-2">字段数</th>
              <th className="px-3 py-2">陈旧</th><th className="px-3 py-2">冲突</th>
              <th className="px-3 py-2">最近可知</th><th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody>
            {entities.map((e) => (
              <tr
                key={`${e.kind}:${e.id}`}
                onClick={() => setSelected(e)}
                className={`cursor-pointer border-b border-neutral-100 ${
                  selected?.id === e.id && selected?.kind === e.kind
                    ? "bg-neutral-100" : "hover:bg-neutral-50"
                }`}
              >
                <td className="px-3 py-2 font-mono text-xs font-semibold">{e.id}</td>
                <td className="px-3 py-2 text-xs text-neutral-500">{e.kind}</td>
                <td className="px-3 py-2">
                  <span className="mr-1 inline-block h-1.5 w-16 overflow-hidden rounded bg-neutral-100 align-middle">
                    <span className="block h-full bg-green-600" style={{ width: `${e.completeness * 100}%` }} />
                  </span>
                  <span className="text-xs">{(e.completeness * 100).toFixed(0)}%</span>
                </td>
                <td className="px-3 py-2 font-mono text-xs">{e.field_count}</td>
                <td className="px-3 py-2 text-xs">{e.stale_count > 0 ? `${e.stale_count} 项` : "—"}</td>
                <td className="px-3 py-2 text-xs">
                  {e.conflict_count > 0
                    ? <span className="rounded bg-amber-50 px-1.5 py-0.5 text-amber-700">{e.conflict_count}</span>
                    : "0"}
                </td>
                <td className="px-3 py-2 font-mono text-xs text-neutral-500">{fmtDate(e.last_knowledge_time)}</td>
                <td className="px-3 py-2">
                  <button
                    onClick={(ev) => { ev.stopPropagation(); setDetail(e); }}
                    className="rounded border border-neutral-200 px-2 py-0.5 text-xs hover:border-neutral-400"
                  >
                    查看详情 →
                  </button>
                </td>
              </tr>
            ))}
            {entities.length === 0 && (
              <tr><td colSpan={8} className="px-3 py-8 text-center text-neutral-400">
                暂无档案——在对话里说「研究一下 BE」或用 /research 开始
              </td></tr>
            )}
          </tbody>
        </table>
      </div>

      {selected && <EntitySummary kind={selected.kind} id={selected.id} onDetail={() => setDetail(selected)} />}
    </div>
  );
}

// ---------- 摘要面板（列表选中联动） ----------
function EntitySummary({ kind, id, onDetail }: { kind: string; id: string; onDetail: () => void }) {
  const [profile, setProfile] = useState<EntityProfile | null>(null);
  const [archives, setArchives] = useState<ArchiveRow[]>([]);

  useEffect(() => {
    setProfile(null);
    api.entityProfile(kind, id).then(setProfile).catch(() => setProfile(null));
    api.archives(kind, id).then(setArchives).catch(() => setArchives([]));
  }, [kind, id]);

  const facts = Object.entries(profile?.facts ?? {}).slice(0, 6);
  const latest = archives[0];
  return (
    <div className="mt-4 grid grid-cols-2 gap-4">
      <div className="rounded-lg border border-neutral-200 bg-white p-4">
        <div className="mb-2 flex items-center justify-between">
          <h3 className="font-mono text-sm font-semibold">{kind}:{id}</h3>
          <button onClick={onDetail} className="text-xs text-blue-700 hover:underline">查看详情 →</button>
        </div>
        {facts.map(([field, f]) => (
          <div key={field} className="flex justify-between border-b border-dashed border-neutral-100 py-1 text-xs">
            <span className="text-neutral-600">{field}</span>
            <span
              className="max-w-[60%] truncate text-right font-mono"
              title={f.evidence?.[0]
                ? `${f.evidence[0].evidence_id} ·「${f.evidence[0].verbatim_quote}」· available ${f.evidence[0].available_at} · PIT-${f.evidence[0].pit_grade}`
                : "无证据"}
            >
              {JSON.stringify(f.value)}
              {f.conflict && <span className="ml-1 text-amber-600">⚠</span>}
            </span>
          </div>
        ))}
        {facts.length === 0 && <div className="text-xs text-neutral-400">（无事实）</div>}
      </div>
      <div className="rounded-lg border border-neutral-200 bg-white p-4">
        <h3 className="mb-2 text-sm font-semibold">HTML 存档（{archives.length} 个版本）</h3>
        {archives.slice(0, 5).map((a) => (
          <div key={a.name} className="flex justify-between py-1 font-mono text-xs">
            <span>{a.name} {a.is_latest && <span className="text-green-700">· latest</span>}</span>
            <span className="text-neutral-400">{fmtDate(a.mtime)}</span>
          </div>
        ))}
        {latest && (
          <iframe
            title="存档预览"
            src={api.archiveUrl(kind, id, "latest.html")}
            className="mt-2 h-64 w-full rounded-md border border-neutral-200"
          />
        )}
        {archives.length === 0 && (
          <div className="text-xs text-neutral-400">（暂无存档——跑过 /profile 或 /decide 后生成）</div>
        )}
      </div>
    </div>
  );
}

// ---------- 详情页 ----------
function EntityDetail({ kind, id, onBack }: { kind: string; id: string; onBack: () => void }) {
  const [profile, setProfile] = useState<EntityProfile | null>(null);
  const [archives, setArchives] = useState<ArchiveRow[]>([]);
  const [asOf, setAsOf] = useState("");
  const [archiveView, setArchiveView] = useState("latest.html");

  const load = (as_of?: string) => {
    api.entityProfile(kind, id, as_of || undefined).then(setProfile).catch(() => setProfile(null));
  };
  useEffect(() => {
    load();
    api.archives(kind, id).then(setArchives).catch(() => setArchives([]));
  }, [kind, id]);

  const facts = Object.entries(profile?.facts ?? {});
  const thesis = profile?.facts?.thesis;
  return (
    <div>
      <button onClick={onBack} className="mb-3 rounded border border-neutral-200 px-2.5 py-1 text-xs hover:bg-neutral-50">
        ← 返回全部档案
      </button>
      <div className="mb-4 flex flex-wrap items-baseline gap-3">
        <h2 className="font-mono text-lg font-bold">{kind}:{id}</h2>
        <span className="text-xs text-neutral-500">
          as_of <input type="date" value={asOf} onChange={(e) => { setAsOf(e.target.value); load(e.target.value ? `${e.target.value}T23:59:59Z` : undefined); }}
            className="rounded border border-neutral-200 px-1.5 py-0.5 text-xs" /> 时光机
        </span>
        {profile && <span className="font-mono text-[11px] text-neutral-400">投影时刻 {profile.as_of.slice(0, 19)}Z</span>}
      </div>

      <div className="grid grid-cols-2 items-start gap-4">
        <div className="space-y-4">
          {thesis && (
            <div className="rounded-lg border border-neutral-200 bg-white p-4">
              <h3 className="mb-1 text-sm font-semibold">投资论点（thesis v{thesis.version}）</h3>
              <p className="text-sm leading-relaxed text-neutral-700">{String(thesis.value)}</p>
            </div>
          )}
          <div className="rounded-lg border border-neutral-200 bg-white p-4">
            <h3 className="mb-2 text-sm font-semibold">核心事实（hover 数字看证据原文）</h3>
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-neutral-200 text-left text-[10px] text-neutral-400">
                  <th className="py-1">字段</th><th>值</th><th>event_time</th><th>knowledge_time</th><th>v</th>
                </tr>
              </thead>
              <tbody>
                {facts.filter(([f]) => f !== "thesis").map(([field, f]) => (
                  <tr key={field} className="border-b border-neutral-100">
                    <td className="py-1.5 font-mono">{field}</td>
                    <td className="max-w-40 truncate py-1.5 font-mono"
                      title={f.evidence?.map((ev) =>
                        `${ev.evidence_id} · ${ev.source_id}\n「${ev.verbatim_quote}」\navailable ${ev.available_at} · PIT-${ev.pit_grade}`
                      ).join("\n\n") || "无证据"}
                    >
                      <span className="border-b border-dotted border-neutral-400">
                        {JSON.stringify(f.value)}
                      </span>
                      {f.conflict && <span className="ml-1 text-amber-600">⚠冲突</span>}
                    </td>
                    <td className="py-1.5 font-mono text-neutral-500">{f.event_time?.slice(0, 10) ?? "—"}</td>
                    <td className="py-1.5 font-mono text-neutral-500">{f.knowledge_time.slice(0, 10)}</td>
                    <td className="py-1.5 font-mono text-neutral-400">v{f.version}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="rounded-lg border border-neutral-200 bg-white p-4">
          <h3 className="mb-2 text-sm font-semibold">HTML 存档</h3>
          <div className="mb-2 flex flex-wrap gap-1">
            <button onClick={() => setArchiveView("latest.html")}
              className={`rounded border px-2 py-0.5 font-mono text-[11px] ${archiveView === "latest.html" ? "border-neutral-800 bg-neutral-900 text-white" : "border-neutral-200"}`}>
              latest
            </button>
            {archives.map((a) => (
              <button key={a.name} onClick={() => setArchiveView(a.name)}
                className={`rounded border px-2 py-0.5 font-mono text-[11px] ${archiveView === a.name ? "border-neutral-800 bg-neutral-900 text-white" : "border-neutral-200"}`}>
                {a.name.replace(".html", "")}
              </button>
            ))}
          </div>
          <iframe
            key={archiveView}
            title="档案存档"
            src={api.archiveUrl(kind, id, archiveView)}
            className="h-[480px] w-full rounded-md border border-neutral-200"
          />
          <a href={api.archiveUrl(kind, id, archiveView)} target="_blank" rel="noreferrer"
            className="mt-1 inline-block text-xs text-blue-700 hover:underline">
            新页打开 ↗
          </a>
        </div>
      </div>
    </div>
  );
}

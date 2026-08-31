// Knowledge 页（R3 重做）：全部档案列表（点击选中 → 摘要联动）+ 详情页
// （facts 证据锚点 / thesis / as_of 时光机 / HTML 存档版本查看器）。
import { Fragment, useEffect, useState } from "react";
import { api, ArchiveRow, CompareItem, EntityProfile, EntityRow, SeriesPoint } from "../api";
import { BarCompare, LineChart } from "../components/MiniChart";

// 值 → 数值（图表用）：取首个数字片段（去逗号）；非数值字段返回 null
function toNumber(v: unknown): number | null {
  if (typeof v === "number") return v;
  const m = String(v).replace(/,/g, "").match(/-?\d+(\.\d+)?/);
  return m ? parseFloat(m[0]) : null;
}

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

// ---------- 图表区（详情页）：字段时序走势 + 跨实体对比 ----------
function FactCharts({ kind, id, facts }: { kind: string; id: string; facts: string[] }) {
  const numericFields = facts.filter((f) => /revenue|income|margin|growth|size|rate|backlog|capacity/i.test(f));
  const [field, setField] = useState<string>("");
  const [series, setSeries] = useState<SeriesPoint[]>([]);
  const [peers, setPeers] = useState<CompareItem[]>([]);

  useEffect(() => {
    setField(numericFields[0] ?? "");
  }, [id, facts.length]);

  useEffect(() => {
    if (!field) return;
    api.series(kind, id, [field]).then((r) => setSeries(r.fields[field] ?? [])).catch(() => setSeries([]));
    if (kind === "stock") {
      api.compare(field).then((r) => setPeers(r.items)).catch(() => setPeers([]));
    } else {
      setPeers([]);
    }
  }, [kind, id, field]);

  if (numericFields.length === 0) return null;

  const linePoints = series
    .map((p) => ({ x: (p.event_time ?? p.knowledge_time).slice(0, 10), y: toNumber(p.value) }))
    .filter((p): p is { x: string; y: number } => p.y !== null);
  const bars = peers
    .map((p) => ({ label: p.id, value: toNumber(p.value) ?? 0, warn: p.conflict }))
    .filter((b) => b.value !== 0);

  return (
    <div className="rounded-lg border border-neutral-200 bg-white p-4">
      <div className="mb-2 flex items-center gap-2">
        <h3 className="text-sm font-semibold">图表</h3>
        <select value={field} onChange={(e) => setField(e.target.value)}
          className="rounded border border-neutral-200 px-1.5 py-0.5 font-mono text-xs">
          {numericFields.map((f) => <option key={f} value={f}>{f}</option>)}
        </select>
      </div>
      <div className="mb-1 text-[10px] text-neutral-400">时序（按 event_time / 版本演进）</div>
      <LineChart points={linePoints} />
      {kind === "stock" && bars.length > 0 && (
        <>
          <div className="mb-1 mt-3 text-[10px] text-neutral-400">同字段跨标的对比（最新版本，琥珀色 = 有冲突）</div>
          <BarCompare items={bars} />
        </>
      )}
    </div>
  );
}

// ---------- 冲突裁决面板（详情页）：以此版本为准 + 版本链展开 ----------
function ConflictResolver({ kind, id, field, factId, onResolved }: {
  kind: string; id: string; field: string; factId: string; onResolved: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);  // 正在裁决的 fact_id
  const [versions, setVersions] = useState<SeriesPoint[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const resolve = async (keepFactId: string) => {
    setBusy(keepFactId);
    setError(null);
    try {
      await api.resolveConflict(kind, id, field, keepFactId, "详情页人工裁决");
      onResolved();  // 重载投影：冲突标记清除、裁决值落地
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(null);
    }
  };
  const toggleVersions = () => {
    if (versions) { setVersions(null); return; }
    api.series(kind, id, [field])
      .then((r) => setVersions(r.fields[field] ?? []))
      .catch(() => setVersions([]));
  };

  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] text-amber-700">同一事件时点出现不同值，需人工裁决</span>
        <button disabled={busy !== null} onClick={() => resolve(factId)}
          className="rounded border border-amber-300 bg-white px-2 py-0.5 text-[11px] text-amber-800 hover:bg-amber-100 disabled:opacity-40">
          {busy === factId ? "裁决中…" : "以此版本为准"}
        </button>
        <button onClick={toggleVersions} className="text-[11px] text-neutral-500 hover:underline">
          {versions ? "收起版本链" : "查看版本链"}
        </button>
      </div>
      {error && <div className="text-[11px] text-red-600">裁决失败：{error}</div>}
      {versions && (
        <div className="space-y-1 rounded border border-neutral-200 bg-white p-2">
          {versions.map((v) => (
            <div key={v.fact_id} className="flex flex-wrap items-center gap-2 font-mono text-[11px]">
              <span className="text-neutral-400">v{v.version}</span>
              <span className="max-w-48 truncate" title={JSON.stringify(v.value)}>
                {JSON.stringify(v.value)}
              </span>
              <span className="text-neutral-400">event {v.event_time?.slice(0, 10) ?? "—"}</span>
              <span className="text-neutral-400">known {v.knowledge_time.slice(0, 10)}</span>
              {v.conflict && <span className="text-amber-600">⚠竞争</span>}
              <button disabled={busy !== null} onClick={() => resolve(v.fact_id)}
                className="ml-auto rounded border border-neutral-200 px-1.5 py-0.5 text-[10px] hover:border-amber-400 disabled:opacity-40">
                {busy === v.fact_id ? "…" : "以此版本为准"}
              </button>
            </div>
          ))}
          {versions.length === 0 && <div className="text-[11px] text-neutral-400">（版本链为空）</div>}
        </div>
      )}
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
                  <Fragment key={field}>
                    <tr className="border-b border-neutral-100">
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
                    {f.conflict && (
                      <tr className="border-b border-neutral-100">
                        <td colSpan={5} className="bg-amber-50/40 px-3 py-2">
                          <ConflictResolver kind={kind} id={id} field={field} factId={f.fact_id}
                            onResolved={() => load(asOf ? `${asOf}T23:59:59Z` : undefined)} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <FactCharts kind={kind} id={id} facts={Object.keys(profile?.facts ?? {})} />
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

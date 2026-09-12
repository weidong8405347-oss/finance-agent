// 旧字段视图组件（§10.2：现有 FactValue、历史版本和冲突裁决能力迁入「来源与审计」，
// 与新组件共用状态）。KnowledgePage 旧详情与 StockDossierPage 的审计区共用这里。

import { useState } from "react";
import { api, SeriesPoint } from "../../api";
import { isOfflineExport } from "./api";

// 存量兼容：硬门禁上线前落库的 JSON 字符串值，渲染层解析回结构化（历史不改写，投影可美化）
export function parseLegacyJson(value: string): unknown | null {
  const s = value.trim();
  if (!s || (s[0] !== "{" && s[0] !== "[")) return null;
  try {
    const parsed = JSON.parse(s);
    return parsed !== null && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

// ---------- 结构化字段值渲染（替代 JSON.stringify 一坨；不从文本猜数值） ----------
export function FactValue({ value, compact = false }: { value: unknown; compact?: boolean }) {
  if (value === null || value === undefined) return <span className="text-neutral-400">—</span>;
  if (typeof value === "string") {
    const legacy = parseLegacyJson(value);
    if (legacy !== null) return <FactValue value={legacy} compact={compact} />;
    const text = compact && value.length > 160 ? `${value.slice(0, 160)}…` : value;
    return <span className="whitespace-pre-wrap break-words">{text}</span>;
  }
  if (typeof value === "boolean") {
    return <span>{value ? "✓" : "—"}</span>;
  }
  if (typeof value === "number") {
    return <span className="font-mono tabular-nums">{value.toLocaleString()}</span>;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="text-neutral-400">（空列表）</span>;
    if (value.every((v) => v !== null && typeof v === "object" && !Array.isArray(v))) {
      const dicts = value as Record<string, unknown>[];
      const keys: string[] = [];
      for (const item of dicts) {
        for (const k of Object.keys(item)) {
          if (k !== "evidence_ids" && !keys.includes(k)) keys.push(k);
        }
      }
      return (
        <table className="w-full border-collapse text-xs">
          <thead>
            <tr>{keys.map((k) => (
              <th key={k} className="border-b border-neutral-200 px-1.5 py-1 text-left text-[10px] font-semibold uppercase tracking-wide text-neutral-400">{k}</th>
            ))}</tr>
          </thead>
          <tbody>
            {(compact ? dicts.slice(0, 5) : dicts).map((item, i) => (
              <tr key={i} className="border-b border-neutral-100">
                {keys.map((k) => (
                  <td key={k} className="max-w-56 px-1.5 py-1 align-top">
                    <FactValue value={item[k]} compact />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      );
    }
    return (
      <ul className="list-disc space-y-0.5 pl-4">
        {(compact ? value.slice(0, 6) : value).map((v, i) => (
          <li key={i}><FactValue value={v} compact /></li>
        ))}
      </ul>
    );
  }
  if (typeof value === "object") {
    return (
      <table className="w-full border-collapse text-xs">
        <tbody>
          {Object.entries(value as Record<string, unknown>).map(([k, v]) => (
            <tr key={k} className="border-b border-neutral-100">
              <td className="w-32 px-1.5 py-1 align-top font-mono text-[11px] text-neutral-500">{k}</td>
              <td className="px-1.5 py-1 align-top"><FactValue value={v} compact /></td>
            </tr>
          ))}
        </tbody>
      </table>
    );
  }
  return <span className="font-mono">{String(value)}</span>;
}

// ---------- 冲突裁决面板：以此版本为准 + 版本链展开（v1 契约保留） ----------
export function ConflictResolver({ kind, id, field, factId, onResolved, readOnly = false }: {
  kind: string; id: string; field: string; factId: string;
  onResolved: () => void; readOnly?: boolean;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [versions, setVersions] = useState<SeriesPoint[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // 离线导出（§11.4）：裁决是写操作、版本链来自 v1 在线库——离线只读
  const offline = isOfflineExport();
  const ro = readOnly || offline;

  const resolve = async (keepFactId: string) => {
    setBusy(keepFactId);
    setError(null);
    try {
      await api.resolveConflict(kind, id, field, keepFactId, "详情页人工裁决");
      onResolved();
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
        {ro ? (
          <span className="rounded bg-indigo-50 px-1.5 py-0.5 text-[10px] text-indigo-700">
            {offline
              ? "离线导出：冲突裁决与版本链需在线版（冻结文件不写回）"
              : "历史视图只读——处理当前冲突请切回当前视图（不借用未来裁决）"}
          </span>
        ) : (
          <button disabled={busy !== null} onClick={() => resolve(factId)}
                  className="rounded border border-amber-300 bg-white px-2 py-0.5 text-[11px] text-amber-800 hover:bg-amber-100 disabled:opacity-40">
            {busy === factId ? "裁决中…" : "以此版本为准"}
          </button>
        )}
        {!offline && (
          <button onClick={toggleVersions} className="text-[11px] text-neutral-500 hover:underline">
            {versions ? "收起版本链" : "查看版本链"}
          </button>
        )}
        {readOnly && versions && (
          <span className="text-[10px] text-indigo-600">
            注意：版本链来自 v1 当前库，未按历史 as_of 过滤（仅供审计参考）
          </span>
        )}
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
              {!readOnly && (
                <button disabled={busy !== null} onClick={() => resolve(v.fact_id)}
                        className="ml-auto rounded border border-neutral-200 px-1.5 py-0.5 text-[10px] hover:border-amber-400 disabled:opacity-40">
                  {busy === v.fact_id ? "…" : "以此版本为准"}
                </button>
              )}
            </div>
          ))}
          {versions.length === 0 && <div className="text-[11px] text-neutral-400">（版本链为空）</div>}
        </div>
      )}
    </div>
  );
}

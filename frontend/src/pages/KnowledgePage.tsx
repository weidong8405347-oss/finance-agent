// Knowledge 首页（设计 §4.2）：研究档案列表——每行回答「这是哪家公司、最新研究
// 发现什么、现在缺什么」，而不是只显示字段数。列表请求失败必须显示失败与重试，
// 不能伪装成「暂无档案」。详情阅读进入 StockDossierPage（旧字段视图迁为其
// 「数据与审计」区）。

import { useEffect, useMemo, useState } from "react";

import { api } from "../api";
import { navigate } from "../app/route";
import { dossierApi } from "../features/dossier/api";
import { ModuleStateBadge } from "../features/dossier/components";
import type { EntityRowV2 } from "../features/dossier/types";

type KindFilter = "all" | "stock" | "industry";
type IssueFilter = "all" | "conflict" | "stale" | "researched" | "draft";

function fmtDate(iso: string | null): string {
  return iso ? iso.slice(0, 10) : "—";
}

function VerdictPill({ verdict }: { verdict: string | null }) {
  if (!verdict) return <span className="text-[11px] text-neutral-400">未评估</span>;
  const meta = verdict === "sufficient"
    ? { label: "充分", cls: "border-green-300 bg-green-50 text-green-700" }
    : verdict === "blocked"
      ? { label: "受阻", cls: "border-red-300 bg-red-50 text-red-700" }
      : { label: "部分", cls: "border-amber-300 bg-amber-50 text-amber-700" };
  return (
    <span className={`rounded-full border px-1.5 py-0.5 text-[10px] ${meta.cls}`}>{meta.label}</span>
  );
}

export default function KnowledgePage() {
  const [rows, setRows] = useState<EntityRowV2[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const [kindFilter, setKindFilter] = useState<KindFilter>("all");
  const [issueFilter, setIssueFilter] = useState<IssueFilter>("all");
  const [view, setView] = useState<"table" | "cards">("table");
  const [showPurged, setShowPurged] = useState(false);
  const [purged, setPurged] = useState<{ entity_kind: string; entity_id: string;
                                          mode: string; reason: string;
                                          purged_at: string }[]>([]);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    dossierApi.entities()
      .then((r) => { if (!cancelled) setRows(r); })
      .catch((e) => {
        if (cancelled) return;
        setRows(null);
        setError(e instanceof Error ? e.message : String(e));
      });
    return () => { cancelled = true; };
  }, [tick]);

  // 已删除（墓碑）清单：可审计、可恢复
  useEffect(() => {
    if (!showPurged) return;
    let cancelled = false;
    api.purgedEntities()
      .then((r) => { if (!cancelled) setPurged(r); })
      .catch(() => { if (!cancelled) setPurged([]); });
    return () => { cancelled = true; };
  }, [showPurged, tick]);

  const filtered = useMemo(() => {
    if (!rows) return [];
    return rows.filter((r) => {
      if (kindFilter !== "all" && r.kind !== kindFilter) return false;
      if (issueFilter === "conflict" && r.conflict_count === 0) return false;
      if (issueFilter === "stale" && r.stale_count === 0) return false;
      if (issueFilter === "researched" && r.research_coverage.required === 0) return false;
      if (issueFilter === "draft" && r.quality_status !== "draft") return false;
      return true;
    });
  }, [rows, kindFilter, issueFilter]);

  const openDossier = (r: EntityRowV2) =>
    navigate({ page: "knowledge", kind: r.kind as "stock" | "industry", id: r.id, params: {} });

  // 删除档案（用户诉求：清掉历史低质量内容）：默认墓碑（可恢复），
  // 硬删需二次确认且说清不可恢复——删了什么、多少行、是否清了孤儿证据都回显
  const [purgeMsg, setPurgeMsg] = useState<string | null>(null);
  const onDelete = async (r: EntityRowV2, mode: "tombstone" | "hard") => {
    const label = `${r.kind}:${r.id}`;
    const first = window.confirm(
      mode === "hard"
        ? `彻底删除 ${label}？\n\n将真删事实/观测/论断/计划/产物/快照/决策卡的行，`
          + `并清理只被它引用的孤儿证据与磁盘存档。\n⚠ 不可恢复（事件日志只保留删除审计）。`
        : `删除 ${label}？\n\n默认为墓碑删除：列表与档案页立即不再出现，`
          + `事实/观测行保留可审计，可在「已删除」里恢复。`
    );
    if (!first) return;
    if (mode === "hard" && !window.confirm(`再次确认：彻底删除 ${label} 不可恢复。继续？`)) return;
    const reason = window.prompt("删除原因（进审计记录）", "历史低质量内容") ?? "";
    try {
      const res = await api.deleteEntity(r.kind, r.id, { mode, reason });
      const parts = [
        `${mode === "hard" ? "已彻底删除" : "已删除（墓碑）"} ${label}`,
        res.total_rows_deleted ? `删 ${res.total_rows_deleted} 行` : "未删行",
        res.orphan_evidence_deleted ? `孤儿证据 ${res.orphan_evidence_deleted} 条` : "",
        res.files_removed.length ? `文件 ${res.files_removed.length} 项` : "",
        res.restorable ? "可在「已删除」恢复" : "不可恢复",
      ].filter(Boolean);
      setPurgeMsg(parts.join("；"));
      setTick((t) => t + 1);
    } catch (e) {
      setPurgeMsg(`删除失败：${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const onRestore = async (kind: string, id: string) => {
    try {
      await api.restoreEntity(kind, id, "用户在知识页恢复");
      setPurgeMsg(`已恢复 ${kind}:${id}`);
      setShowPurged(false);
      setTick((t) => t + 1);
    } catch (e) {
      setPurgeMsg(`恢复失败：${e instanceof Error ? e.message : String(e)}`);
    }
  };

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold text-neutral-500">研究档案</h2>
        <div className="flex gap-1">
          {([["all", "全部"], ["stock", "股票"], ["industry", "行业"]] as [KindFilter, string][]).map(([k, label]) => (
            <button key={k} onClick={() => setKindFilter(k)}
                    className={`rounded px-2 py-0.5 text-xs ${kindFilter === k ? "bg-neutral-900 text-white" : "border border-neutral-200 text-neutral-600"}`}>
              {label}
            </button>
          ))}
        </div>
        <select value={issueFilter} onChange={(e) => setIssueFilter(e.target.value as IssueFilter)}
                className="rounded border border-neutral-200 px-1.5 py-0.5 text-xs text-neutral-600">
          <option value="all">全部状态</option>
          <option value="conflict">有冲突</option>
          <option value="stale">待更新</option>
          <option value="researched">已研究（有问题计划）</option>
          <option value="draft">待验收</option>
        </select>
        <div className="ml-auto flex gap-1">
          <button onClick={() => setShowPurged((v) => !v)}
                  className={`rounded px-2 py-0.5 text-xs ${
                    showPurged ? "bg-neutral-900 text-white" : "border border-neutral-200 text-neutral-500"
                  }`}
                  title="已删除（墓碑）清单：可审计、可恢复">
            已删除
          </button>
          {(["table", "cards"] as const).map((v) => (
            <button key={v} onClick={() => setView(v)}
                    className={`rounded px-2 py-0.5 text-xs ${view === v ? "bg-neutral-100 font-semibold text-neutral-800" : "text-neutral-400"}`}>
              {v === "table" ? "紧凑表格" : "阅读列表"}
            </button>
          ))}
        </div>
      </div>

      {purgeMsg && (
        <div className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-neutral-200 bg-neutral-50 px-3 py-2 text-xs text-neutral-700">
          <span>{purgeMsg}</span>
          <button onClick={() => setPurgeMsg(null)}
                  className="ml-auto text-neutral-400 hover:text-neutral-600">✕</button>
        </div>
      )}

      {showPurged && (
        <div className="mb-3 rounded-lg border border-neutral-200 bg-white p-3">
          <div className="mb-2 text-xs font-semibold text-neutral-500">
            已删除（墓碑）——事件日志保留删除审计；tombstone 可恢复，hard 不可恢复
          </div>
          {purged.length === 0
            ? <div className="text-xs text-neutral-400">（无已删除实体）</div>
            : (
              <ul className="space-y-1">
                {purged.map((p) => (
                  <li key={`${p.entity_kind}:${p.entity_id}`}
                      className="flex flex-wrap items-center gap-2 text-xs">
                    <span className="font-mono text-neutral-700">{p.entity_kind}:{p.entity_id}</span>
                    <span className={`rounded-full border px-1.5 py-0.5 text-[10px] ${
                      p.mode === "hard"
                        ? "border-red-200 bg-red-50 text-red-700"
                        : "border-neutral-200 bg-neutral-50 text-neutral-600"
                    }`}>
                      {p.mode === "hard" ? "已彻底删除" : "墓碑"}
                    </span>
                    <span className="text-neutral-400">{fmtDate(p.purged_at)}</span>
                    {p.reason && <span className="text-neutral-500">原因：{p.reason}</span>}
                    {p.mode === "tombstone" && (
                      <button onClick={() => void onRestore(p.entity_kind, p.entity_id)}
                              className="rounded border border-neutral-200 px-1.5 py-0.5 text-[11px] text-neutral-600 hover:border-green-300 hover:text-green-700">
                        恢复
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            )}
        </div>
      )}

      {error && (
        <div className="mb-3 rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700">
          <span className="font-semibold">档案列表读取失败：</span>{error}
          <button onClick={() => setTick((t) => t + 1)}
                  className="ml-3 rounded border border-red-300 bg-white px-2 py-0.5 hover:bg-red-100">
            重试
          </button>
          <span className="ml-2 text-red-400">（不伪装成「暂无档案」）</span>
        </div>
      )}

      {!rows && !error && (
        <div className="py-12 text-center text-sm text-neutral-400">档案库加载中…</div>
      )}

      {rows && rows.length === 0 && !error && (
        <div className="rounded-lg border border-dashed border-neutral-300 py-12 text-center text-sm text-neutral-400">
          暂无档案——在对话里说「研究一下 BE」或用 /research 开始
        </div>
      )}

      {filtered.length > 0 && view === "table" && (
        <div className="overflow-hidden rounded-lg border border-neutral-200 bg-white">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-neutral-200 bg-neutral-50 text-left text-[11px] text-neutral-500">
                  <th className="px-3 py-2">公司/行业</th>
                  <th className="px-3 py-2">研究摘要</th>
                  <th className="px-3 py-2">研究覆盖</th>
                  <th className="px-3 py-2">数据状态</th>
                  <th className="px-3 py-2">字段完整度</th>
                  <th className="px-3 py-2">最近可知</th>
                  <th className="px-3 py-2" />
                </tr>
              </thead>
              <tbody>
                {filtered.map((r) => (
                  <tr key={`${r.kind}:${r.id}`} onClick={() => openDossier(r)}
                      className="cursor-pointer border-b border-neutral-100 hover:bg-neutral-50">
                    <td className="px-3 py-2">
                      <div className="font-mono text-xs font-semibold text-neutral-800">{r.id}</div>
                      <div className="text-[10px] text-neutral-400">
                        {r.kind === "stock" ? "股票" : "行业"}
                        {r.recipe_id ? ` · ${r.recipe_id}` : ""}
                      </div>
                    </td>
                    <td className="max-w-xs px-3 py-2">
                      <div className="truncate text-xs text-neutral-700" title={r.latest_artifact_title ?? ""}>
                        {r.latest_artifact_title ?? <span className="text-neutral-400">（尚无研究产物）</span>}
                      </div>
                      <div className="mt-0.5 flex items-center gap-1.5">
                        <VerdictPill verdict={r.research_coverage.verdict} />
                        <span className="font-mono text-[10px] text-neutral-400">
                          观测 {r.observation_count}
                        </span>
                      </div>
                    </td>
                    <td className="px-3 py-2 font-mono text-xs">
                      {r.research_coverage.required > 0
                        ? `${r.research_coverage.answered}/${r.research_coverage.required}`
                        : "—"}
                    </td>
                    <td className="px-3 py-2">
                      <div className="flex flex-wrap gap-1">
                        {r.conflict_count > 0 && (
                          <button onClick={(e) => { e.stopPropagation(); setIssueFilter("conflict"); }}
                                  className="rounded-full border border-red-300 bg-red-50 px-1.5 py-0.5 text-[10px] text-red-700"
                                  title="点击过滤有冲突的档案">
                            ⚠ {r.conflict_count} 冲突
                          </button>
                        )}
                        {r.stale_count > 0 && (
                          <button onClick={(e) => { e.stopPropagation(); setIssueFilter("stale"); }}
                                  className="rounded-full border border-orange-300 bg-orange-50 px-1.5 py-0.5 text-[10px] text-orange-700">
                            陈旧 {r.stale_count}
                          </button>
                        )}
                        {r.quality_status === "verified" ? (
                          <span className="rounded-full border border-green-300 bg-green-50 px-1.5 py-0.5 text-[10px] text-green-700">✓ 基础校验</span>
                        ) : (
                          <span className="rounded-full border border-amber-300 bg-amber-50 px-1.5 py-0.5 text-[10px] text-amber-700"
                                title={r.quality_issues.join("\n")}>◔ 待验收</span>
                        )}
                      </div>
                    </td>
                    <td className="px-3 py-2">
                      <span className="mr-1 inline-block h-1.5 w-14 overflow-hidden rounded bg-neutral-100 align-middle">
                        <span className={`block h-full ${r.quality_status === "verified" ? "bg-green-600" : "bg-amber-500"}`}
                              style={{ width: `${(r.completeness * 100).toFixed(0)}%` }} />
                      </span>
                      <span className="text-xs text-neutral-500">{(r.completeness * 100).toFixed(0)}%</span>
                    </td>
                    <td className="px-3 py-2 font-mono text-xs text-neutral-500">{fmtDate(r.last_knowledge_time)}</td>
                    <td className="px-3 py-2">
                      <div className="flex items-center gap-1">
                        <button onClick={(e) => { e.stopPropagation(); openDossier(r); }}
                                className="rounded border border-neutral-200 px-2 py-0.5 text-xs hover:border-neutral-400">
                          阅读 →
                        </button>
                        <button onClick={(e) => { e.stopPropagation(); void onDelete(r, "tombstone"); }}
                                title="删除（墓碑：列表与档案页不再出现，可在「已删除」恢复）"
                                className="rounded border border-neutral-200 px-1.5 py-0.5 text-xs text-neutral-400 hover:border-red-300 hover:text-red-600">
                          删除
                        </button>
                        <button onClick={(e) => { e.stopPropagation(); void onDelete(r, "hard"); }}
                                title="彻底删除（真删行 + 孤儿证据 + 磁盘存档；不可恢复）"
                                className="rounded border border-neutral-200 px-1.5 py-0.5 text-xs text-neutral-400 hover:border-red-400 hover:bg-red-50 hover:text-red-700">
                          彻底删
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {filtered.length > 0 && view === "cards" && (
        <div className="grid gap-3 md:grid-cols-2">
          {filtered.map((r) => (
            <button key={`${r.kind}:${r.id}`} onClick={() => openDossier(r)}
                    className="rounded-lg border border-neutral-200 bg-white p-4 text-left hover:border-neutral-400">
              <div className="mb-1 flex items-center gap-2">
                <span className="font-mono text-sm font-bold text-neutral-900">{r.id}</span>
                <span className="text-[10px] text-neutral-400">{r.kind === "stock" ? "股票" : "行业"}{r.recipe_id ? ` · ${r.recipe_id}` : ""}</span>
                <span className="ml-auto"><VerdictPill verdict={r.research_coverage.verdict} /></span>
              </div>
              <div className="mb-2 line-clamp-2 text-xs text-neutral-600">
                {r.latest_artifact_title ?? "尚无研究产物——发起研究后这里显示一句话结论"}
              </div>
              <div className="flex flex-wrap items-center gap-2 text-[10px] text-neutral-400">
                <span>问题覆盖 {r.research_coverage.answered}/{r.research_coverage.required || "—"}</span>
                <span>观测 {r.observation_count}</span>
                {r.conflict_count > 0 && <span className="text-red-600">⚠ {r.conflict_count} 冲突</span>}
                {r.stale_count > 0 && <span className="text-orange-600">陈旧 {r.stale_count}</span>}
                <span className="ml-auto font-mono">{fmtDate(r.last_knowledge_time)}</span>
              </div>
            </button>
          ))}
        </div>
      )}

      {rows && filtered.length === 0 && rows.length > 0 && (
        <div className="rounded-lg border border-dashed border-neutral-300 py-8 text-center text-xs text-neutral-400">
          当前过滤条件下无档案
          <button onClick={() => { setKindFilter("all"); setIssueFilter("all"); }}
                  className="ml-2 text-blue-700 hover:underline">清除过滤</button>
        </div>
      )}
    </div>
  );
}

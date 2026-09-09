// 对话主页（重设计 R2）：侧栏会话列表（收窄可折叠）+ 单列对话流 + 底部 composer。
// 一切内容 = 事件流经 assemble() 装配出的节点投影；SSE 增量驱动，无轮询。
import { useEffect, useMemo, useRef, useState } from "react";
import { api, EventRow, SessionRow } from "../api";
import { assemble } from "../lib/assemble";
import { ChatNodeView } from "../components/nodes";
import Composer from "../components/Composer";

const STATUS_LABEL: Record<string, { text: string; cls: string }> = {
  running: { text: "运行中", cls: "bg-blue-100 text-blue-700" },
  done: { text: "已完成", cls: "bg-green-100 text-green-700" },
  error: { text: "失败", cls: "bg-red-100 text-red-700" },
  // blocked = 研究停滞、管道拦停（不是系统故障，不併入失败）
  blocked: { text: "已拦停", cls: "bg-amber-100 text-amber-700" },
  cancelled: { text: "已取消", cls: "bg-neutral-200 text-neutral-500" },
  idle: { text: "空闲", cls: "bg-neutral-100 text-neutral-500" },
};

function relTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff / 60000);
  if (m < 1) return "刚刚";
  if (m < 60) return `${m} 分钟前`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} 小时前`;
  return `${Math.floor(h / 24)} 天前`;
}

export default function SessionsPage() {
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [events, setEvents] = useState<EventRow[]>([]);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const esRef = useRef<EventSource | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  const refreshSessions = () => api.sessions().then(setSessions).catch(() => {});

  useEffect(() => {
    refreshSessions().then?.(() => {});
    const t = setInterval(refreshSessions, 5000);  // 侧栏低频刷新（会话状态聚合）
    return () => clearInterval(t);
  }, []);

  // 会话事件流：首次全量 + SSE 增量（seq 去重）
  useEffect(() => {
    esRef.current?.close();
    if (!selected) return;
    setEvents([]);
    api.sessionEvents(selected).then(setEvents).catch(() => setEvents([]));
    const es = new EventSource(`/api/sessions/${selected}/stream`);
    es.onmessage = (msg) => {
      const e = JSON.parse(msg.data) as EventRow;
      setEvents((prev) => (prev.some((x) => x.seq === e.seq) ? prev : [...prev, e]));
    };
    esRef.current = es;
    const t = setInterval(refreshSessions, 5000);
    return () => { es.close(); clearInterval(t); };
  }, [selected]);

  const nodes = useMemo(() => assemble(events), [events]);
  const busy = useMemo(() => {
    const s = sessions.find((x) => x.run_id === selected);
    return s?.status === "running";
  }, [sessions, selected]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [nodes.length, nodes.length && nodes[nodes.length - 1].kind === "assistant" ? (nodes[nodes.length - 1] as { content?: string }).content : null]);

  const send = async (text: string) => {
    const r = await api.chat(selected, text);
    if (!selected) {
      setSelected(r.run_id);
      refreshSessions();
    }
  };

  const stop = async () => {
    if (selected) await api.stopSession(selected).catch(() => {});
  };

  // 删除会话（含子 run 与报告目录）：二次确认 + 运行中先停再删，失败原因不吞
  const [deleteMsg, setDeleteMsg] = useState<string | null>(null);
  // 批量选择（用户诉求：一个个删太慢、每次多次点击）
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const toggleCheck = (rid: string) =>
    setChecked((prev) => {
      const next = new Set(prev);
      if (next.has(rid)) next.delete(rid); else next.add(rid);
      return next;
    });
  const quickSelect = (pred: (s: SessionRow) => boolean) =>
    setChecked(new Set(sessions.filter(pred).map((s) => s.run_id)));

  const onDeleteBatch = async () => {
    const ids = [...checked];
    if (!ids.length) return;
    const targets = sessions.filter((s) => checked.has(s.run_id));
    const running = targets.filter((s) => s.status === "running");
    const reason = window.prompt(
      `删除 ${ids.length} 个会话？\n` +
      (running.length ? `⚠ 其中 ${running.length} 个显示运行中（会先停止再删）：\n` +
        running.map((s) => `  - ${s.title || s.run_id}`).join("\n") + "\n" : "") +
      "删除原因（进审计记录）：",
      "批量清理低质量历史",
    );
    if (reason === null) return;
    try {
      if (running.length) {
        for (const s of running) await api.stopSession(s.run_id).catch(() => {});
      }
      const res = await api.batchDeleteSessions(ids, {
        force: running.length > 0, reason,
      });
      const failed = res.results.filter((r) => !r.ok);
      setDeleteMsg(
        `已删 ${res.deleted} 个会话（共 ${res.total_events} 条事件）` +
        (failed.length
          ? `；${failed.length} 个失败：${failed.map((f) => `${f.run_id}（${f.error}）`).join("；")}`
          : ""),
      );
      setChecked(new Set());
      if (selected && checked.has(selected)) { setSelected(null); setEvents([]); }
      refreshSessions();
    } catch (e) {
      setDeleteMsg(`批量删除失败：${e instanceof Error ? e.message : String(e)}`);
    }
  };
  const onDeleteSession = async (s: SessionRow) => {
    const running = s.status === "running";
    const ok = window.confirm(
      `删除会话 ${s.title || s.run_id}？\n\n`
      + `将删除该会话及其全部子 run 的事件与报告文件（不可恢复）。\n`
      + (running ? "⚠ 该会话仍在运行：会先尝试停止再删。\n" : "")
    );
    if (!ok) return;
    try {
      if (running) await api.stopSession(s.run_id).catch(() => {});
      const res = await api.deleteSession(s.run_id, {
        force: running, reason: "用户在会话页删除",
      });
      if (selected === s.run_id) { setSelected(null); setEvents([]); }
      refreshSessions();
      setDeleteMsg(`已删除 ${res.deleted_runs.length} 个 run（共 ${res.total_events} 条事件）`
        + (res.files_removed.length ? `、${res.files_removed.length} 个报告目录` : ""));
    } catch (e) {
      setDeleteMsg(`删除失败：${e instanceof Error ? e.message : String(e)}`);
    }
  };

  return (
    <div className="flex" style={{ height: "calc(100vh - 45px)" }}>
      {/* 侧栏：会话列表（收窄可折叠；子 run 不进列表） */}
      {sidebarOpen && (
        <aside className="w-60 flex-none overflow-y-auto border-r border-neutral-200 bg-white p-2">
          <div className="mb-2 flex items-center justify-between px-1">
            <span className="text-[11px] font-semibold tracking-wide text-neutral-400">
              会话{checked.size > 0 && `（已选 ${checked.size}）`}
            </span>
            <button onClick={() => setSelected(null)}
              className="rounded border border-neutral-200 px-2 py-0.5 text-[11px] text-neutral-600 hover:bg-neutral-50">
              + 新会话
            </button>
          </div>
          {/* 批量操作条：一次确认删多个，不用逐行点 */}
          <div className="mb-2 space-y-1 rounded border border-neutral-200 bg-neutral-50/60 p-1.5">
            <div className="flex flex-wrap gap-1">
              <button onClick={() => quickSelect((s) => s.status === "running" && s.possibly_stale)}
                      title="选中所有「运行中但可能已中断」的僵尸会话"
                      className="rounded border border-neutral-200 bg-white px-1.5 py-0.5 text-[10px] text-neutral-600 hover:border-amber-300 hover:text-amber-700">
                选僵尸
              </button>
              <button onClick={() => quickSelect((s) => s.status === "blocked" || s.status === "error")}
                      title="选中所有已拦停/失败的会话"
                      className="rounded border border-neutral-200 bg-white px-1.5 py-0.5 text-[10px] text-neutral-600 hover:border-red-300 hover:text-red-700">
                选拦停/失败
              </button>
              <button onClick={() => setChecked(new Set(sessions.map((s) => s.run_id)))}
                      className="rounded border border-neutral-200 bg-white px-1.5 py-0.5 text-[10px] text-neutral-600 hover:border-neutral-400">
                全选
              </button>
              {checked.size > 0 && (
                <button onClick={() => setChecked(new Set())}
                        className="rounded border border-neutral-200 bg-white px-1.5 py-0.5 text-[10px] text-neutral-500 hover:bg-neutral-100">
                  清空
                </button>
              )}
            </div>
            {checked.size > 0 && (
              <button onClick={() => void onDeleteBatch()}
                      className="w-full rounded border border-red-200 bg-red-50 px-2 py-1 text-[11px] font-semibold text-red-700 hover:bg-red-100">
                删除所选 {checked.size} 个会话
              </button>
            )}
          </div>
          <ul className="space-y-0.5">
            {sessions.map((s) => {
              const st = STATUS_LABEL[s.status] ?? STATUS_LABEL.idle;
              return (
                <li key={s.run_id}
                    className={`group relative rounded-lg ${
                      selected === s.run_id ? "bg-neutral-100 ring-1 ring-neutral-300" : "hover:bg-neutral-50"
                    }`}>
                  <div
                    onClick={() => setSelected(s.run_id)}
                    role="button"
                    tabIndex={0}
                    onKeyDown={(e) => { if (e.key === "Enter") setSelected(s.run_id); }}
                    className="cursor-pointer px-2.5 py-2 pr-7"
                  >
                    <div className="flex items-start gap-1.5">
                      <input
                        type="checkbox"
                        checked={checked.has(s.run_id)}
                        onClick={(e) => e.stopPropagation()}
                        onChange={() => toggleCheck(s.run_id)}
                        title="勾选后批量删除"
                        className="mt-0.5 h-3 w-3 flex-none accent-red-600"
                      />
                      <span className="min-w-0 flex-1 truncate text-[13px]">{s.title || s.run_id}</span>
                    </div>
                    <div className="mt-1 flex items-center justify-between gap-1">
                      <span className={`rounded px-1.5 py-0.5 text-[10px] ${st.cls}`}>
                        {st.text}
                        {s.status === "running" && s.possibly_stale && "（可能已中断）"}
                      </span>
                      <span className="text-[10px] text-neutral-400">{relTime(s.last_active)}</span>
                    </div>
                    {/* 运行中时把上一条命令的结果单独说清（不拿旧结果当当前状态） */}
                    {s.status === "running" && s.last_outcome && s.last_outcome !== "completed" && (
                      <div className="mt-1 text-[10px] text-neutral-400">
                        上一条命令：{STATUS_LABEL[s.last_outcome]?.text ?? s.last_outcome}
                      </div>
                    )}
                    {(s.status === "error" || s.status === "blocked") && s.status_detail && (
                      <div className="mt-1 select-text break-all text-[11px] text-red-500">{s.status_detail}</div>
                    )}
                  </div>
                  <button
                    onClick={async (e) => {
                      e.stopPropagation();
                      await onDeleteSession(s);
                    }}
                    title={s.status === "running"
                      ? "删除会话（运行中：会先停止再删）"
                      : "删除会话（含子 run 与报告文件）"}
                    className="absolute right-1.5 top-1.5 rounded px-1 py-0.5 text-[11px] text-neutral-300 hover:bg-red-50 hover:text-red-600 group-hover:text-neutral-400"
                  >
                    ✕
                  </button>
                </li>
              );
            })}
            {sessions.length === 0 && <li className="px-2 py-1 text-xs text-neutral-400">暂无会话</li>}
          </ul>
          {deleteMsg && (
            <div className="mt-2 rounded border border-neutral-200 bg-neutral-50 px-2 py-1 text-[11px] text-neutral-600">
              {deleteMsg}
              <button onClick={() => setDeleteMsg(null)}
                      className="ml-1 text-neutral-400 hover:text-neutral-600">✕</button>
            </div>
          )}
        </aside>
      )}

      {/* 对话流 + composer */}
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-center gap-2 border-b border-neutral-100 px-3 py-1.5">
          <button onClick={() => setSidebarOpen(!sidebarOpen)}
            className="rounded px-1.5 py-0.5 font-mono text-xs text-neutral-400 hover:bg-neutral-100">
            {sidebarOpen ? "◂" : "▸"}
          </button>
          <span className="truncate font-mono text-xs text-neutral-400">
            {selected ?? "新会话"}
          </span>
        </div>
        <div className="flex-1 overflow-y-auto">
          <div className="mx-auto max-w-3xl space-y-2.5 px-4 py-5">
            {nodes.map((n) => <ChatNodeView key={n.key} node={n} />)}
            {!selected && nodes.length === 0 && (
              <div className="pt-20 text-center text-sm text-neutral-400">
                <p className="mb-2">直接说事，或敲 <span className="font-mono font-bold">/</span> 唤起 command：</p>
                <p className="font-mono text-xs text-neutral-300">
                  /research 深度研究 · /profile 建档 · /decide 出决策卡 · /evaluate 效果评估
                </p>
              </div>
            )}
            <div ref={bottomRef} />
          </div>
        </div>
        <Composer busy={busy} onSend={send} onStop={stop} />
      </div>
    </div>
  );
}

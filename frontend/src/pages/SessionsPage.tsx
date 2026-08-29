import { useEffect, useRef, useState } from "react";
import { api, EventRow, SessionRow } from "../api";

// 对话式主交互（参考 deepseek-harness）：左侧会话列表，右侧消息流 + 底部输入框。
// 一切交互内容（用户/助手/工具/评审/决策/错误）都是事件流的投影。

const HIDDEN = new Set([
  "turn/start",
  "turn/end",
  "step/start",
  "step/end",
  "tool/call",
  "context/inject",
]);

export default function SessionsPage() {
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [events, setEvents] = useState<EventRow[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const esRef = useRef<EventSource | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  const refreshSessions = () => api.sessions().then(setSessions).catch(() => setSessions([]));

  useEffect(() => {
    refreshSessions();
  }, []);

  useEffect(() => {
    esRef.current?.close();
    if (!selected) return;
    api.sessionEvents(selected).then(setEvents);
    const es = new EventSource(`/api/sessions/${selected}/stream`);
    es.onmessage = (msg) => {
      const e = JSON.parse(msg.data) as EventRow;
      setEvents((prev) => (prev.some((x) => x.seq === e.seq) ? prev : [...prev, e]));
    };
    esRef.current = es;
    return () => es.close();
  }, [selected]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [events.length]);

  const send = async () => {
    const message = draft.trim();
    if (!message || sending) return;
    setSending(true);
    try {
      const r = await api.chat(selected, message);
      setDraft("");
      await refreshSessions();
      if (!selected) setSelected(r.run_id);
      else api.sessionEvents(selected).then(setEvents); // 立刻拉到 user/message
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="grid grid-cols-3 gap-6" style={{ height: "calc(100vh - 130px)" }}>
      <section className="col-span-1 overflow-y-auto">
        <h2 className="mb-3 text-sm font-semibold text-neutral-500">会话</h2>
        <ul className="space-y-1">
          {sessions.map((s) => (
            <li key={s.run_id}>
              <button
                onClick={() => setSelected(s.run_id)}
                className={`w-full rounded border px-3 py-2 text-left font-mono text-xs ${
                  selected === s.run_id
                    ? "border-neutral-900 bg-neutral-900 text-white"
                    : "border-neutral-200 bg-white hover:border-neutral-400"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate">{s.run_id}</span>
                  <StatusBadge status={s.status} />
                </div>
                {s.status === "error" && s.status_detail && (
                  <div className="mt-1 select-text break-all text-[11px] text-red-500">
                    {s.status_detail}
                  </div>
                )}
              </button>
            </li>
          ))}
          {sessions.length === 0 && <li className="text-sm text-neutral-400">暂无会话</li>}
        </ul>
      </section>

      <section className="col-span-2 flex flex-col">
        <div className="flex-1 space-y-2 overflow-y-auto pb-3">
          {events
            .filter((e) => !HIDDEN.has(e.type))
            .map((e) => (
              <ChatEvent key={e.seq} event={e} />
            ))}
          {!selected && (
            <p className="pt-16 text-center text-sm text-neutral-400">
              在下方输入框直接说事：「帮我深度研究一下 AAPL」/「600519 现在可以买吗」
            </p>
          )}
          <div ref={bottomRef} />
        </div>

        <div className="border-t border-neutral-200 pt-3">
          <div className="flex items-end gap-2">
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault();
                  send();
                }
              }}
              placeholder="输入消息…（Enter 发送，Shift+Enter 换行）"
              rows={2}
              className="w-full resize-none rounded-lg border border-neutral-300 px-3 py-2 text-sm focus:border-neutral-500 focus:outline-none"
            />
            <button
              onClick={send}
              disabled={sending || !draft.trim()}
              className="rounded-lg bg-neutral-900 px-4 py-2 text-sm text-white disabled:opacity-40"
            >
              发送
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}

const STATUS_LABEL: Record<string, { text: string; cls: string }> = {
  running: { text: "运行中", cls: "bg-blue-100 text-blue-800" },
  done: { text: "已完成", cls: "bg-green-100 text-green-800" },
  error: { text: "失败", cls: "bg-red-100 text-red-800" },
  cancelled: { text: "已取消", cls: "bg-neutral-200 text-neutral-600" },
};

function StatusBadge({ status }: { status: SessionRow["status"] }) {
  const s = STATUS_LABEL[status] ?? { text: status, cls: "bg-neutral-100" };
  return <span className={`rounded px-1.5 py-0.5 text-[10px] ${s.cls}`}>{s.text}</span>;
}

function ChatEvent({ event }: { event: EventRow }) {
  const [open, setOpen] = useState(false);
  const p = event.payload as Record<string, unknown>;

  if (event.type === "user/message") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[80%] select-text whitespace-pre-wrap rounded-2xl bg-neutral-900 px-4 py-2 text-sm text-white">
          {String(p.content ?? "")}
        </div>
      </div>
    );
  }
  if (event.type === "assistant/message") {
    if (!p.content) return null;
    return (
      <div className="flex justify-start">
        <div className="max-w-[85%] select-text whitespace-pre-wrap rounded-2xl border border-neutral-200 bg-white px-4 py-2 text-sm">
          {String(p.content)}
        </div>
      </div>
    );
  }
  if (event.type === "research/error" || event.type === "decision/error") {
    return (
      <div className="rounded-lg border border-red-300 bg-red-50 px-3 py-2 text-xs">
        <span className="font-mono font-medium text-red-700">✕ {event.type}</span>
        <div className="mt-1 select-text break-all text-red-700">{String(p.reason ?? "")}</div>
      </div>
    );
  }
  if (event.type === "decision/card_issued") {
    return (
      <div className="rounded-lg border border-green-300 bg-green-50 px-3 py-2 text-xs">
        <span className="font-mono font-medium text-green-800">
          ✓ 决策卡 {String(p.action ?? "").toUpperCase()}（{String(p.subject ?? "")}）
        </span>
        <div className="mt-0.5 text-neutral-600">
          信心 {String(p.conviction)} · 详见 Decisions 页
        </div>
      </div>
    );
  }
  if (event.type === "research/round_end") {
    return (
      <div className="rounded border border-neutral-200 bg-neutral-50 px-3 py-1.5 font-mono text-[11px] text-neutral-500">
        第 {String(p.round)} 轮研究完成 · 完整度{" "}
        {(Number(p.completeness_before) * 100).toFixed(0)}% →{" "}
        {(Number(p.completeness_after) * 100).toFixed(0)}%
      </div>
    );
  }
  // 其余事件：可展开的调试卡片（payload 可复制）
  return (
    <div className="rounded border border-neutral-200 bg-white px-3 py-1.5 text-[11px]">
      <button
        onClick={() => setOpen(!open)}
        className="flex w-full items-center justify-between text-left font-mono text-neutral-500"
      >
        <span>
          {open ? "▾" : "▸"} {event.type}
        </span>
        <span className="text-neutral-300">#{event.seq}</span>
      </button>
      {open && (
        <pre className="mt-1 max-h-48 select-text overflow-auto whitespace-pre-wrap break-all rounded bg-neutral-50 p-2 text-neutral-600">
          {JSON.stringify(p, null, 2)}
        </pre>
      )}
    </div>
  );
}

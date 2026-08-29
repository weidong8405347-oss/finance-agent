import { useEffect, useRef, useState } from "react";
import { api, EventRow, SessionRow } from "../api";

// dsh 范式：会话列表 + turn/step 时间线（工具卡片可展开，事件皆可回指 seq）
// 选中会话后通过 SSE 实时推送新事件
export default function SessionsPage() {
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [events, setEvents] = useState<EventRow[]>([]);
  const [ticker, setTicker] = useState("");
  const [needApproval, setNeedApproval] = useState(true);
  const esRef = useRef<EventSource | null>(null);

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

  const startResearch = async () => {
    if (!ticker.trim()) return;
    const r = await api.startResearch(ticker.trim().toUpperCase(), "深度研究", needApproval);
    await refreshSessions();
    setSelected(r.run_id);
  };

  return (
    <div className="grid grid-cols-3 gap-6">
      <section className="col-span-1">
        <h2 className="mb-3 text-sm font-semibold text-neutral-500">会话</h2>
        <div className="mb-3 rounded border border-neutral-200 bg-white p-3">
          <div className="mb-2 text-xs font-medium text-neutral-500">发起研究</div>
          <div className="flex gap-2">
            <input
              value={ticker}
              onChange={(e) => setTicker(e.target.value)}
              placeholder="AAPL"
              className="w-full rounded border border-neutral-300 px-2 py-1 font-mono text-sm"
            />
            <button
              onClick={startResearch}
              className="rounded bg-neutral-900 px-3 py-1 text-xs text-white"
            >
              研究
            </button>
          </div>
          <label className="mt-2 flex items-center gap-1.5 text-xs text-neutral-500">
            <input
              type="checkbox"
              checked={needApproval}
              onChange={(e) => setNeedApproval(e.target.checked)}
            />
            需人工审批（milestone 档）
          </label>
        </div>
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
                <div>{s.run_id}</div>
                <div className="mt-0.5 opacity-70">
                  {s.event_count} events · {new Date(s.started_at).toLocaleString()}
                </div>
              </button>
            </li>
          ))}
          {sessions.length === 0 && (
            <li className="text-sm text-neutral-400">暂无会话</li>
          )}
        </ul>
      </section>
      <section className="col-span-2">
        <h2 className="mb-3 text-sm font-semibold text-neutral-500">时间线</h2>
        <div className="space-y-1.5">
          {events.map((e) => (
            <EventCard key={e.seq} event={e} />
          ))}
          {selected && events.length === 0 && (
            <p className="text-sm text-neutral-400">加载中…</p>
          )}
        </div>
      </section>
    </div>
  );
}

const STYLE: Record<string, string> = {
  "user/message": "border-blue-200 bg-blue-50",
  "assistant/message": "border-neutral-200 bg-white",
  "tool/result": "border-amber-200 bg-amber-50",
  "leakage/attempt": "border-red-300 bg-red-50",
  "hook/verdict": "border-purple-200 bg-purple-50",
  "decision/card_issued": "border-green-200 bg-green-50",
};

function EventCard({ event }: { event: EventRow }) {
  const [open, setOpen] = useState(false);
  const style = STYLE[event.type] ?? "border-neutral-200 bg-white";
  return (
    <button
      onClick={() => setOpen(!open)}
      className={`block w-full rounded border px-3 py-2 text-left text-xs ${style}`}
    >
      <div className="flex items-center justify-between">
        <span className="font-mono font-medium">{event.type}</span>
        <span className="font-mono text-neutral-400">
          #{event.seq} · t{event.turn}/s{event.step}
        </span>
      </div>
      {open && (
        <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all rounded bg-white/70 p-2 font-mono text-[11px] text-neutral-700">
          {JSON.stringify(event.payload, null, 2)}
        </pre>
      )}
    </button>
  );
}

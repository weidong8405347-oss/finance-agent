// Composer：底部常驻输入框。slash 补全（/api/commands）、Send ⇄ Stop 双态、
// 运行中发送 = 排队（消息照常落库，主 agent 串行认领）。
import { useEffect, useRef, useState } from "react";
import { api, CommandSpec } from "../api";

export default function Composer({
  busy,
  onSend,
  onStop,
}: {
  busy: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
}) {
  const [draft, setDraft] = useState("");
  const [commands, setCommands] = useState<CommandSpec[]>([]);
  const [menuIndex, setMenuIndex] = useState(0);
  const taRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    api.commands().then(setCommands).catch(() => setCommands([]));
  }, []);

  const menuOpen = draft.startsWith("/") && !draft.includes(" ") && draft.length > 0;
  const filtered = menuOpen
    ? commands.filter((c) => `/${c.name}`.startsWith(draft.toLowerCase()))
    : [];

  const send = () => {
    const text = draft.trim();
    if (!text) return;
    onSend(text);
    setDraft("");  // 乐观清空（发送在后台进行，可继续输入/排队）
    taRef.current?.focus();
  };

  const pick = (name: string) => {
    setDraft(`/${name} `);
    taRef.current?.focus();
  };

  return (
    <div className="border-t border-neutral-200 bg-white">
      <div className="relative mx-auto max-w-3xl px-4 py-3">
        {menuOpen && filtered.length > 0 && (
          <div className="absolute inset-x-4 bottom-full mb-1 overflow-hidden rounded-lg border border-neutral-200 bg-white shadow-lg">
            {filtered.map((c, i) => (
              <button
                key={c.name}
                onClick={() => pick(c.name)}
                className={`flex w-full items-baseline gap-3 px-3 py-2 text-left ${
                  i === menuIndex ? "bg-neutral-100" : "hover:bg-neutral-50"
                }`}
              >
                <span className="font-mono text-sm font-bold">/{c.name}</span>
                <span className="text-xs text-neutral-500">{c.summary}</span>
                {c.needs_approval && (
                  <span className="ml-auto rounded bg-amber-50 px-1.5 py-0.5 text-[10px] text-amber-700">需审批</span>
                )}
              </button>
            ))}
          </div>
        )}
        <textarea
          ref={taRef}
          value={draft}
          onChange={(e) => { setDraft(e.target.value); setMenuIndex(0); }}
          onKeyDown={(e) => {
            if (menuOpen && filtered.length > 0) {
              if (e.key === "ArrowDown") { e.preventDefault(); setMenuIndex((i) => (i + 1) % filtered.length); return; }
              if (e.key === "ArrowUp") { e.preventDefault(); setMenuIndex((i) => (i - 1 + filtered.length) % filtered.length); return; }
              if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
                e.preventDefault(); pick(filtered[menuIndex].name); return;
              }
              if (e.key === "Escape") { setDraft(""); return; }
            }
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              send();
            }
          }}
          placeholder={busy ? "运行中——发送将排队，或 Stop 中断…" : "输入消息，或 / 唤起 command…（Enter 发送，Shift+Enter 换行）"}
          rows={2}
          className="w-full resize-none rounded-xl border border-neutral-300 px-3.5 py-2.5 text-sm focus:border-neutral-500 focus:outline-none"
        />
        <div className="mt-1.5 flex items-center gap-2">
          <span className="text-[11px] text-neutral-400">
            {busy ? "运行中：新消息将排队" : "空闲"}
          </span>
          <span className="flex-1" />
          {busy && (
            <button onClick={onStop}
              className="rounded-lg border border-red-300 px-4 py-1.5 text-sm text-red-700 hover:bg-red-50">
              ■ Stop
            </button>
          )}
          <button onClick={send} disabled={!draft.trim()}
            className="rounded-lg bg-neutral-900 px-4 py-1.5 text-sm text-white disabled:opacity-40">
            发送
          </button>
        </div>
      </div>
    </div>
  );
}

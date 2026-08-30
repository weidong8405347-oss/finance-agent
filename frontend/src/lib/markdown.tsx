// 迷你 markdown 渲染（零依赖）：##/### 标题、**粗体**、`code`、列表、> 引用、| 表格 |。
// 不追求完整 CommonMark——够用、可读、安全（全部经 React 转义，无 dangerouslySetInnerHTML）。
import React from "react";

function inline(text: string): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  let rest = text, k = 0;
  while (rest.length) {
    const m = rest.match(/\*\*([^*]+)\*\*|`([^`]+)`/);
    if (!m || m.index === undefined) { out.push(rest); break; }
    if (m.index > 0) out.push(rest.slice(0, m.index));
    if (m[1] !== undefined) out.push(<strong key={k++}>{m[1]}</strong>);
    else out.push(<code key={k++} className="rounded bg-neutral-100 px-1 font-mono text-[0.85em]">{m[2]}</code>);
    rest = rest.slice(m.index + m[0].length);
  }
  return out;
}

export function Markdown({ text }: { text: string }) {
  const blocks: React.ReactNode[] = [];
  const lines = text.split("\n");
  let listBuf: string[] = [];
  let tableBuf: string[] = [];
  const flushList = (key: string) => {
    if (!listBuf.length) return;
    blocks.push(
      <ul key={key} className="my-1 list-inside list-disc space-y-0.5">
        {listBuf.map((li, i) => <li key={i}>{inline(li)}</li>)}
      </ul>,
    );
    listBuf = [];
  };
  const flushTable = (key: string) => {
    if (!tableBuf.length) return;
    blocks.push(
      <pre key={key} className="my-1 overflow-auto rounded bg-neutral-50 p-2 font-mono text-xs">{tableBuf.join("\n")}</pre>,
    );
    tableBuf = [];
  };
  lines.forEach((line, i) => {
    const trimmed = line.trimEnd();
    const isTable = /^\|.*\|$/.test(trimmed);
    const isList = /^[-•]\s+|^\d+\.\s+/.test(trimmed);
    if (!isTable) flushTable(`t${i}`);
    if (!isList) flushList(`l${i}`);
    if (isTable) { tableBuf.push(trimmed); return; }
    if (isList) { listBuf.push(trimmed.replace(/^[-•]\s+|^\d+\.\s+/, "")); return; }
    if (trimmed.startsWith("###")) {
      blocks.push(<div key={i} className="mt-2 text-sm font-semibold">{inline(trimmed.replace(/^#+\s*/, ""))}</div>);
    } else if (trimmed.startsWith("#")) {
      blocks.push(<div key={i} className="mt-2 text-base font-semibold">{inline(trimmed.replace(/^#+\s*/, ""))}</div>);
    } else if (trimmed.startsWith(">")) {
      blocks.push(<div key={i} className="my-1 border-l-2 border-neutral-300 pl-3 text-neutral-600">{inline(trimmed.slice(1))}</div>);
    } else if (trimmed) {
      blocks.push(<p key={i} className="my-1">{inline(trimmed)}</p>);
    }
  });
  flushList("end-l"); flushTable("end-t");
  return <>{blocks}</>;
}

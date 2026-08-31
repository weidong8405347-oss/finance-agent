// 对话流节点渲染器：只消费 assemble() 产出的节点（投影），不碰原始事件。
import { useState } from "react";
import { ChatNode, ProfileCardData, StepNode, outcomeLabel } from "../lib/assemble";
import { Markdown } from "../lib/markdown";
import { api } from "../api";

function Arrow({ open }: { open: boolean }) {
  return <span className="mr-1 inline-block w-3 font-mono text-neutral-400">{open ? "▾" : "▸"}</span>;
}

// ---------- 用户 / 助手 ----------
function UserNode({ node }: { node: Extract<ChatNode, { kind: "user" }> }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[80%] select-text whitespace-pre-wrap rounded-2xl bg-neutral-900 px-4 py-2 text-sm text-white">
        {node.content}
      </div>
    </div>
  );
}

function AssistantNode({ node }: { node: Extract<ChatNode, { kind: "assistant" }> }) {
  return (
    <div className="flex justify-start">
      <div className="max-w-[88%]">
        <div className="select-text rounded-2xl border border-neutral-200 bg-white px-4 py-2.5 text-sm leading-relaxed">
          <Markdown text={node.content} />
          {node.live && <span className="ml-0.5 inline-block h-3.5 w-1.5 animate-pulse bg-neutral-400" />}
        </div>
        {node.model && (
          <div className="mt-0.5 pl-2 font-mono text-[10px] text-neutral-400">{node.model}</div>
        )}
      </div>
    </div>
  );
}

// ---------- 工具卡（call/result 配对折叠） ----------
function ToolNode({ node }: { node: Extract<ChatNode, { kind: "tool" }> }) {
  const [open, setOpen] = useState(false);
  const argsPreview = JSON.stringify(node.args ?? {});
  const resultPreview = node.result === undefined
    ? "运行中…"
    : node.result.length > 120
      ? node.result.slice(0, 120) + "…"
      : node.result;
  return (
    <div className={`rounded-lg border text-xs ${node.resultError ? "border-red-300 bg-red-50" : "border-neutral-200 bg-white"}`}>
      <button onClick={() => setOpen(!open)} className="flex w-full items-center px-3 py-1.5 text-left font-mono">
        <Arrow open={open} />
        <span className="font-semibold text-neutral-800">{node.name}</span>
        <span className="ml-2 truncate text-neutral-500">{argsPreview.slice(0, 80)}</span>
        <span className="ml-auto pl-2 text-neutral-400">
          {node.result === undefined ? "⏳" : node.resultError ? "✕" : "✓"}
        </span>
      </button>
      {!open && node.result !== undefined && (
        <div className={`truncate border-t px-3 py-1 font-mono text-[11px] ${node.resultError ? "border-red-200 text-red-600" : "border-neutral-100 text-neutral-400"}`}>
          {resultPreview}
        </div>
      )}
      {open && (
        <div className="space-y-1 border-t border-neutral-100 p-2">
          <div className="text-[10px] text-neutral-400">arguments</div>
          <pre className="select-text overflow-auto whitespace-pre-wrap break-all rounded bg-neutral-50 p-2 text-neutral-600">{JSON.stringify(node.args, null, 2)}</pre>
          {node.result !== undefined && (
            <>
              <div className="text-[10px] text-neutral-400">result</div>
              <pre className="max-h-56 select-text overflow-auto whitespace-pre-wrap break-all rounded bg-neutral-50 p-2 text-neutral-600">{node.result}</pre>
            </>
          )}
        </div>
      )}
    </div>
  );
}

// ---------- ProfileCard（show_profile 内联档案卡） ----------
function ProfileCard({ card }: { card: ProfileCardData }) {
  return (
    <div className="rounded-lg border border-neutral-200 bg-white p-3 text-xs">
      <div className="mb-1 flex items-center gap-2">
        <span className="font-mono font-semibold">{card.entity}</span>
        <span className="text-neutral-500">完整度 {(card.completeness * 100).toFixed(0)}% · {card.fact_count} 字段</span>
        {card.conflicts.length > 0 && (
          <span className="rounded bg-amber-50 px-1.5 py-0.5 text-[10px] text-amber-700">冲突 {card.conflicts.length}</span>
        )}
        <a className="ml-auto text-blue-700 hover:underline" href="#knowledge"
           onClick={(e) => { e.preventDefault(); window.dispatchEvent(new CustomEvent("nav", { detail: "knowledge" })); }}>
          查看完整档案 →
        </a>
      </div>
      {card.thesis && <div className="line-clamp-3 text-neutral-600">{card.thesis}</div>}
    </div>
  );
}

// ---------- CommandCard（嵌套 StepAgentCard + 报告折叠卡） ----------
function StepCard({ step }: { step: StepNode }) {
  const [open, setOpen] = useState(step.status === "running");
  const dot = step.status === "running" ? "bg-blue-600 animate-pulse"
    : step.status === "completed" ? "bg-green-600"
    : step.status === "error" || step.status === "blocked" ? "bg-red-600" : "bg-neutral-300";
  return (
    <div className="rounded-md border border-neutral-200 bg-neutral-50/50">
      <button onClick={() => setOpen(!open)} className="flex w-full items-center px-2.5 py-1.5 text-left text-xs">
        <Arrow open={open} />
        <span className={`mr-1.5 inline-block h-2 w-2 rounded-full ${dot}`} />
        <b>{step.title}</b>
        <span className="ml-2 text-neutral-500">{step.summary ?? step.progress[step.progress.length - 1] ?? ""}</span>
      </button>
      {open && (
        <div className="max-h-40 space-y-0.5 overflow-auto border-t border-neutral-200 px-3 py-1.5 font-mono text-[11px] text-neutral-500">
          {step.progress.map((p, i) => <div key={i}>{p}</div>)}
          {step.progress.length === 0 && <div>（无进度事件）</div>}
        </div>
      )}
    </div>
  );
}

const STAT_LABEL: Record<string, string> = {
  mean_net_return: "平均净收益", kb_delta: "知识库增量", leakage_events: "穿越事件",
  n_complete: "决策点", hit_rate: "胜率",
};

function ReportFoldCard({ card }: { card: import("../lib/assemble").ReportCard }) {
  const [open, setOpen] = useState(false);
  const [full, setFull] = useState<string | null>(null);
  const isEval = card.reportKind === "evaluation";
  const toggle = () => {
    const next = !open;
    setOpen(next);
    // 展开时在线拉取报告全文（artifact_ref → /api/reports/<child_run>/<file>）
    if (next && full === null) {
      const ref = card.artifactRef;
      if (ref) api.reportText(ref).then(setFull).catch(() => setFull("（读取失败：" + card.artifact + "）"));
    }
  };
  const border = isEval
    ? card.verdict === "clean" ? "border-blue-300 bg-blue-50/40" : "border-red-300 bg-red-50/40"
    : "border-green-200 bg-green-50/40";
  return (
    <div className={`rounded-md border ${border}`}>
      <button onClick={toggle} className="flex w-full items-center px-2.5 py-1.5 text-left text-xs">
        <Arrow open={open} />
        <span>{isEval ? "📊" : "📄"} <b>{card.title}</b></span>
        {isEval && card.verdict && (
          <span className={`ml-2 rounded px-1.5 py-0.5 font-mono text-[10px] ${
            card.verdict === "clean" ? "bg-blue-100 text-blue-800" : "bg-red-100 text-red-700"
          }`}>
            {card.verdict === "clean" ? "干净（无穿越）" : "已污染"}
          </span>
        )}
        <span className="ml-2 flex gap-1">
          {card.flags.map((f) => (
            <span key={f} className="rounded border border-amber-200 bg-amber-50 px-1 py-0.5 font-mono text-[10px] text-amber-700">{f}</span>
          ))}
        </span>
      </button>
      <div className="px-3 pb-2 text-xs text-neutral-600">{card.summary}</div>
      {isEval && card.stats && Object.keys(card.stats).length > 0 && (
        <div className="grid grid-cols-3 gap-2 px-3 pb-2">
          {Object.entries(card.stats).map(([k, v]) => (
            <div key={k} className="rounded bg-white/70 p-1.5 text-center">
              <div className="font-mono text-sm font-semibold">
                {typeof v === "number" && Math.abs(v) < 2 ? (v * 100).toFixed(1) + "%" : v}
              </div>
              <div className="text-[10px] text-neutral-500">{STAT_LABEL[k] ?? k}</div>
            </div>
          ))}
        </div>
      )}
      {open && (
        <div className="max-h-72 overflow-auto border-t border-green-200 px-3 py-2 text-xs text-neutral-700">
          {full === null ? <span className="text-neutral-400">加载中…</span> : <Markdown text={full} />}
        </div>
      )}
    </div>
  );
}

function CommandCard({ node }: { node: Extract<ChatNode, { kind: "command" }> }) {
  const running = node.outcome === undefined;
  const badge = running
    ? "bg-blue-100 text-blue-800"
    : node.outcome === "completed"
      ? "bg-green-100 text-green-800"
      : "bg-red-100 text-red-800";
  const doneSteps = node.steps.filter((s) => s.status !== "running").length;
  return (
    <div className="rounded-lg border border-neutral-300 bg-white">
      <div className="flex items-center gap-2 px-3 py-2">
        <span className="font-mono text-sm font-bold">{node.raw}</span>
        <span className={`rounded px-1.5 py-0.5 text-[10px] ${badge}`}>{outcomeLabel(node.outcome)}</span>
        <span className="ml-auto font-mono text-[10px] text-neutral-400">{doneSteps}/{node.steps.length || "?"} 步</span>
      </div>
      {(node.steps.length > 0 || running) && (
        <div className="px-3 pb-1">
          <div className="h-1 overflow-hidden rounded bg-neutral-100">
            <div className="h-full rounded bg-neutral-800 transition-all"
                 style={{ width: node.steps.length ? `${(doneSteps / node.steps.length) * 100}%` : "8%" }} />
          </div>
        </div>
      )}
      <div className="space-y-1.5 px-3 pb-2 pt-1.5">
        {node.steps.map((s) => <StepCard key={s.childRunId} step={s} />)}
        {node.reports.map((r, i) => <ReportFoldCard key={i} card={r} />)}
        {node.outcome && node.outcome !== "completed" && node.summary && (
          <div className="px-1 pt-1 text-xs text-neutral-500">{node.summary}</div>
        )}
      </div>
    </div>
  );
}

// ---------- ApprovalCard（内联审批，SSE 驱动） ----------
function ApprovalCard({ node }: { node: Extract<ChatNode, { kind: "approval" }> }) {
  const [busy, setBusy] = useState(false);
  if (node.state === "waived") {
    return (
      <div className="rounded-lg border border-neutral-200 bg-neutral-50 px-3 py-1.5 font-mono text-[11px] text-neutral-500">
        审批豁免（当次）：{node.op} · 依据 {node.basis}
      </div>
    );
  }
  if (node.state !== "pending") {
    return (
      <div className="rounded-lg border border-neutral-200 bg-neutral-50 px-3 py-1.5 text-xs text-neutral-500">
        审批 {node.op}：{node.state === "approved" ? "✓ 已允许" : "✕ 已拒绝"}
      </div>
    );
  }
  const decide = async (approved: boolean) => {
    setBusy(true);
    try { await api.decideApproval(node.approvalId, approved); } finally { setBusy(false); }
  };
  return (
    <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2.5">
      <div className="text-xs font-semibold text-amber-800">审批 · {node.op} 需要确认</div>
      <div className="mb-2 mt-0.5 font-mono text-[11px] text-neutral-600">{node.detail}</div>
      <div className="flex gap-2">
        <button disabled={busy} onClick={() => decide(true)}
          className="rounded-md bg-neutral-900 px-3 py-1 text-xs text-white disabled:opacity-40">允许一次</button>
        <button disabled={busy} onClick={() => decide(false)}
          className="rounded-md border border-red-300 px-3 py-1 text-xs text-red-700 disabled:opacity-40">拒绝</button>
      </div>
    </div>
  );
}

// ---------- 其余 ----------
function ErrorNode({ node }: { node: Extract<ChatNode, { kind: "error" }> }) {
  return (
    <div className="rounded-lg border border-red-300 bg-red-50 px-3 py-2 text-xs">
      <span className="font-mono font-medium text-red-700">✕ {node.label}</span>
      <div className="mt-0.5 select-text break-all text-red-700">{node.reason}</div>
    </div>
  );
}

function DebugNode({ node }: { node: Extract<ChatNode, { kind: "debug" }> }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded border border-neutral-200 bg-white px-2.5 py-1 text-[11px]">
      <button onClick={() => setOpen(!open)} className="flex w-full items-center font-mono text-neutral-400">
        <Arrow open={open} />{node.type}<span className="ml-auto">#{node.seq}</span>
      </button>
      {open && <pre className="mt-1 max-h-44 select-text overflow-auto whitespace-pre-wrap break-all rounded bg-neutral-50 p-2 text-neutral-600">{node.payload}</pre>}
    </div>
  );
}

function TurnFold({ node }: { node: Extract<ChatNode, { kind: "turnfold" }> }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button onClick={() => setOpen(!open)} className="flex w-full items-center gap-2 py-0.5 font-mono text-[11px] text-neutral-400 hover:text-neutral-600">
        <Arrow open={open} />
        <span>
          {node.tools.length} 次工具调用
          {node.tokens != null ? ` · ${(node.tokens / 1000).toFixed(1)}k tok` : ""}
          {node.model ? ` · ${node.model}` : ""}
        </span>
        <span className="flex-1 border-t border-neutral-200" />
      </button>
      {open && <div className="space-y-1.5 pb-1">{node.tools.map((t) => <ToolNode key={t.key} node={t} />)}</div>}
    </div>
  );
}

export function ChatNodeView({ node }: { node: ChatNode }) {
  switch (node.kind) {
    case "user": return <UserNode node={node} />;
    case "assistant": return <AssistantNode node={node} />;
    case "tool":
      return node.profileCard ? <ProfileCard card={node.profileCard} /> : <ToolNode node={node} />;
    case "command": return <CommandCard node={node} />;
    case "report": return <ReportFoldCard card={node} />;
    case "approval": return <ApprovalCard node={node} />;
    case "error": return <ErrorNode node={node} />;
    case "turnfold": return <TurnFold node={node} />;
    default: return <DebugNode node={node} />;
  }
}

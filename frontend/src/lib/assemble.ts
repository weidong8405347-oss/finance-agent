// 装配层（简化版 dsh ui-conversation Definition/Location）：事件流 → 节点列表。
// 渲染器只消费节点，不消费原始事件——「UI 是投影」在前端的落地，也是加节点的扩展点。

export interface EventRow {
  seq: number;
  type: string;
  turn: number;
  step: number;
  payload: Record<string, any>;
  ts: string;
}

export type ChatNode =
  | { kind: "user"; key: string; content: string; ts: string; turn: number }
  | { kind: "assistant"; key: string; content: string; live: boolean; turn: number; model?: string }
  | { kind: "tool"; key: string; callId: string; name: string; args: unknown; result?: string; resultError: boolean; turn: number; profileCard?: ProfileCardData }
  | { kind: "command"; key: string; commandId: string; name: string; raw: string; outcome?: string; summary?: string; steps: StepNode[]; reports: ReportCard[] }
  | { kind: "report"; key: string; title: string; summary: string; flags: string[]; artifact: string }
  | { kind: "approval"; key: string; approvalId: string; op: string; detail: string; state: "pending" | "approved" | "rejected" | "waived"; basis?: string }
  | { kind: "error"; key: string; label: string; reason: string }
  | { kind: "debug"; key: string; type: string; payload: string; seq: number }
  | { kind: "turnfold"; key: string; turn: number; tools: Extract<ChatNode, { kind: "tool" }>[]; tokens: number | null; model?: string };

export interface StepNode {
  childRunId: string;
  step: string;
  title: string;
  index: number;
  total: number;
  status: "running" | "completed" | "error" | "blocked" | "cancelled";
  summary?: string;
  progress: string[];
}

export interface ReportCard {
  title: string;
  summary: string;
  flags: string[];
  artifact: string;
  artifactRef?: string;  // /api/reports/<child_run_id>/<file> —— 在线阅读全文
}

export interface ProfileCardData {
  entity: string;
  completeness: number;
  fact_count: number;
  thesis?: string | null;
  conflicts: string[];
}

const HIDDEN = new Set([
  "turn/start", "turn/end", "step/start", "step/end",
  "context/inject", "session/title", "tool/result", // tool/result 并入 tool 节点
]);

const OUTCOME_LABEL: Record<string, string> = {
  completed: "已完成", blocked: "被门禁阻断", cancelled: "已停止", error: "出错",
  rejected: "已拒绝", usage_error: "用法错误", unknown: "未知命令", needs_config: "需要配置",
};

export function outcomeLabel(o?: string): string {
  return OUTCOME_LABEL[o ?? ""] ?? "运行中";
}

export function assemble(events: EventRow[]): ChatNode[] {
  const nodes: ChatNode[] = [];
  const toolByCallId = new Map<string, Extract<ChatNode, { kind: "tool" }>>();
  const commandById = new Map<string, Extract<ChatNode, { kind: "command" }>>();
  const approvalById = new Map<string, Extract<ChatNode, { kind: "approval" }>>();
  const stepByChildRun = new Map<string, StepNode>();
  // 每个 (turn,step) 的最终 assistant/message 到达后，其 chunk 缓冲作废
  const finalizedSteps = new Set<string>();
  let liveAssistant: Extract<ChatNode, { kind: "assistant" }> | null = null;
  const closedTurns = new Set<number>();
  const turnTokens = new Map<number, number>();
  const turnModels = new Map<number, string>();

  for (const e of events) {
    const p = e.payload ?? {};
    const stepKey = `${e.turn}:${e.step}`;
    switch (e.type) {
      case "user/message":
        nodes.push({ kind: "user", key: `u${e.seq}`, content: String(p.content ?? ""), ts: e.ts, turn: e.turn });
        break;
      case "assistant/chunk": {
        if (finalizedSteps.has(stepKey)) break;
        if (liveAssistant && liveAssistant.key === `live-${e.turn}-${e.step}`) {
          liveAssistant.content += String(p.text ?? "");
        } else {
          liveAssistant = { kind: "assistant", key: `live-${e.turn}-${e.step}`, content: String(p.text ?? ""), live: true, turn: e.turn };
          nodes.push(liveAssistant);
        }
        break;
      }
      case "assistant/message": {
        finalizedSteps.add(stepKey);
        const content = String(p.content ?? "");
        const model = p.model ? String(p.model) : undefined;
        if (liveAssistant && liveAssistant.key === `live-${e.turn}-${e.step}`) {
          liveAssistant.content = content;
          liveAssistant.live = false;
          liveAssistant.model = model;
          liveAssistant = null;
        } else if (content) {
          nodes.push({ kind: "assistant", key: `a${e.seq}`, content, live: false, turn: e.turn, model });
        }
        break;
      }
      case "tool/call": {
        const node = {
          kind: "tool" as const, key: `t${e.seq}`, callId: String(p.call_id ?? e.seq),
          name: String(p.name ?? ""), args: p.arguments, resultError: false, turn: e.turn,
        };
        toolByCallId.set(node.callId, node);
        nodes.push(node);
        break;
      }
      case "tool/result": {
        const callId = String(p.call_id ?? "");
        const node = toolByCallId.get(callId);
        if (!node) break;
        const content = String(p.content ?? "");
        node.result = content;
        node.resultError = content.startsWith("error") || content.startsWith("rejected");
        if (node.name === "show_profile" && !node.resultError) {
          try { node.profileCard = JSON.parse(content) as ProfileCardData; } catch { /* 保持原文 */ }
        }
        break;
      }
      case "command/run": {
        const node = {
          kind: "command" as const, key: `c${e.seq}`, commandId: String(p.command_id),
          name: String(p.name ?? ""), raw: String(p.raw_input ?? `/${p.name}`),
          steps: [] as StepNode[], reports: [] as ReportCard[],
        };
        commandById.set(node.commandId, node);
        nodes.push(node);
        break;
      }
      case "step_agent/start": {
        const cmd = commandById.get(String(p.command_id));
        const step: StepNode = {
          childRunId: String(p.child_run_id), step: String(p.step),
          title: String(p.title ?? p.step), index: Number(p.index ?? 0), total: Number(p.total ?? 0),
          status: "running", progress: [],
        };
        stepByChildRun.set(step.childRunId, step);
        cmd?.steps.push(step);
        break;
      }
      case "step_agent/progress": {
        const step = stepByChildRun.get(String(p.child_run_id));
        if (step) step.progress.push(String(p.summary ?? ""));
        break;
      }
      case "step_agent/end": {
        const step = stepByChildRun.get(String(p.child_run_id));
        if (step) {
          step.status = (p.status as StepNode["status"]) ?? "completed";
          step.summary = String(p.summary ?? "");
        }
        break;
      }
      case "report/published": {
        const card: ReportCard = {
          title: String(p.title ?? "研究报告"), summary: String(p.summary ?? ""),
          flags: (p.quality_flags as string[]) ?? [], artifact: String(p.artifact_path ?? ""),
          artifactRef: p.artifact_ref ? String(p.artifact_ref) : undefined,
        };
        const cmd = p.command_id ? commandById.get(String(p.command_id)) : undefined;
        if (cmd) cmd.reports.push(card);
        else nodes.push({ kind: "report", key: `r${e.seq}`, ...card });
        break;
      }
      case "command/done": {
        const cmd = commandById.get(String(p.command_id));
        if (cmd) {
          cmd.outcome = String(p.outcome ?? "");
          cmd.summary = String(p.summary ?? "");
        }
        break;
      }
      case "approval/asked": {
        const node = {
          kind: "approval" as const, key: `ap${e.seq}`, approvalId: String(p.approval_id),
          op: String(p.detail?.op ?? ""), detail: JSON.stringify(p.detail ?? {}),
          state: "pending" as const,
        };
        approvalById.set(node.approvalId, node);
        nodes.push(node);
        break;
      }
      case "approval/decided": {
        const node = approvalById.get(String(p.approval_id));
        if (node) node.state = p.approved ? "approved" : "rejected";
        break;
      }
      case "approval/waived": {
        nodes.push({
          kind: "approval", key: `aw${e.seq}`, approvalId: "", op: String(p.op ?? ""),
          detail: "", state: "waived", basis: String(p.basis ?? ""),
        });
        break;
      }
      case "turn/error":
        nodes.push({ kind: "error", key: `e${e.seq}`, label: "turn/error", reason: String(p.reason ?? "") });
        break;
      case "turn/end":
        if (e.turn > 0) {
          closedTurns.add(e.turn);
          const total = (p.usage as { total_tokens?: number } | undefined)?.total_tokens;
          if (total) turnTokens.set(e.turn, (turnTokens.get(e.turn) ?? 0) + total);
          if (p.model) turnModels.set(e.turn, String(p.model));
        }
        break;
      default: {
        if (HIDDEN.has(e.type)) break;
        if (e.type.endsWith("/error")) {
          nodes.push({ kind: "error", key: `e${e.seq}`, label: e.type, reason: String(p.reason ?? "") });
          break;
        }
        if (e.type === "fact/asserted" || e.type === "hook/verdict") break; // 低频审计类：折叠进 turn 过程
        nodes.push({ kind: "debug", key: `d${e.seq}`, type: e.type, payload: JSON.stringify(p, null, 2), seq: e.seq });
      }
    }
  }

  // ---- turn 折叠后处理（dsh turn-process folding 简化版） ----
  // 已关闭 turn 的工具卡折进「N 次工具调用」控件；最终回答露在外面。
  const out: ChatNode[] = [];
  const foldBuffer = new Map<number, Extract<ChatNode, { kind: "tool" }>[]>();
  const flush = (turn: number) => {
    const buf = foldBuffer.get(turn);
    if (buf && buf.length) {
      out.push({
        kind: "turnfold", key: `tf-${turn}`, turn, tools: buf,
        tokens: turnTokens.get(turn) ?? null, model: turnModels.get(turn),
      });
    }
    foldBuffer.delete(turn);
  };
  for (const node of nodes) {
    const t = (node as { turn?: number }).turn ?? 0;
    if (node.kind === "tool" && t > 0 && closedTurns.has(t)) {
      const buf = foldBuffer.get(t) ?? [];
      buf.push(node);
      foldBuffer.set(t, buf);
      continue;
    }
    if (t > 0 && foldBuffer.has(t)) flush(t);
    out.push(node);
  }
  for (const t of [...foldBuffer.keys()]) flush(t);
  return out;
}

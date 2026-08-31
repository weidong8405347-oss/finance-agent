// 装配层契约（L4 核心：事件流 → 节点的投影正确性）
import { describe, expect, it } from "vitest";
import { assemble, EventRow } from "../assemble";

let seq = 0;
const ev = (type: string, payload: Record<string, unknown> = {}, turn = 0, step = 0): EventRow => ({
  seq: ++seq, type, payload: payload as EventRow["payload"], turn, step,
  ts: "2026-08-30T00:00:00Z",
});

describe("assemble", () => {
  it("user/assistant 气泡 + streaming chunk 合并为终稿", () => {
    const nodes = assemble([
      ev("user/message", { content: "你好" }),
      ev("assistant/chunk", { text: "你" }, 1, 1),
      ev("assistant/chunk", { text: "好呀" }, 1, 1),
      ev("assistant/message", { content: "好呀（终稿）", model: "mock" }, 1, 1),
      ev("turn/end", { usage: { total_tokens: 15 }, model: "mock" }, 1),
    ]);
    const texts = nodes.filter((n) => n.kind === "assistant");
    expect(texts).toHaveLength(1);
    expect(texts[0]).toMatchObject({ content: "好呀（终稿）", live: false, model: "mock" });
  });

  it("tool/call 与 tool/result 按 call_id 配对；错误结果标红", () => {
    const nodes = assemble([
      ev("user/message", { content: "查" }),
      ev("tool/call", { call_id: "c1", name: "query_kb", arguments: {} }, 1, 1),
      ev("tool/result", { call_id: "c1", name: "query_kb", content: "error: boom" }, 1, 1),
    ]);
    const tool = nodes.find((n) => n.kind === "tool");
    expect(tool).toMatchObject({ name: "query_kb", result: "error: boom", resultError: true });
  });

  it("command 卡片聚合 step 生命周期与进度桥接", () => {
    const nodes = assemble([
      ev("command/run", { command_id: "cmd-1", name: "research", raw_input: "/research BE" }),
      ev("step_agent/start", { command_id: "cmd-1", child_run_id: "r--1", step: "research", title: "S1 研究", index: 1, total: 2 }),
      ev("step_agent/progress", { child_run_id: "r--1", summary: "第 1 轮完成" }),
      ev("step_agent/end", { command_id: "cmd-1", child_run_id: "r--1", step: "research", status: "completed", summary: "done" }),
      ev("command/done", { command_id: "cmd-1", outcome: "completed", summary: "ok" }),
    ]);
    const cmd = nodes.find((n) => n.kind === "command");
    expect(cmd).toMatchObject({ name: "research", outcome: "completed" });
    expect(cmd?.steps[0]).toMatchObject({ step: "research", status: "completed", progress: ["第 1 轮完成"] });
  });

  it("已关闭 turn 的工具卡折叠进 turnfold，最终回答外露", () => {
    const nodes = assemble([
      ev("user/message", { content: "go" }),
      ev("tool/call", { call_id: "c1", name: "query_kb", arguments: {} }, 1, 1),
      ev("tool/result", { call_id: "c1", content: "{}" }, 1, 1),
      ev("assistant/message", { content: "结论", model: "m" }, 1, 2),
      ev("turn/end", { usage: { total_tokens: 42 }, model: "m" }, 1),
    ]);
    const fold = nodes.find((n) => n.kind === "turnfold");
    expect(fold).toMatchObject({ turn: 1, tokens: 42, model: "m" });
    expect(nodes.filter((n) => n.kind === "tool")).toHaveLength(0); // 折进去了
    expect(nodes.some((n) => n.kind === "assistant" && n.content === "结论")).toBe(true);
  });

  it("审批生命周期：asked → decided 内联状态迁移", () => {
    const nodes = assemble([
      ev("approval/asked", { approval_id: "a1", detail: { op: "/evaluate" } }),
      ev("approval/decided", { approval_id: "a1", approved: false }),
    ]);
    expect(nodes[0]).toMatchObject({ kind: "approval", state: "rejected" });
  });
});

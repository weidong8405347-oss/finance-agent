// API client：所有数据都是后端投影（字段可回指 event/fact id）
export interface SessionRow {
  run_id: string;
  title: string | null;
  started_at: string;
  last_active: string;
  /** 活动状态：运行中 > 错误 > 拦停 > 取消 > 完成 > 空闲（与上一条命令结果分开） */
  status: "idle" | "running" | "done" | "error" | "blocked" | "cancelled";
  status_detail: string | null;
  last_outcome: string | null;
  running_commands: string[];
  open_turns: number;
  last_blocked: string | null;
  last_error: string | null;
  /** 运行中但超过阈值无事件：进程可能被杀，诚实标注（不假装活着） */
  possibly_stale: boolean;
}

export interface CommandSpec {
  name: string;
  summary: string;
  usage: string;
  needs_approval: boolean;
}

export interface ChildRun {
  run_id: string;
  step: string;
  kind: string;
  command_id: string;
  started_at: string;
  status: string;
}

export interface ApprovalRow {
  approval_id: string;
  run_id: string;
  detail: Record<string, unknown>;
  created_at: string;
}

export interface EventRow {
  seq: number;
  type: string;
  turn: number;
  step: number;
  payload: Record<string, unknown>;
  ts: string;
}

export interface EntityRow {
  kind: string;
  id: string;
  field_count: number;
  completeness: number;
  stale_count: number;
  conflict_count: number;
  last_knowledge_time: string | null;
  quality_score: number;
  quality_status: "verified" | "draft" | "purged";
  quality_issues: string[];
  purged?: boolean;
  purge_mode?: string;
  purged_at?: string;
}

/** 删除回执（knowledge/purged）：逐项可核对，不笼统报「已清理」 */
export interface PurgeReport {
  entity_kind: string;
  entity_id: string;
  mode: "tombstone" | "hard";
  namespace: string;
  reason: string;
  purged_at: string;
  counts: Record<string, number>;
  orphan_evidence_deleted: number;
  evidence_remaining: number | null;
  files_removed: string[];
  restorable: boolean;
  warnings: string[];
  total_rows_deleted: number;
}

export interface SessionDeleteResult {
  run_id: string;
  deleted_runs: string[];
  total_events: number;
  counts: Record<string, number>;
  files_removed: string[];
  reason: string;
  forced: boolean;
}

export interface ArchiveRow {
  name: string;
  mtime: string;
  is_latest: boolean;
}

export interface Capabilities {
  main_agent: { tools: string[]; model: string };
  gateway_sources: string[];
  models: Record<string, string>;
  commands: {
    name: string; summary: string; usage: string; needs_approval: boolean;
    steps: {
      step: string; title?: string; model_role?: string | null;
      tools?: string[]; plugins?: string[]; hooks?: string[];
      budget?: Record<string, number>;
    }[];
  }[];
}

export interface SeriesPoint {
  fact_id: string;
  event_time: string | null;
  knowledge_time: string;
  value: unknown;
  version: number;
  conflict: boolean;
}

export interface CompareItem {
  id: string;
  value: unknown;
  knowledge_time: string;
  conflict: boolean;
}

export interface EvidenceJson {
  evidence_id: string;
  source_id?: string;
  url?: string | null;
  verbatim_quote?: string;
  available_at?: string | null;
  pit_grade?: string;
  missing?: boolean;
}

export interface FactJson {
  fact_id: string;
  value: unknown;
  event_time: string | null;
  knowledge_time: string;
  version: number;
  conflict: boolean;
  issues?: string[];
  evidence: EvidenceJson[];
}

export interface FieldQuality {
  field: string;
  status: "ok" | "weak" | "stale" | "conflict" | "missing";
  issues: string[];
  required: boolean;
}

export interface EntityQuality {
  entity_kind: string;
  entity_id: string;
  as_of: string;
  fields: FieldQuality[];
  quality_score: number;
  status: "verified" | "draft";
  issues: string[];
}

export interface EntityProfile {
  kind: string;
  id: string;
  as_of: string;
  namespace: string;
  quality?: EntityQuality;
  facts: Record<string, FactJson>;
}

export interface DecisionCardJson {
  card_id: string;
  action: string;
  conviction: number;
  horizon: string;
  rationale: string[];
  invalidation: string[];
  kb_snapshot_id: string;
  created_at: string;
  quality_flags: string[];
  subject: { kind: string; id: string };
  position?: { sizing_pct: number; max_loss_pct: number } | null;
}

export interface EvalSummary {
  eval_run_id: string;
  config_name: string;
  verdict: string;
  leakage_events: number;
  mean_net_return: number;
  kb_delta: number;
}

async function get<T>(url: string): Promise<T> {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`${url}: ${resp.status}`);
  return resp.json() as Promise<T>;
}

/** DELETE/POST 带查询参数：失败时把服务端 detail 带出来（删除必须能解释为什么被拒） */
async function send<T>(url: string, method: "DELETE" | "POST"): Promise<T> {
  const resp = await fetch(url, { method });
  const body = await resp.json().catch(() => null);
  if (!resp.ok) {
    const detail = body && typeof body === "object" && "detail" in body
      ? String((body as { detail: unknown }).detail)
      : `${resp.status}`;
    throw new Error(detail);
  }
  return body as T;
}

async function post<T>(url: string, body?: unknown): Promise<T> {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail ?? `${url}: ${resp.status}`);
  }
  return resp.json() as Promise<T>;
}

// P5 provider 自配（类型与 lib/providers.ts 对齐）
export interface ProvidersView {
  source: "own" | "fallback";
  config_path: string;
  file: {
    providers: Record<string, { base_url: string; api_key: string; models: string[] }>;
    default_provider?: string;
    role_map?: Record<string, string>;
    role_options?: Record<string, { effort?: string; timeout?: number }>;
  } | null;
  effective: {
    providers?: { name: string; base_url: string; models: string[]; has_key: boolean }[];
    default_provider?: string;
    role_map?: Record<string, string>;
    role_options?: Record<string, { effort?: string; timeout?: number }>;
    error?: string;
  };
}

export interface ProviderProbeResult {
  ok: boolean;
  latency_ms: number;
  error: string | null;
}

export const api = {
  sessions: () => get<SessionRow[]>("/api/sessions"),
  sessionEvents: (runId: string) => get<EventRow[]>(`/api/sessions/${runId}/events`),
  /** 删除会话（级联子 run + 报告目录）；运行中需 force */
  deleteSession: (runId: string, opts?: { force?: boolean; reason?: string }) =>
    send<SessionDeleteResult>(
      `/api/sessions/${encodeURIComponent(runId)}?force=${opts?.force ? "true" : "false"}`
      + `&reason=${encodeURIComponent(opts?.reason ?? "")}`, "DELETE"),
  entities: (includePurged = false) =>
    get<EntityRow[]>(`/api/knowledge/entities?include_purged=${includePurged}`),
  purgedEntities: () =>
    get<{ entity_kind: string; entity_id: string; mode: string; reason: string;
          purged_at: string }[]>("/api/knowledge/purged"),
  /** 删除知识实体：tombstone（默认，可恢复）/ hard（真删行+孤儿证据+存档，不可恢复） */
  deleteEntity: (kind: string, id: string, opts?: { mode?: "tombstone" | "hard";
                                                     reason?: string }) =>
    send<PurgeReport>(
      `/api/knowledge/${kind}/${encodeURIComponent(id)}?mode=${opts?.mode ?? "tombstone"}`
      + `&reason=${encodeURIComponent(opts?.reason ?? "")}`, "DELETE"),
  restoreEntity: (kind: string, id: string, reason = "") =>
    send<{ restored: boolean; entity: string }>(
      `/api/knowledge/${kind}/${encodeURIComponent(id)}/restore?reason=${encodeURIComponent(reason)}`,
      "POST"),
  archives: (kind: string, id: string) =>
    get<ArchiveRow[]>(`/api/knowledge/${kind}/${id}/archives`),
  archiveUrl: (kind: string, id: string, name: string) =>
    `/api/knowledge/${kind}/${id}/archives/${name}`,
  capabilities: () => get<Capabilities>("/api/capabilities"),
  series: (kind: string, id: string, fields: string[]) =>
    get<{ fields: Record<string, SeriesPoint[]> }>(
      `/api/knowledge/${kind}/${id}/series?fields=${fields.join(",")}`,
    ),
  compare: (field: string, kind = "stock") =>
    get<{ field: string; items: CompareItem[] }>(
      `/api/knowledge/compare?field=${encodeURIComponent(field)}&kind=${kind}`,
    ),
  reportText: async (ref: string) => {
    const resp = await fetch(`/api/reports/${ref}`);
    if (!resp.ok) throw new Error(`reportText: ${resp.status}`);
    return resp.text();
  },
  entityProfile: (kind: string, id: string, asOf?: string) =>
    get<EntityProfile>(
      `/api/knowledge/${kind}/${id}` + (asOf ? `?as_of=${encodeURIComponent(asOf)}` : ""),
    ),
  resolveConflict: async (
    kind: string, id: string, field: string, keepFactId: string, note?: string,
  ) => {
    const resp = await fetch(`/api/knowledge/${kind}/${id}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ field, keep_fact_id: keepFactId, note }),
    });
    if (!resp.ok) {
      const detail = await resp.json().catch(() => ({}));
      throw new Error(detail.detail ?? `resolveConflict: ${resp.status}`);
    }
    return (await resp.json()) as { resolved: string; cleared: number; new_fact_id: string | null };
  },
  decisions: () => get<DecisionCardJson[]>("/api/decisions"),
  evaluations: () => get<EvalSummary[]>("/api/evaluations"),
  pendingApprovals: () => get<ApprovalRow[]>("/api/approvals/pending"),
  decideApproval: async (approvalId: string, approved: boolean, comment?: string) => {
    const resp = await fetch(`/api/approvals/${approvalId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approved, comment: comment || undefined }),
    });
    if (!resp.ok) throw new Error(`decideApproval: ${resp.status}`);
  },
  commands: () => get<CommandSpec[]>("/api/commands"),
  sessionChildren: (runId: string) => get<ChildRun[]>(`/api/sessions/${runId}/children`),
  stopSession: async (runId: string) => {
    const resp = await fetch(`/api/sessions/${runId}/stop`, { method: "POST" });
    if (!resp.ok) throw new Error(`stopSession: ${resp.status}`);
    return (await resp.json()) as { stopped: string | null };
  },
  providers: () => get<ProvidersView>("/api/providers"),
  saveProviders: (payload: Record<string, unknown>) =>
    post<ProvidersView>("/api/providers", payload),
  resetProviders: () => post<ProvidersView>("/api/providers/reset"),
  testProvider: (body: {
    name?: string; base_url: string; api_key?: string; model: string;
  }) => post<ProviderProbeResult>("/api/providers/test", body),
  chat: async (sessionId: string | null, message: string) => {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, message }),
    });
    if (!resp.ok) {
      const detail = await resp.json().catch(() => ({}));
      throw new Error(detail.detail ?? `chat: ${resp.status}`);
    }
    return (await resp.json()) as { run_id: string };
  },
};

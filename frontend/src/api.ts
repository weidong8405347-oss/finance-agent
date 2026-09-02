// API client：所有数据都是后端投影（字段可回指 event/fact id）
export interface SessionRow {
  run_id: string;
  title: string | null;
  started_at: string;
  last_active: string;
  status: "idle" | "running" | "done" | "error" | "cancelled";
  status_detail: string | null;
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
  evidence: EvidenceJson[];
}

export interface EntityProfile {
  kind: string;
  id: string;
  as_of: string;
  namespace: string;
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

export const api = {
  sessions: () => get<SessionRow[]>("/api/sessions"),
  sessionEvents: (runId: string) => get<EventRow[]>(`/api/sessions/${runId}/events`),
  entities: () => get<EntityRow[]>("/api/knowledge/entities"),
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

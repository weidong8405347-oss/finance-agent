// API client：所有数据都是后端投影（字段可回指 event/fact id）
export interface SessionRow {
  run_id: string;
  event_count: number;
  started_at: string;
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
  entityProfile: (kind: string, id: string, asOf?: string) =>
    get<EntityProfile>(
      `/api/knowledge/${kind}/${id}` + (asOf ? `?as_of=${encodeURIComponent(asOf)}` : ""),
    ),
  decisions: () => get<DecisionCardJson[]>("/api/decisions"),
  evaluations: () => get<EvalSummary[]>("/api/evaluations"),
};

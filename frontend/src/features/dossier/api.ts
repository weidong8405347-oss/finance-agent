// /api/v2 档案客户端（设计 §10.1）。统一请求 key {snapshot, module, params} 由
// 调用方（页面 hook）做缓存与竞态控制；这里只负责契约与错误语义。

import type {
  ChangesJson,
  DossierSnapshot,
  EntityRowV2,
  EvidenceDetail,
  ModulePayload,
  ResearchArtifactJson,
} from "./types";

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown, url: string) {
    super(typeof detail === "string" ? detail : `${url}: ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

async function getJson<T>(url: string): Promise<T> {
  const resp = await fetch(url);
  if (!resp.ok) {
    const detail = await resp.json().catch(() => null);
    throw new ApiError(resp.status, detail?.detail ?? detail, url);
  }
  return (await resp.json()) as T;
}

async function postJson<T>(url: string, body?: unknown): Promise<T> {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => null);
    throw new ApiError(resp.status, detail?.detail ?? detail, url);
  }
  return (await resp.json()) as T;
}

const qs = (params: Record<string, string | undefined | null>): string => {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") sp.set(k, v);
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
};

export interface OpenDossierParams {
  as_of?: string | null;
  namespace?: string;
  mode?: "live" | "historical" | "rebuilt";
}

export const dossierApi = {
  entities: (params?: { namespace?: string; as_of?: string }) =>
    getJson<EntityRowV2[]>(`/api/v2/knowledge/entities${qs(params ?? {})}`),

  openDossier: (kind: string, id: string, params?: OpenDossierParams) =>
    getJson<DossierSnapshot>(
      `/api/v2/knowledge/${kind}/${encodeURIComponent(id)}/dossier${qs({
        as_of: params?.as_of,
        namespace: params?.namespace,
        mode: params?.mode,
      })}`,
    ),

  snapshot: (snapshotId: string) =>
    getJson<DossierSnapshot>(`/api/v2/dossiers/${encodeURIComponent(snapshotId)}`),

  module: (snapshotId: string, module: string, params?: { metric?: string; frequency?: string }) =>
    getJson<ModulePayload>(
      `/api/v2/dossiers/${encodeURIComponent(snapshotId)}/modules/${module}${qs(params ?? {})}`,
    ),

  evidence: (snapshotId: string, evidenceId: string) =>
    getJson<EvidenceDetail>(
      `/api/v2/dossiers/${encodeURIComponent(snapshotId)}/evidence/${encodeURIComponent(evidenceId)}`,
    ),

  series: (snapshotId: string, params?: { metric?: string; frequency?: string }) =>
    getJson<{ snapshot_id: string; as_of: string; series: unknown[]; notes: string[] }>(
      `/api/v2/dossiers/${encodeURIComponent(snapshotId)}/series${qs(params ?? {})}`,
    ),

  compare: (params: { entities: string; metric: string; as_of?: string; namespace?: string }) =>
    getJson<{
      metric: string; as_of: string;
      items: Record<string, unknown>[]; exclusions: { entity: string; reason: string }[];
      notes: string[]; comparable: boolean;
    }>(`/api/v2/knowledge/compare${qs(params)}`),

  artifact: (artifactId: string) =>
    getJson<ResearchArtifactJson>(
      `/api/v2/research/artifacts/${encodeURIComponent(artifactId)}`,
    ),

  changes: (snapshotId: string, baselineSnapshotId: string) =>
    getJson<ChangesJson>(
      `/api/v2/dossiers/${encodeURIComponent(snapshotId)}/changes${qs({
        baseline_snapshot_id: baselineSnapshotId,
      })}`,
    ),

  requestResearch: (body: {
    entity_kind?: string;
    entity_id: string;
    objective?: string;
    depth?: string;
    focus?: string;
    base_snapshot?: string | null;
    session_run_id?: string;
    idempotency_key?: string;
  }) =>
    postJson<{
      session_run_id: string; command_id: string; depth: string;
      status: string; idempotency_key: string | null;
    }>("/api/v2/research/requests", body),

  valuationPreview: (body: {
    entity_kind?: string; entity_id: string; formula_id: string;
    inputs: Record<string, unknown>[]; assumptions: Record<string, string>;
  }) => postJson<Record<string, any>>("/api/v2/valuations/preview", body),

  saveScenario: (body: {
    base_snapshot: string; model_version?: string; assumption_hash: string;
    validated_calculation_id: string; name?: string; idempotency_key?: string;
  }) =>
    postJson<{
      artifact_id: string; name: string; base_snapshot: string;
      calculation_id: string; assumption_hash: string; note: string;
    }>("/api/v2/valuations/scenarios", body),

  scenario: (artifactId: string) =>
    getJson<ResearchArtifactJson>(
      `/api/v2/valuations/scenarios/${encodeURIComponent(artifactId)}`,
    ),

  exportSnapshot: (snapshotId: string, format: "json" | "markdown") =>
    postJson<{
      job_id: string; status: string; snapshot_id: string;
      format: string; artifact_name: string; created_at: string;
    }>(`/api/v2/dossiers/${encodeURIComponent(snapshotId)}/exports`, { format }),

  jobArtifactUrl: (jobId: string) => `/api/v2/jobs/${encodeURIComponent(jobId)}/artifact`,
};

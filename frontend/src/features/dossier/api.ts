// /api/v2 档案客户端（设计 §10.1）。统一请求 key {snapshot, module, params} 由
// 调用方（页面 hook）做缓存与竞态控制；这里只负责契约与错误语义。
//
// 离线导出（§11.4）：installOfflineData 安装冻结数据后，读方法（snapshot/
// module/evidence/artifact/openDossier）短路到内嵌数据，服务器写/算操作
// 一律 503——导出的自包含 HTML 与在线页面跑同一套组件，数据层在此分流。

import type {
  ChangesJson,
  DossierSnapshot,
  EntityRowV2,
  EvidenceDetail,
  ModulePayload,
  ResearchArtifactJson,
} from "./types";

/** 冻结导出内嵌数据（后端 export_html.collect_export_data 产出）。 */
export interface DossierExportData {
  kind: "dossier-html-export";
  format_version: number;
  exported_at: string;
  snapshot: DossierSnapshot;
  modules: Record<string, ModulePayload>;
  evidence: Record<string, EvidenceDetail>;
  artifacts: Record<string, ResearchArtifactJson>;
}

let offlineData: DossierExportData | null = null;

/** 安装/清空离线数据（导出 viewer 入口调用；测试可传 null 复位）。 */
export function installOfflineData(data: DossierExportData | null): void {
  offlineData = data;
}

/** 是否处于离线导出模式（StockDossierPage 等据此禁用服务器依赖的交互）。 */
export function isOfflineExport(): boolean {
  return offlineData !== null;
}

const offlineUnavailable = (what: string) =>
  Promise.reject(new ApiError(503, `离线导出文件：${what}需在线版`, "offline"));

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
  entities: (params?: { namespace?: string; as_of?: string }) => {
    if (offlineData) return offlineUnavailable("档案库列表");
    return getJson<EntityRowV2[]>(`/api/v2/knowledge/entities${qs(params ?? {})}`);
  },

  openDossier: (kind: string, id: string, params?: OpenDossierParams) => {
    if (offlineData) {
      const s = offlineData.snapshot;
      return s.entity.kind === kind && s.entity.id === id
        ? Promise.resolve(s) // 新版本检查拿到同一冻结快照 → 不提示「有新版本」
        : Promise.reject(new ApiError(404, `离线导出未包含 ${kind}:${id}`, "offline"));
    }
    return getJson<DossierSnapshot>(
      `/api/v2/knowledge/${kind}/${encodeURIComponent(id)}/dossier${qs({
        as_of: params?.as_of,
        namespace: params?.namespace,
        mode: params?.mode,
      })}`,
    );
  },

  snapshot: (snapshotId: string) => {
    if (offlineData) {
      return offlineData.snapshot.context.snapshot_id === snapshotId
        ? Promise.resolve(offlineData.snapshot)
        : Promise.reject(new ApiError(404, `离线导出未包含快照 ${snapshotId}`, "offline"));
    }
    return getJson<DossierSnapshot>(`/api/v2/dossiers/${encodeURIComponent(snapshotId)}`);
  },

  module: (snapshotId: string, module: string, params?: { metric?: string; frequency?: string }) => {
    if (offlineData) {
      if (params && (params.metric || params.frequency)) {
        return offlineUnavailable("参数化模块读取");
      }
      const p = offlineData.modules[module];
      return p ? Promise.resolve(p) : Promise.reject(
        new ApiError(404, `离线导出未包含模块 ${module}`, "offline"));
    }
    return getJson<ModulePayload>(
      `/api/v2/dossiers/${encodeURIComponent(snapshotId)}/modules/${module}${qs(params ?? {})}`,
    );
  },

  evidence: (snapshotId: string, evidenceId: string) => {
    if (offlineData) {
      const e = offlineData.evidence[evidenceId];
      return e ? Promise.resolve(e) : Promise.reject(
        new ApiError(404, `离线导出未包含证据 ${evidenceId}`, "offline"));
    }
    return getJson<EvidenceDetail>(
      `/api/v2/dossiers/${encodeURIComponent(snapshotId)}/evidence/${encodeURIComponent(evidenceId)}`,
    );
  },

  series: (snapshotId: string, params?: { metric?: string; frequency?: string }) => {
    if (offlineData) return offlineUnavailable("序列读取");
    return getJson<{ snapshot_id: string; as_of: string; series: unknown[]; notes: string[] }>(
      `/api/v2/dossiers/${encodeURIComponent(snapshotId)}/series${qs(params ?? {})}`,
    );
  },

  compare: (params: { entities: string; metric: string; as_of?: string; namespace?: string }) => {
    if (offlineData) return offlineUnavailable("跨实体比较");
    return getJson<{
      metric: string; as_of: string;
      items: Record<string, unknown>[]; exclusions: { entity: string; reason: string }[];
      notes: string[]; comparable: boolean;
    }>(`/api/v2/knowledge/compare${qs(params)}`);
  },

  artifact: (artifactId: string) => {
    if (offlineData) {
      const a = offlineData.artifacts[artifactId];
      return a ? Promise.resolve(a) : Promise.reject(
        new ApiError(404, `离线导出未包含研究产物 ${artifactId}`, "offline"));
    }
    return getJson<ResearchArtifactJson>(
      `/api/v2/research/artifacts/${encodeURIComponent(artifactId)}`,
    );
  },

  changes: (snapshotId: string, baselineSnapshotId: string) => {
    if (offlineData) {
      // 冻结文件没有「其他版本」可比：返回空 diff（页面不会触发变更提示）
      const ctx = offlineData.snapshot.context;
      return Promise.resolve({
        baseline: { snapshot_id: baselineSnapshotId, as_of: ctx.as_of, generated_at: ctx.generated_at },
        current: { snapshot_id: snapshotId, as_of: ctx.as_of, generated_at: ctx.generated_at },
        changed_modules: [], new_claims: [], removed_claims: [],
        new_artifacts: [], key_metric_changes: [],
      } as ChangesJson);
    }
    return getJson<ChangesJson>(
      `/api/v2/dossiers/${encodeURIComponent(snapshotId)}/changes${qs({
        baseline_snapshot_id: baselineSnapshotId,
      })}`,
    );
  },

  requestResearch: (body: {
    entity_kind?: string;
    entity_id: string;
    objective?: string;
    depth?: string;
    focus?: string;
    base_snapshot?: string | null;
    session_run_id?: string;
    idempotency_key?: string;
  }) => {
    if (offlineData) return offlineUnavailable("补研");
    return postJson<{
      session_run_id: string; command_id: string; depth: string;
      status: string; idempotency_key: string | null;
    }>("/api/v2/research/requests", body);
  },

  valuationPreview: (body: {
    entity_kind?: string; entity_id: string; formula_id: string;
    inputs: Record<string, unknown>[]; assumptions: Record<string, string>;
  }) => {
    if (offlineData) return offlineUnavailable("估值试算");
    return postJson<Record<string, any>>("/api/v2/valuations/preview", body);
  },

  saveScenario: (body: {
    base_snapshot: string; model_version?: string; assumption_hash: string;
    validated_calculation_id: string; name?: string; idempotency_key?: string;
  }) => {
    if (offlineData) return offlineUnavailable("情景保存");
    return postJson<{
      artifact_id: string; name: string; base_snapshot: string;
      calculation_id: string; assumption_hash: string; note: string;
    }>("/api/v2/valuations/scenarios", body);
  },

  scenario: (artifactId: string) => {
    if (offlineData) {
      const a = offlineData.artifacts[artifactId];
      return a ? Promise.resolve(a) : Promise.reject(
        new ApiError(404, `离线导出未包含情景 ${artifactId}`, "offline"));
    }
    return getJson<ResearchArtifactJson>(
      `/api/v2/valuations/scenarios/${encodeURIComponent(artifactId)}`,
    );
  },

  exportSnapshot: (snapshotId: string, format: "json" | "markdown" | "html") => {
    if (offlineData) return offlineUnavailable("再导出");
    return postJson<{
      job_id: string; status: string; snapshot_id: string;
      format: string; artifact_name: string; created_at: string;
    }>(`/api/v2/dossiers/${encodeURIComponent(snapshotId)}/exports`, { format });
  },

  jobArtifactUrl: (jobId: string) => `/api/v2/jobs/${encodeURIComponent(jobId)}/artifact`,
};

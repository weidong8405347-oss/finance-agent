// Dossier 契约类型（与后端 src/finance_agent/dossier/models.py 对齐，设计 §6.3）。
// 数值一律是十进制字符串（前端只在绘图边界转 number；转换失败退回表格）。

export type ModuleStatus =
  | "ready" | "partial" | "missing" | "stale" | "conflicted"
  | "not_applicable" | "unavailable_at_as_of";

export type ValueNature =
  | "reported" | "calculated" | "guidance" | "consensus" | "model_estimate";

export interface EntityRef {
  kind: "stock" | "industry";
  id: string;
  name: string;
}

export interface DossierContext {
  mode: "live" | "historical" | "rebuilt";
  namespace: string;
  as_of: string;
  snapshot_id: string;
  kb_snapshot_id: string | null;
  generated_at: string;
  projector_version: string;
  recipe_id: string;
  recipe_version: string;
}

export interface ModuleState {
  status: ModuleStatus;
  title: string;
  reasons: string[];
  gap_refs: string[];
  data_ref: string | null;
  last_knowledge_time: string | null;
}

export interface KeyMetric {
  metric_key: string;
  label: string;
  value: string | null;
  unit: string;
  currency: string | null;
  period_label: string;
  nature: string;
  observation_id: string | null;
  status: "ok" | "missing" | "stale" | "conflicted" | "not_meaningful";
  as_of_note: string;
}

export interface DossierSummary {
  thesis: string | null;
  thesis_refs: string[];
  thesis_kind: "claim" | "legacy_analysis" | "draft" | "none";
  key_changes: string[];
  drivers: string[];
  counter_evidence: string | null;
  counter_refs: string[];
  key_metrics: KeyMetric[];
  updated_at: string | null;
}

export interface ResearchCoverage {
  answered: number;
  required: number;
  verdict: string | null;
  artifact_refs: string[];
  plan_id: string | null;
  assessment_id: string | null;
}

export interface DossierSnapshot {
  schema_version: string;
  entity: EntityRef;
  context: DossierContext;
  recipe: { id: string; version: string };
  summary: DossierSummary;
  modules: Record<string, ModuleState>;
  research: ResearchCoverage;
  document_refs: string[];
  evidence_refs: string[];
  decision_refs: string[];
  limitations: string[];
  data_hash: string;
}

// ---------------- 模块 payload ----------------

export interface MetricPoint {
  period_label: string;
  period_end: string;
  period_start: string | null;
  value: string | null;
  nature: ValueNature | string;
  basis: string;
  unit: string;
  currency: string | null;
  observation_id: string;
  status: string;
  knowledge_time: string;
  conflict: boolean;
}

export interface MetricSeries {
  metric_key: string;
  label: string;
  unit: string;
  currency: string | null;
  frequency: string;
  points: MetricPoint[];
  status: ModuleStatus;
  issues: string[];
}

export interface MetricSeriesSet {
  series: MetricSeries[];
  calculations: Record<string, unknown>[];
  notes: string[];
}

export interface BusinessGraphNode {
  node_id: string;
  label: string;
  kind: string;
  note: string;
}

export interface BusinessGraph {
  nodes: BusinessGraphNode[];
  edges: { source: string; target: string; label: string; value_ref: string | null }[];
  narrative: string;
  narrative_refs: string[];
}

export interface ClaimItem {
  claim_id: string;
  statement: string;
  kind: "fact_summary" | "inference" | "hypothesis" | "analysis" | string;
  status: "draft" | "validated" | "superseded" | string;
  question_id: string | null;
  support_refs: string[];
  counter_refs: string[];
  limitations: string[];
  created_at: string;
  legacy: boolean;
}

export interface EvidenceItem {
  evidence_id: string;
  source_id: string;
  provider_id: string;
  document_id: string | null;
  url: string | null;
  verbatim_quote: string;
  available_at: string | null;
  retrieved_at: string | null;
  pit_grade: string;
  used_by: string[];
}

export interface LegacyFactItem {
  field: string;
  fact_id: string;
  value: unknown;
  event_time: string | null;
  knowledge_time: string;
  version: number;
  conflict: boolean;
  issues: string[];
  evidence_ids: string[];
  needs_normalization: boolean;
}

export interface ModulePayload {
  module: string;
  snapshot_id: string;
  status: ModuleStatus;
  reasons: string[];
  as_of: string;
  payload: Record<string, any>;
}

export interface EvidenceDetail {
  evidence_id: string;
  provider_id: string;
  document: Record<string, unknown> | null;
  url: string | null;
  verbatim_quote: string;
  available_at: string | null;
  retrieved_at: string;
  pit_grade: string;
  raw_ref: string | null;
}

export interface EntityRowV2 {
  kind: string;
  id: string;
  field_count: number;
  last_knowledge_time: string | null;
  completeness: number;
  stale_count: number;
  conflict_count: number;
  quality_score: number;
  quality_status: "verified" | "draft";
  quality_issues: string[];
  observation_count: number;
  artifact_count: number;
  latest_artifact_title: string | null;
  research_coverage: { answered: number; required: number; verdict: string | null };
  recipe_id: string | null;
  snapshot_id: string | null;
}

export interface ResearchArtifactJson {
  artifact_id: string;
  entity_kind: string;
  entity_id: string;
  title: string;
  status: "draft" | "validated" | "superseded";
  sufficiency: "sufficient" | "partial" | "blocked";
  created_at: string;
  evidence_cutoff: string | null;
  plan_id: string | null;
  claim_ids: string[];
  markdown: string;
  validation_issues: { code: string; ref: string; message: string; hard: boolean }[];
  report_document: {
    doc_id: string;
    title: string;
    blocks: Record<string, any>[];
    limitations: string[];
  };
}

export interface ChangesJson {
  baseline: { snapshot_id: string; as_of: string; generated_at: string };
  current: { snapshot_id: string; as_of: string; generated_at: string };
  changed_modules: string[];
  new_claims: string[];
  removed_claims: string[];
  new_artifacts: string[];
  key_metric_changes: {
    metric_key: string;
    old_value: string | null;
    new_value: string | null;
    old_status: string | null;
    new_status: string | null;
  }[];
}

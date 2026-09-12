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
  /** 披露原文锚点（raw.value_text，如 "85%" "100x" "80-90%"）：ratio 显示优先用原文 */
  raw_text?: string;
  unit: string;
  currency: string | null;
  period_label: string;
  nature: string;
  observation_id: string | null;
  evidence_refs: string[];  // 指标自己的来源（review #21：点击直达，不用全局首条证据背书）
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
  // audit §3.8：首屏必须回答目标，并诚实表达可信度与限制
  objective?: string;
  tiers?: Record<string, string[]>;
  biggest_disagreement?: string;
  limitations?: string[];
  question_progress?: string;
  credibility?: Record<string, string>;
  // tear-sheet 首屏（升级方案 §5/§26）：来自 ExecutiveSummary/IndustryMap 结构产物，
  // 有则分块渲染，无则不显示（不编造）
  stage?: string;
  why_now?: string[];
  value_capture?: string;
  thesis_breakers?: string[];
  bottlenecks?: string[];
}

export interface ResearchCoverage {
  answered: number;
  required: number;
  verdict: string | null;
  artifact_refs: string[];
  plan_id: string | null;
  assessment_id: string | null;
}

/** What Changed 日志条目（§32：快照级 diff，发布时服务端冻结）。 */
export interface ChangeLogEntry {
  icon: "up" | "risk" | "new";
  kind: "thesis" | "risk" | "metric" | "company_status" | "catalyst" | "module" | string;
  tag: string;
  text: string;
  refs: string[];
}

/** Thesis 对象（§13/§14：服务端从 claims 确定性推导；无则前端回退 ClaimItem 渲染）。 */
export interface ThesisObject {
  id: string;
  title: string;
  summary: string;
  kind: string;
  status: string;
  importance: number | null;   // 计划问题优先级映射；无归属 → null
  confidence: number | null;   // claim 状态映射（非统计置信度；读 confidence_basis）
  confidence_basis: string;
  direction: string | null;    // 无确定性来源 → null（不从文本猜多空）
  support_count: number;
  counter_count: number;
  unresolved_count: number;
  supports: string[];
  contradicts: string[];
  monitor: string[];
  related_companies: string[];
  bear_case_status: "met" | "unmet";  // §14：unmet = 反证义务未履行
}

/** 护城河十维评估（§28：评分无证据时留空——不编造 1-5）。 */
export interface MoatAssessment {
  entity_id: string;
  name: string;
  tier: string;
  dimensions: Record<string, {
    score: number | null;
    confidence: "low" | "medium" | "high" | null;
    trend: "strengthening" | "stable" | "weakening" | "unknown" | null;
    evidence_refs: string[];
    note: string;
  }>;
  evidence_groups: Record<string, string[]>;
  claim_refs: string[];
  notes: string[];
}

/** 候选四象限（§20：离散阶段的规范序坐标，不是评分；未定位公司单列）。 */
export interface QuadrantPoint {
  entity_id: string;
  name: string;
  tier: string;
  x: number;  // technology_stage 规范序 0-4
  y: number;  // commercial_stage 规范序 0-4
  x_stage: string;
  y_stage: string;
  evidence_count: number;
}

export interface QuadrantPayload {
  x_axis: { key: string; title: string; order: string[]; labels: Record<string, string> };
  y_axis: { key: string; title: string; order: string[]; labels: Record<string, string> };
  points: QuadrantPoint[];
  unpositioned: { entity_id: string; name: string; tier: string; evidence_count: number; reason: string }[];
}

/** Profit Pool（§22/§48.4：value_flow 边的定量份额；单一来源标 estimated）。 */
export interface ProfitPool {
  kind: "stacked_bar";
  unit: "percent";
  entries: {
    node: string; label: string; layer: string;
    share: string;  // 百分数十进制字符串（"45" = 45%）
    estimated: boolean;
    evidence_refs: string[];
    note: string;
  }[];
  total_share: string;
  notes: string[];
}

/** 一致预期修订视图（§26：同一目标期间的多时点快照，x=可知时刻）。 */
export interface RevisionSeries {
  metric_key: string;
  period_label: string;
  unit: string;
  currency: string | null;
  points: { at: string; value: string; observation_id: string; evidence_refs: string[] }[];
}

/** 价格序列点（§26 Price vs Revision 的 Price 腿，x=交易日）。 */
export interface PricePoint {
  at: string;
  value: string;
  currency: string | null;
  observation_id: string;
  evidence_refs: string[];
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
  /** 结构化产物（audit §3.7）：来自冻结产物，不在请求时临时生成 */
  structures?: Record<string, any>;
  /** 模块注册表（audit §3.6）：导航顺序与 renderer 由此决定，前端不硬编码 */
  module_registry?: ModuleRegistry;
  /** What Changed 日志（§32：发布时冻结的快照级 diff；空 → 回退 summary.key_changes） */
  change_log?: ChangeLogEntry[];
  /** Investment Objects（§12-§15：投影层确定性推导的论点/护城河对象） */
  investment_objects?: {
    theses?: ThesisObject[];
    moat_assessments?: MoatAssessment[];
  };
}

// ---------------- 模块 payload ----------------

export interface MetricPoint {
  period_label: string;
  period_end: string;
  period_start: string | null;
  value: string | null;
  /** 披露原文锚点（raw.value_text）：tooltip/数据表逐字展示，不从十进制反猜格式 */
  raw_text?: string;
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
  dimensions: Record<string, string>;  // 完整语义键拆分（review #15）
  basis: string;
  nature: string;
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
  layer?: string;
  company_refs?: string[];
  bottleneck?: boolean;
  evidence_refs?: string[];
}

export interface BusinessGraphEdge {
  source: string;
  target: string;
  label: string;
  value_ref: string | null;
  relation?: string;
  flow_known?: boolean;
  evidence_refs?: string[];
}

export interface BusinessGraph {
  nodes: BusinessGraphNode[];
  edges: BusinessGraphEdge[];
  narrative: string;
  narrative_refs: string[];
  layers?: string[];
  /** 规范 layer key → 展示名（归一层从显示名捕获，列头优先用） */
  layer_labels?: Record<string, string>;
  routes?: Record<string, string>[];
  /** 价值流/利润池说明（带引用） */
  value_flow_note?: string;
  bottlenecks?: string[];
}

// ---------------- 结构化产物（audit §3.7） ----------------

export interface IndustryMapNode {
  node_id: string;
  label: string;
  layer: string;
  company_refs: string[];
  bottleneck: boolean;
  note: string;
  evidence_refs: string[];
}

export interface IndustryMapEdge {
  source: string;
  target: string;
  relation: string;
  flow_known: boolean;
  flow_value: string | null;
  note: string;
  evidence_refs: string[];
}

export interface CandidateItem {
  entity_id: string;
  name: string;
  listing_status: "listed" | "private" | "subsidiary" | "unknown" | string;
  market: string;
  security_relation: string;
  tier: "included" | "watchlist" | "excluded" | "needs_review" | string;
  technology_stage: string;
  commercial_stage: string;
  moat_evidence: string[];
  commercial_evidence: string[];
  sustainability_evidence: string[];
  counter_evidence: string[];
  reason: string;
  next_validation: string;
  evidence_refs: string[];
  investable: boolean | null;
}

export interface ValidationItem {
  event: string;
  window_start: string;
  window_end: string;
  status: "occurred" | "expected" | "unknown" | string;
  trigger_condition: string;
  affected_judgment: string;
  company_refs: string[];
  evidence_refs: string[];
}

export interface ModuleRegistryEntry {
  module_id: string;
  title: string;
  renderer: string;
  applicability: "always" | "stock_only" | "industry_only" | "data_dependent" | string;
  default_nav: boolean;
  metric_keys: string[];
  legacy_fields: string[];
  structures: string[];
  payload_keys: string[];
  notes: string;
}

export interface ModuleRegistry {
  registry_version: string;
  entity_kind: string;
  modules: ModuleRegistryEntry[];
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

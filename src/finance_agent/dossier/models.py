"""Stock Dossier 契约（设计 §6.3/§4.6/§6.6）。

DossierSnapshot 是可重建的页面读模型：
- 所有模块共用冻结的 DossierContext（mode/namespace/as_of/snapshot_id）——
  表格、图表、来源、质量和研究结论必须来自同一可见世界；
- 首屏只带 summary 与轻量模块目录，明细按模块加载（模块 payload 类型固定，
  不返回任意 ECharts option）；
- data_hash 内容寻址：同输入重投影得到同一快照（幂等发布）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0"
#: 投影器版本（进 data_hash：投影逻辑变更 → 新快照身份，旧快照仍可回溯）
# v2：首屏回退填充去重（同值同单位双键登记不重复展示）+ tear-sheet/layer_labels/
#     raw_text/comparison_numerics 投影
# v3：Investment Objects 投影（§12-§15/§34：ThesisObject + MoatAssessment 从冻结
#     claims/时间线/候选确定性推导）+ 快照 change_log 字段（发布时冻结）
# v4：ThesisObject 标题不再用 targeted 问题原文（操作指令不是论点标题）；
#     structures 内容摘进 data_hash（原地结构更新产生新快照）
PROJECTOR_VERSION = "4"

DossierMode = Literal["live", "historical", "rebuilt"]
ModuleStatus = Literal[
    "ready", "partial", "missing", "stale", "conflicted",
    "not_applicable", "unavailable_at_as_of",
]

#: 首发十模块（§4.4）；第一个可上线切片 = 1/2/4/5/9/10 基础版 + 6/7/8 降级
MODULE_IDS: tuple[str, ...] = (
    "investment_snapshot", "business_engine", "revenue_segments", "key_kpi",
    "financial_quality", "expectations", "valuation_lab", "peers",
    "catalysts_risks", "research_sources",
)

MODULE_TITLES: dict[str, str] = {
    "investment_snapshot": "研究结论",
    "business_engine": "商业引擎",
    "revenue_segments": "收入与分部",
    "key_kpi": "关键 KPI",
    "financial_quality": "财务质量",
    "expectations": "预期差",
    "valuation_lab": "估值实验",
    "peers": "同业与竞争",
    "catalysts_risks": "催化与风险",
    "research_sources": "研究与来源",
}


class EntityRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["stock", "industry"]
    id: str
    name: str = ""


class DossierContext(BaseModel):
    """统一快照上下文：首次打开 live 将「现在」固定为一个服务端时刻（§6.6）。"""

    model_config = ConfigDict(extra="forbid")

    mode: DossierMode = "live"
    namespace: str = "prod"
    as_of: datetime
    snapshot_id: str = ""
    kb_snapshot_id: str | None = None  # 旧 DecisionCard 绑定哈希（继续可用，不替换算法）
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    projector_version: str = PROJECTOR_VERSION
    recipe_id: str = "general"
    recipe_version: str = "1"


class ModuleState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ModuleStatus
    title: str = ""
    reasons: list[str] = Field(default_factory=list)  # 降级/缺失原因（不伪装 ready）
    gap_refs: list[str] = Field(default_factory=list)
    data_ref: str | None = None
    last_knowledge_time: datetime | None = None


class KeyMetric(BaseModel):
    """首屏关键指标：只消费服务端 typed 观测/计算，不从文本猜数（§2.2）。"""

    model_config = ConfigDict(extra="forbid")

    metric_key: str
    label: str
    value: str | None = None  # 十进制字符串；None = 缺口（不用零或旧估计补位）
    #: 披露原文锚点（raw.value_text，如 "85%" "100x" "80-90%"）：展示优先用原文，
    #: 避免 unit=ratio 的百分数/倍数被前端按分数 ×100 误显示（85 → 8500%）
    raw_text: str = ""
    unit: str = ""
    currency: str | None = None
    period_label: str = ""
    nature: str = ""  # reported/calculated/guidance/consensus/model_estimate
    observation_id: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)  # 点击指标直达自己的来源（review #21）
    status: Literal["ok", "missing", "stale", "conflicted", "not_meaningful"] = "missing"
    as_of_note: str = ""


class DossierSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thesis: str | None = None  # 研究结论一句话（来自 validated claim / 最新 artifact）
    thesis_refs: list[str] = Field(default_factory=list)
    thesis_kind: Literal["claim", "legacy_analysis", "draft", "none"] = "none"
    key_changes: list[str] = Field(default_factory=list)  # 最近变化 ①②③
    drivers: list[str] = Field(default_factory=list)  # 关键驱动链
    counter_evidence: str | None = None  # 最大反证
    counter_refs: list[str] = Field(default_factory=list)
    key_metrics: list[KeyMetric] = Field(default_factory=list)
    updated_at: datetime | None = None
    # ---- audit §3.8：首屏必须回答目标，并诚实表达可信度与限制 ----
    #: 用户研究目标原文（首屏结论必须对它作答，不得静默换成背景叙述）
    objective: str = ""
    #: 候选分层（tier → 公司）：来自 CandidateAssessment/ExecutiveSummary 结构产物
    tiers: dict[str, list[str]] = Field(default_factory=dict)
    biggest_disagreement: str = ""
    #: 限制与未核验部分（不被硬截断，全文保留）
    limitations: list[str] = Field(default_factory=list)
    #: 问题进展：无论是否已有 assessment 都显示（0/9 不得隐藏）
    question_progress: str = ""
    #: 可信度分层：引用可解析 / 事实已核对 / 分析已复核 / 研究充分度
    credibility: dict[str, str] = Field(default_factory=dict)
    # ---- tear-sheet 首屏（升级方案 §4/§5/§26）：来自 ExecutiveSummary 结构产物，
    # 有则按投资语义分块渲染，无则不显示（不编造） ----
    #: 行业/公司所处阶段（离散描述，不是评分）
    stage: str = ""
    #: 为什么现在值得关注
    why_now: list[str] = Field(default_factory=list)
    #: 价值捕获在哪里
    value_capture: str = ""
    #: 证伪条件（kill criteria）：什么事情发生会推翻本结论
    thesis_breakers: list[str] = Field(default_factory=list)
    #: 关键瓶颈环节（来自 industry_map.bottlenecks）
    bottlenecks: list[str] = Field(default_factory=list)


class SnapshotInputs(BaseModel):
    """快照冻结的输入版本集（review #2）：模块/来源/序列请求按这些 id 读取，
    不再重查当前库——补录历史观测不会改变已冻结快照的任何模块内容。

    旧快照（无 inputs）回退 as_of 查询（契约升级前的兼容路径，投影层标注）。"""

    model_config = ConfigDict(extra="forbid")

    fact_ids: dict[str, str] = Field(default_factory=dict)  # field → fact_id（选中版本）
    observation_ids: list[str] = Field(default_factory=list)
    claims: list[dict[str, Any]] = Field(default_factory=list)  # 冻结 payload（状态不漂移）
    artifacts: list[dict[str, Any]] = Field(default_factory=list)  # 冻结摘要（id/status/…）
    resolution_ids: list[str] = Field(default_factory=list)
    calculation_ids: list[str] = Field(default_factory=list)
    conflicted_semantic_hashes: list[str] = Field(default_factory=list)  # as_of 时点未裁决
    plan: dict[str, Any] | None = None  # 冻结计划 payload（问题状态以发布时刻为准）
    plan_status_reliable: bool = True  # 历史投影下计划后续被更新过 → False


class ResearchCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answered: int = 0
    required: int = 0
    verdict: str | None = None  # sufficient/partial/blocked（最新 assessment）
    artifact_refs: list[str] = Field(default_factory=list)
    plan_id: str | None = None
    assessment_id: str | None = None


class DossierSnapshot(BaseModel):
    """档案快照（首屏响应）：summary + 模块目录；明细按模块懒加载。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    entity: EntityRef
    context: DossierContext
    recipe: dict[str, str] = Field(default_factory=dict)  # {id, version}
    summary: DossierSummary = Field(default_factory=DossierSummary)
    modules: dict[str, ModuleState] = Field(default_factory=dict)
    research: ResearchCoverage = Field(default_factory=ResearchCoverage)
    document_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    decision_refs: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    data_hash: str = ""
    inputs: SnapshotInputs = Field(default_factory=SnapshotInputs)
    #: 结构化产物投影（audit §3.7）：industry_map/candidate_assessment/
    #: comparison_matrix/validation_timeline/executive_summary——来自冻结产物，
    #: 不在请求时调 LLM 临时生成
    structures: dict[str, Any] = Field(default_factory=dict)
    #: 模块注册表投影（audit §3.6）：前端据此渲染导航与组件，不再硬编码 SECTION_ORDER
    module_registry: dict[str, Any] = Field(default_factory=dict)
    #: What Changed 日志（升级方案 §32）：发布时由 service 相对上一 live 快照计算并冻结
    #: （dossier/changes.py；不进 data_hash——快照身份由输入版本集决定，变化日志是
    #: 发布时刻的派生视图）；空列表 → 前端回退到 summary.key_changes
    change_log: list[dict[str, Any]] = Field(default_factory=list)
    #: Investment Objects（升级方案 §12-§15/§34）：投影层从 claims+时间线+候选推导的
    #: 确定性 ThesisObject / MoatAssessment（不从文本猜置信度；无数据 = 空）
    investment_objects: dict[str, Any] = Field(default_factory=dict)

    def data_hash_of(self) -> str:
        """内容寻址：投影输入的版本集（fact/observation/claim/artifact/resolution id+版本）。"""
        return self.data_hash

    def compute_data_hash(self, inputs: dict[str, Any]) -> str:
        """快照身份哈希（§6.6 可缓存键）：namespace/模式/输入版本集/配方/投影器版本。

        live 模式的 as_of 不进哈希：「现在」只是服务端固定时刻，快照身份由
        可见输入版本集决定——数据不变则重复打开复用同一冻结快照（幂等发布）；
        historical/rebuilt 的 as_of 进哈希（同一实体不同截止时点是不同快照）。"""
        ctx_part: dict[str, Any] = {
            "mode": self.context.mode,
            "namespace": self.context.namespace,
            "projector_version": self.context.projector_version,
            "recipe": self.recipe,
        }
        if self.context.mode != "live":
            ctx_part["as_of"] = self.context.as_of.isoformat()
        canon = json.dumps(
            {
                "entity": f"{self.entity.kind}:{self.entity.id}",
                "context": ctx_part,
                "inputs": inputs,
            },
            ensure_ascii=False, sort_keys=True, default=str,
        )
        return "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:32]


# ---------------- 模块 payload 类型（固定 registry；模型不能注入执行逻辑） ----------------


class MetricPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period_label: str
    period_end: str
    period_start: str | None = None
    value: str | None  # 十进制字符串（前端只在绘图边界转 number）
    #: 披露原文锚点（raw.value_text）：tooltip/数据表优先展示原文，不从十进制反猜显示格式
    raw_text: str = ""
    nature: str
    basis: str = "GAAP"
    unit: str = ""
    currency: str | None = None
    observation_id: str
    status: str = "ok"
    knowledge_time: str = ""
    conflict: bool = False


class MetricSeries(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric_key: str
    label: str
    unit: str = ""
    currency: str | None = None
    frequency: str = "FY"
    #: 完整语义键拆分（review #15）：分部/口径/币种/性质不同的观测各自成序列，
    #: 不混合投影（分部收入/指引不得被画成公司实际收入）
    dimensions: dict[str, str] = Field(default_factory=dict)
    basis: str = "GAAP"
    nature: str = "reported"
    points: list[MetricPoint] = Field(default_factory=list)
    status: ModuleStatus = "missing"
    issues: list[str] = Field(default_factory=list)


class MetricSeriesSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    series: list[MetricSeries] = Field(default_factory=list)
    calculations: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class BusinessGraphNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    label: str
    kind: Literal["customer", "product", "revenue", "cost", "cashflow", "input", "other"] = "other"
    note: str = ""
    #: 产业链分层（audit §3.7）：upstream/midstream/downstream/platform/application
    layer: str = ""
    #: 关联公司（页面点击联动到候选行）
    company_refs: list[str] = Field(default_factory=list)
    bottleneck: bool = False
    evidence_refs: list[str] = Field(default_factory=list)


class BusinessGraphEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    target: str
    label: str = ""
    #: 流量宽度只能来自带来源的数值；无数据用流程图（不能编造 Sankey 宽度，§4.4）
    value_ref: str | None = None  # observation_id
    #: 关系类型与流量可知性（audit §3.7：flow_known=False → 前端画等宽边）
    relation: str = "supplies"
    flow_known: bool = False
    evidence_refs: list[str] = Field(default_factory=list)


class BusinessGraph(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[BusinessGraphNode] = Field(default_factory=list)
    edges: list[BusinessGraphEdge] = Field(default_factory=list)
    narrative: str = ""  # 兼容投影的 legacy 文本（business_model 字段）
    narrative_refs: list[str] = Field(default_factory=list)
    #: 分层顺序与技术路线（行业图布局用）
    layers: list[str] = Field(default_factory=list)
    #: 规范 layer key → 展示名（归一层从显示名捕获，前端列头优先用）
    layer_labels: dict[str, str] = Field(default_factory=dict)
    routes: list[dict[str, str]] = Field(default_factory=list)
    #: 价值流/利润池说明（带引用）
    value_flow_note: str = ""
    bottlenecks: list[str] = Field(default_factory=list)


class ClaimItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str
    statement: str
    kind: str
    status: str
    question_id: str | None = None
    support_refs: list[str] = Field(default_factory=list)
    counter_refs: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    created_at: str = ""
    legacy: bool = False  # 旧 thesis 兼容投影（legacy analysis，不是披露事实）


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    source_id: str = ""
    provider_id: str = ""
    document_id: str | None = None
    url: str | None = None
    verbatim_quote: str = ""
    available_at: str | None = None
    retrieved_at: str | None = None
    pit_grade: str = ""
    used_by: list[str] = Field(default_factory=list)  # 引用它的字段/观测/论断


class LegacyFactItem(BaseModel):
    """旧字段视图（「数据与审计」区）：保留契约，不与新模块混排。"""

    model_config = ConfigDict(extra="forbid")

    field: str
    fact_id: str
    value: Any = None
    event_time: str | None = None
    knowledge_time: str = ""
    version: int = 1
    conflict: bool = False
    issues: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    needs_normalization: bool = False  # 数值语义不明（单位/期间无法确定）的旧值


class ModulePayload(BaseModel):
    """模块明细的统一信封：payload 类型由 module 决定（固定 registry）。"""

    model_config = ConfigDict(extra="allow")

    module: str
    snapshot_id: str
    status: ModuleStatus
    reasons: list[str] = Field(default_factory=list)
    as_of: str
    payload: dict[str, Any] = Field(default_factory=dict)

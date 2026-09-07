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
PROJECTOR_VERSION = "1"

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
    unit: str = ""
    currency: str | None = None
    period_label: str = ""
    nature: str = ""  # reported/calculated/guidance/consensus/model_estimate
    observation_id: str | None = None
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

    def data_hash_of(self) -> str:
        """内容寻址：投影输入的版本集（fact/observation/claim/artifact/resolution id+版本）。"""
        return self.data_hash

    def compute_data_hash(self, inputs: dict[str, Any]) -> str:
        canon = json.dumps(
            {
                "entity": f"{self.entity.kind}:{self.entity.id}",
                "context": {
                    "mode": self.context.mode,
                    "namespace": self.context.namespace,
                    "as_of": self.context.as_of.isoformat(),
                    "projector_version": self.context.projector_version,
                    "recipe": self.recipe,
                },
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


class BusinessGraphEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    target: str
    label: str = ""
    #: 流量宽度只能来自带来源的数值；无数据用流程图（不能编造 Sankey 宽度，§4.4）
    value_ref: str | None = None  # observation_id


class BusinessGraph(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[BusinessGraphNode] = Field(default_factory=list)
    edges: list[BusinessGraphEdge] = Field(default_factory=list)
    narrative: str = ""  # 兼容投影的 legacy 文本（business_model 字段）
    narrative_refs: list[str] = Field(default_factory=list)


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

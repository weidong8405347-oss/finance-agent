"""Typed 指标观测契约：SourceDocument / MetricObservation（设计 §6.1–§6.2）。

与旧 Fact 的分离与兼容：
- Fact = 粗粒度字段档案（knowledge/models.py，保留兼容）；
- MetricObservation = 带期间/维度/口径/性质的结构化数值观测，按语义键版本化；
- 所有正式数值必须能追溯到原始摘录（reported/guidance）、已登记公式（calculated）、
  供应商快照（consensus）或冻结产物（model_estimate）——按 nature 判别联合，
  schema 层强制各类别的来源义务（不靠 optional 字段放行无来源数值）。

数值纪律：value 是标准化十进制**字符串**（缺失 = None，不是 0）；
原文保留在 raw（value_text/unit_text/quote_ref），换算经 normalization 显式登记。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import PitGrade

ValueNature = Literal["reported", "calculated", "guidance", "consensus", "model_estimate"]
Frequency = Literal["FY", "Q", "H1", "TTM", "instant"]
Basis = Literal["GAAP", "IFRS", "non_GAAP", "operating_metric"]
ObservationStatus = Literal["ok", "missing", "conflicted", "not_meaningful"]

#: 可加总的流量指标（TTM 只允许对它们做四季度求和；余额类用时点值，设计 §6.4.2）
ADDITIVE_FLOW_METRICS = frozenset(
    {"revenue", "net_income", "cfo", "capex", "fcf", "ebitda", "gross_profit", "operating_income"}
)


class MetricPeriod(BaseModel):
    """业务期间（不是版本时间）：报表所属起止 + 频率 + 财年标签（52/53 周保留原样）。"""

    model_config = ConfigDict(extra="forbid")

    start: date | None = None
    end: date
    frequency: Frequency
    fiscal_label: str = ""

    @model_validator(mode="after")
    def _check(self) -> MetricPeriod:
        if self.frequency == "instant":
            if self.start is not None:
                raise ValueError("instant 频率不允许 start（时点值）")
        else:
            if self.start is None:
                raise ValueError(f"{self.frequency} 频率必须给出期间 start")
            if self.start > self.end:
                raise ValueError("期间 start 不能晚于 end")
        return self


class RawValue(BaseModel):
    """原文锚点：逐字值文本 + 单位文本 + 摘录引用（evidence_id 或 quote 定位）。"""

    model_config = ConfigDict(extra="forbid")

    value_text: str = Field(min_length=1)
    unit_text: str = ""
    quote_ref: str = ""
    #: 多数字摘录里显式选定的 cell/span（audit §3.2）：逐字子串，服务端校验。
    #: 不给则当摘录只含一个数字时才允许登记（不许猜用户要哪个数）。
    span: str = ""


class SourceDocument(BaseModel):
    """文档目录条目（设计 §6.1）：一份文档的明确版本。

    命名三层：provider_id（采集 adapter）→ document_id（文档版本）→ evidence_id（摘录）。
    Evidence 继续保存逐字摘录；本对象保存文档级元数据与定位。
    """

    model_config = ConfigDict(extra="forbid")

    document_id: str
    provider_id: str
    publisher: str = ""
    document_type: str = ""  # 10-K / 10-Q / annual_report / transcript / presentation ...
    title: str = ""
    url: str | None = None
    raw_hash: str | None = None
    language: str = ""
    accounting_standard: str = ""
    period_label: str = ""
    published_at: datetime | None = None
    retrieved_at: datetime
    locator: dict[str, str] = Field(default_factory=dict)  # page/section/table 定位
    access_status: Literal["ok", "paywalled", "unavailable", "partial"] = "ok"
    parser_version: str = ""


class _ObservationBase(BaseModel):
    """各类 nature 的公共字段。子类按 nature 判别并强制来源义务。"""

    model_config = ConfigDict(extra="forbid")

    observation_id: str = ""
    nature: ValueNature  # 判别字段：子类收窄为 Literal 并给默认值
    entity_kind: Literal["stock", "industry"]
    entity_id: str
    #: 指标主体（audit §3.2）：与研究范围（entity_*）分离。公司财务写公司实体，
    #: 行业总量写行业实体；None = 主体即研究实体（向后兼容）。
    subject_entity_kind: Literal["stock", "industry"] | None = None
    subject_entity_id: str | None = None
    metric_key: str = Field(min_length=1)  # revenue / capex / arr / contracted_mw ...
    period: MetricPeriod
    dimensions: dict[str, str] = Field(default_factory=dict)  # segment/geography/product...
    basis: Basis = "GAAP"
    value: str | None = None  # 标准化十进制字符串；缺失 = None（绝不是 0）
    unit: str = ""
    currency: str | None = None
    raw: RawValue | None = None
    #: 文档内定位（document/page/table/row/column/cell/span）：财务证据绑定用
    #: （audit §3.2：只有裸数字的摘录不能直接变成可靠金额）
    locator: dict[str, str] = Field(default_factory=dict)
    normalization: list[dict[str, Any]] = Field(default_factory=list)  # NormalizationStep 序列化
    evidence_refs: list[str] = Field(default_factory=list)
    calculation_ref: str | None = None
    document_refs: list[str] = Field(default_factory=list)
    knowledge_time: datetime  # 何时可知（延续事实层时态纪律）
    source_available_at: datetime | None = None  # 资料公开时刻
    retrieved_at: datetime
    created_at: datetime  # 系统记录时间，不冒充公开时间
    artifact_ref: str | None = None
    pit_grade: PitGrade = PitGrade.C
    status: ObservationStatus = "ok"
    run_id: str | None = None
    note: str = ""

    @model_validator(mode="after")
    def _check_value(self) -> _ObservationBase:
        if self.status == "ok" and self.value is None:
            raise ValueError("status=ok 的观测必须给出 value（缺失请标 status=missing）")
        if self.value is not None:
            try:
                Decimal(self.value)
            except InvalidOperation as e:
                raise ValueError(f"value 必须是十进制字符串（收到 {self.value!r}）") from e
        if self.period.frequency == "TTM" and self.metric_key not in ADDITIVE_FLOW_METRICS:
            # TTM 只对可加总流量指标开放（余额类用时点值；防止拼出错假的 TTM）
            raise ValueError(
                f"TTM 仅允许可加总流量指标（{sorted(ADDITIVE_FLOW_METRICS)}），"
                f"{self.metric_key} 请用 instant/期末值"
            )
        return self

    # ---- 语义键（设计 §6.4.1）：实体+指标+期间+维度+币种+口径+性质 ----

    def semantic_key(self) -> dict[str, Any]:
        return {
            "entity": f"{self.entity_kind}:{self.entity_id}",
            # 主体进语义键（audit §3.2）：相同期间不同公司的收入不得归为同一指标序列
            "subject": f"{self.subject_kind}:{self.subject_id}",
            "metric_key": self.metric_key,
            "period_start": self.period.start.isoformat() if self.period.start else None,
            "period_end": self.period.end.isoformat(),
            "frequency": self.period.frequency,
            "dimensions": self.dimensions,
            "currency": self.currency,
            "basis": self.basis,
            "nature": self.nature,
        }

    def semantic_hash(self) -> str:
        canon = json.dumps(self.semantic_key(), ensure_ascii=False, sort_keys=True)
        return "sem-" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]

    @property
    def subject_kind(self) -> str:
        """指标主体类别（未显式给出 = 研究实体）。"""
        return self.subject_entity_kind or self.entity_kind

    @property
    def subject_id(self) -> str:
        """指标主体 id（未显式给出 = 研究实体）。"""
        return self.subject_entity_id or self.entity_id

    @property
    def is_cross_subject(self) -> bool:
        """是否跨主体引用（多主体研究：需在授权范围内）。"""
        return (
            self.subject_entity_kind is not None
            and (self.subject_entity_kind != self.entity_kind
                 or (self.subject_entity_id or "") != self.entity_id)
        )


class ReportedObservation(_ObservationBase):
    """原文披露值：必须含原文证据（evidence_refs ≥1）与 raw 锚点。"""

    nature: Literal["reported"] = "reported"

    @model_validator(mode="after")
    def _check_sources(self) -> ReportedObservation:
        if self.status == "ok" and not self.evidence_refs:
            raise ValueError("reported 观测必须绑定 ≥1 条证据（evidence_refs）")
        if self.status == "ok" and self.raw is None:
            raise ValueError("reported 观测必须保留原文锚点（raw.value_text）")
        if self.calculation_ref is not None:
            raise ValueError("reported 是披露事实，不允许携带 calculation_ref")
        return self


class CalculatedObservation(_ObservationBase):
    """派生计算值：必须引用已登记的 CalculationRun（不假装原文披露）。"""

    nature: Literal["calculated"] = "calculated"

    @model_validator(mode="after")
    def _check_sources(self) -> CalculatedObservation:
        if self.status == "ok" and not self.calculation_ref:
            raise ValueError("calculated 观测必须引用 calculation_ref（可重算契约）")
        return self


class GuidanceObservation(_ObservationBase):
    """公司指引：披露事实中的未来值——必须含发布者、指引发布时刻与目标期间。"""

    nature: Literal["guidance"] = "guidance"
    issuer: str = Field(min_length=1)  # 发布者（公司名/管理层角色）
    guidance_published_at: datetime
    target_period: MetricPeriod  # 指引覆盖的未来期间

    @model_validator(mode="after")
    def _check_sources(self) -> GuidanceObservation:
        if self.status == "ok" and not self.evidence_refs:
            raise ValueError("guidance 是披露事实，必须绑定 ≥1 条证据")
        return self


class ConsensusObservation(_ObservationBase):
    """一致预期：数据供应商的观察快照——必须含供应商与快照时点（缺快照不可回填）。"""

    nature: Literal["consensus"] = "consensus"
    vendor: str = Field(min_length=1)
    consensus_snapshot_at: datetime


class ModelEstimateObservation(_ObservationBase):
    """模型估计：假设产物——必须含非空 assumptions 与生成它的冻结 artifact。"""

    nature: Literal["model_estimate"] = "model_estimate"
    assumptions: dict[str, Any] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_sources(self) -> ModelEstimateObservation:
        if self.status == "ok" and not self.artifact_ref:
            raise ValueError("model_estimate 必须引用生成它的冻结产物（artifact_ref）")
        return self


MetricObservation = Annotated[
    (
        ReportedObservation
        | CalculatedObservation
        | GuidanceObservation
        | ConsensusObservation
        | ModelEstimateObservation
    ),
    Field(discriminator="nature"),
]


class ObservationRecord(BaseModel):
    """落库后的观测：语义键版本链 + 冲突标记（append-only，同 Fact 纪律）。"""

    model_config = ConfigDict(extra="forbid")

    observation: MetricObservation
    observation_id: str
    namespace: str = "prod"
    semantic_hash: str
    version: int = 1
    supersedes: str | None = None
    conflict_flag: bool = False


def observation_from_dict(data: dict[str, Any]) -> MetricObservation:
    """按 nature 判别反序列化（存储/API 共用入口，非法类别组合 fail-loud）。"""
    from pydantic import TypeAdapter

    adapter: TypeAdapter[Any] = TypeAdapter(MetricObservation)
    return adapter.validate_python(data)

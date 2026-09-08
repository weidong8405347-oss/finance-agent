"""结构化产物契约（audit §3.7）：产业链图 / 候选评估 / 对照矩阵 / 验证时间线。

事故形态：`business_graph()` 只拼接旧 value_chain/competition/sub_sectors 文本，
不填 nodes/edges；前端也没有真正消费 edges——页面因此显示长文本与原始 JSON。
「无流量所以不画 Sankey」不妨碍画**带证据的产业链关系图**。

纪律：合成器负责产出结构化候选，服务端验证（引用可解析、层级/关系合法），
projector 做确定性投影；不在读页面时调 LLM 临时生成图。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

StructureKind = Literal[
    "industry_map", "candidate_assessment", "comparison_matrix",
    "validation_timeline", "executive_summary",
]

Tier = Literal["included", "watchlist", "excluded", "needs_review"]
ListingStatus = Literal["listed", "private", "subsidiary", "unknown"]
TimelineStatus = Literal["occurred", "expected", "unknown"]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IndustryMapNode(_Base):
    """产业链节点：一个环节或一家公司（分层 + 证据）。"""

    node_id: str
    label: str
    #: 层级：upstream / midstream / downstream / platform / application / infrastructure
    layer: str = "midstream"
    #: 关联公司（可点击联动到候选行）
    company_refs: list[str] = Field(default_factory=list)
    #: 是否瓶颈环节（页面高亮）
    bottleneck: bool = False
    note: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class IndustryMapEdge(_Base):
    """节点间关系：上下游/供给/替代/竞争。流量未知时用等宽边（不编造粗细）。"""

    source: str
    target: str
    relation: Literal["supplies", "competes", "substitutes", "depends_on", "enables"] = "supplies"
    #: 流量/份额是否已知：False → 前端画等宽边，不做假 Sankey
    flow_known: bool = False
    flow_value: str | None = None
    note: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class IndustryMap(_Base):
    """产业链与技术路线（可点击分层关系图）。"""

    nodes: list[IndustryMapNode] = Field(default_factory=list)
    edges: list[IndustryMapEdge] = Field(default_factory=list)
    layers: list[str] = Field(default_factory=list)
    #: 技术路线对比（路线 → 成熟度/成本/代表公司）
    routes: list[dict[str, str]] = Field(default_factory=list)
    bottlenecks: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class CandidateItem(_Base):
    """一家公司的候选评估行（audit §3.4 五个维度 + 可投资范围）。"""

    entity_id: str
    name: str = ""
    listing_status: ListingStatus = "unknown"
    market: str = ""
    #: 证券与经营主体的关系（母子公司/ADR/借壳）；不明确必须标待核实
    security_relation: str = ""
    tier: Tier = "needs_review"
    #: 技术验证阶段 / 商业兑现阶段（离散分类，有明确定义；不造精确坐标）
    technology_stage: str = ""
    commercial_stage: str = ""
    moat_evidence: list[str] = Field(default_factory=list)
    commercial_evidence: list[str] = Field(default_factory=list)
    sustainability_evidence: list[str] = Field(default_factory=list)
    counter_evidence: list[str] = Field(default_factory=list)
    #: 入选/淘汰/待核实原因（保留「超时/数据不足/研究否定」的区别）
    reason: str = ""
    #: 下一次可验证的触发条件与时点
    next_validation: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    #: 是否可交易候选（未上市公司作技术参照，不混入可交易候选）
    investable: bool | None = None


class CandidateAssessment(_Base):
    """公司证据矩阵与筛选逻辑。"""

    objective: str = ""
    criteria: list[str] = Field(default_factory=list)
    candidates: list[CandidateItem] = Field(default_factory=list)
    #: 阶段分类的定义（页面显示，避免「阶段」被当成评分）
    stage_definitions: dict[str, str] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)


class ComparisonRow(_Base):
    """对照表一行：同期间同口径数据 + 不可比原因。"""

    label: str
    cells: dict[str, str | None] = Field(default_factory=dict)  # 列 id → 值（None = 缺口）
    observation_ids: dict[str, str] = Field(default_factory=dict)  # 列 id → 观测引用
    comparable: bool = True
    incomparable_reason: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class ComparisonMatrix(_Base):
    """横向对照（满足可比条件才绘图，否则只给表）。"""

    title: str = ""
    columns: list[dict[str, str]] = Field(default_factory=list)  # [{id,label,period,unit}]
    rows: list[ComparisonRow] = Field(default_factory=list)
    period_label: str = ""
    #: 全表可比才允许出图（前端据此决定画不画）
    chartable: bool = False
    incomparable_reasons: list[str] = Field(default_factory=list)


class ValidationItem(_Base):
    """验证时间线一项：事件、时间范围、触发条件、受影响判断。"""

    event: str
    window_start: str = ""
    window_end: str = ""
    status: TimelineStatus = "unknown"
    trigger_condition: str = ""
    affected_judgment: str = ""
    company_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class ValidationTimeline(_Base):
    """催化与证伪：展示条件而不是无依据的概率。"""

    items: list[ValidationItem] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ExecutiveSummary(_Base):
    """首屏结论：必须回答用户目标（audit §3.8）。"""

    objective: str = ""
    answer: str = ""
    #: 候选分层（tier → 公司列表），与 CandidateAssessment 同源
    tiers: dict[str, list[str]] = Field(default_factory=dict)
    main_basis: list[str] = Field(default_factory=list)
    biggest_disagreement: str = ""
    limitations: list[str] = Field(default_factory=list)
    question_progress: str = ""
    refs: list[str] = Field(default_factory=list)
    #: 可信度分层（audit §3.8）：引用可解析 / 事实已核对 / 分析已复核 / 研究充分度
    credibility: dict[str, str] = Field(default_factory=dict)


STRUCTURE_MODELS: dict[str, type[BaseModel]] = {
    "industry_map": IndustryMap,
    "candidate_assessment": CandidateAssessment,
    "comparison_matrix": ComparisonMatrix,
    "validation_timeline": ValidationTimeline,
    "executive_summary": ExecutiveSummary,
}


class StructureError(ValueError):
    """结构产物校验失败（fail-closed）：消息即拒绝原因，可当轮修复。"""


def parse_structures(raw: dict[str, Any]) -> dict[str, BaseModel]:
    """合成器提交的结构候选 → 类型化对象（未知 kind 直接拒，不静默丢弃）。"""
    out: dict[str, BaseModel] = {}
    for kind, payload in (raw or {}).items():
        model = STRUCTURE_MODELS.get(kind)
        if model is None:
            raise StructureError(
                f"未知结构产物 {kind!r}（可用：{sorted(STRUCTURE_MODELS)}）"
            )
        if payload is None:
            continue
        try:
            out[kind] = model.model_validate(payload)
        except Exception as e:  # noqa: BLE001 - pydantic 错误转可读拒绝
            raise StructureError(f"{kind} 结构非法：{type(e).__name__}: {e}") from e
    return out


def validate_structures(
    structures: dict[str, BaseModel], *, resolvable=None
) -> list[str]:
    """服务端验证（确定性）：引用可解析 + 图完整性 + 可比性口径。

    resolvable: (ref_id) -> bool；缺省只查内部一致性。返回问题清单（空 = 通过）。
    """
    issues: list[str] = []

    def _refs(obj: BaseModel) -> list[str]:
        out: list[str] = []
        for name in ("evidence_refs", "moat_evidence", "commercial_evidence",
                     "sustainability_evidence", "counter_evidence"):
            value = getattr(obj, name, None)
            if isinstance(value, list):
                out += [str(v) for v in value if str(v).startswith(("ev-", "obs-", "calc-",
                                                                    "claim-", "fact-"))]
        return out

    def _check_ref(ref: str, where: str) -> None:
        if resolvable is not None and not resolvable(ref):
            issues.append(f"{where}: 引用不可解析 {ref}")

    imap = structures.get("industry_map")
    if isinstance(imap, IndustryMap):
        ids = {n.node_id for n in imap.nodes}
        if len(ids) != len(imap.nodes):
            issues.append("industry_map: node_id 重复")
        for edge in imap.edges:
            if edge.source not in ids:
                issues.append(f"industry_map: 边的 source 未定义 {edge.source}")
            if edge.target not in ids:
                issues.append(f"industry_map: 边的 target 未定义 {edge.target}")
            if edge.flow_value and not edge.flow_known:
                issues.append("industry_map: flow_known=False 却给了 flow_value（不许编造流量）")
        for node in imap.nodes:
            for ref in _refs(node):
                _check_ref(ref, f"industry_map:{node.node_id}")
        if imap.nodes and not imap.layers:
            issues.append("industry_map: 有节点但无分层（页面无法布局）")

    cand = structures.get("candidate_assessment")
    if isinstance(cand, CandidateAssessment):
        seen: set[str] = set()
        for item in cand.candidates:
            if item.entity_id in seen:
                issues.append(f"candidate_assessment: 公司重复 {item.entity_id}")
            seen.add(item.entity_id)
            if item.tier == "excluded" and not item.reason:
                issues.append(f"candidate_assessment: {item.entity_id} 淘汰必须给原因")
            if item.listing_status == "listed" and not item.market:
                issues.append(f"candidate_assessment: {item.entity_id} 上市必须给市场")
            if item.listing_status in ("private", "subsidiary") and item.investable is True:
                issues.append(
                    f"candidate_assessment: {item.entity_id} 未上市/子公司不得标为可交易候选"
                )
            for ref in _refs(item):
                _check_ref(ref, f"candidate_assessment:{item.entity_id}")

    matrix = structures.get("comparison_matrix")
    if isinstance(matrix, ComparisonMatrix):
        col_ids = {c.get("id") for c in matrix.columns}
        for row in matrix.rows:
            unknown = set(row.cells) - col_ids
            if unknown:
                issues.append(f"comparison_matrix: 行 {row.label} 有未定义列 {sorted(unknown)}")
            if not row.comparable and not row.incomparable_reason:
                issues.append(f"comparison_matrix: 行 {row.label} 不可比却未给原因")
            for oid in row.observation_ids.values():
                _check_ref(oid, f"comparison_matrix:{row.label}")
        if matrix.chartable and any(not r.comparable for r in matrix.rows):
            issues.append("comparison_matrix: 存在不可比行时不得标 chartable（不许硬画图）")

    timeline = structures.get("validation_timeline")
    if isinstance(timeline, ValidationTimeline):
        for item in timeline.items:
            if item.status == "expected" and not (item.window_start or item.window_end):
                issues.append(f"validation_timeline: 预计事件 {item.event} 缺时间范围")
            if not item.trigger_condition:
                issues.append(f"validation_timeline: {item.event} 缺触发条件（页面只能展示条件）")
            for ref in _refs(item):
                _check_ref(ref, f"validation_timeline:{item.event}")

    summary = structures.get("executive_summary")
    if isinstance(summary, ExecutiveSummary):
        if not summary.answer:
            issues.append("executive_summary: 必须给出回答用户目标的结论")
        for ref in summary.refs:
            _check_ref(ref, "executive_summary")
    return issues


def structures_payload(structures: dict[str, BaseModel]) -> dict[str, Any]:
    return {k: v.model_dump(mode="json") for k, v in structures.items()}


__all__ = [
    "IndustryMap", "IndustryMapNode", "IndustryMapEdge", "CandidateAssessment",
    "CandidateItem", "ComparisonMatrix", "ComparisonRow", "ValidationTimeline",
    "ValidationItem", "ExecutiveSummary", "STRUCTURE_MODELS", "StructureError",
    "parse_structures", "validate_structures", "structures_payload", "StructureKind",
]

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

#: 关系规范词汇（前端据此定样式；未知值保留原文如实显示，不硬译）
CANONICAL_RELATIONS: tuple[str, ...] = (
    "supplies", "competes", "substitutes", "depends_on", "enables", "value_flow",
)


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
    """节点间关系：上下游/供给/替代/竞争。流量未知时用等宽边（不编造粗细）。

    relation 推荐用 CANONICAL_RELATIONS 词汇（归一层会把常见中文同义词映射过去）；
    未知值保留原文——前端按 `RELATION_LABELS[r] ?? r` 如实显示，不硬译、不丢语义。
    （事故：合成器给「支撑/价值捕获/采购」被 Literal 整批拒掉，内容全丢。）"""

    source: str
    target: str
    relation: str = "supplies"
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
    #: 规范 layer key → 展示名（归一层从显示名捕获，如 upstream → 上游算力与基础设施）
    layer_labels: dict[str, str] = Field(default_factory=dict)
    #: 技术路线对比（路线 → 成熟度/成本/代表公司）
    routes: list[dict[str, str]] = Field(default_factory=list)
    #: 价值流/利润池说明（带引用；合成器常交，入 schema 而不是被 extra=forbid 拒掉）
    value_flow_note: str = ""
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
    """首屏结论：必须回答用户目标（audit §3.8）+ tear-sheet 字段（升级方案 §5/§26）。

    tear-sheet 字段全部可选：有则首屏按投资语义分块渲染，无则不显示（不编造）。"""

    objective: str = ""
    answer: str = ""
    #: 行业/公司所处阶段（如「商业兑现早期」）——离散描述，不是评分
    stage: str = ""
    #: 为什么现在值得关注（3-5 条，每条绑证据更好）
    why_now: list[str] = Field(default_factory=list)
    #: 价值捕获在哪里（哪个环节/角色赚到钱）
    value_capture: str = ""
    #: 证伪条件（kill criteria，方案 §15）：什么事情发生会推翻本结论
    thesis_breakers: list[str] = Field(default_factory=list)
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


# ---------------- 确定性形状归一（宽进严出的「宽进」半边） ----------------
#
# 事故形态（live-a2cce641 synthesize，2026-09-09，6 连拒 → 产物冻结 structures=[]）：
# 合成器内容完备、67 个引用全部可解析，却因字段形状漂移全军覆没——
# bottleneck 给描述字符串、relation/status 给中文或近义词（「支撑」「pending」）、
# layers 给显示名而 node.layer 是 key、cells 给数组、tiers/main_basis 给字符串。
# 一个 kind 的一个字段非法 → 整批拒 → 模型 12 步预算烧光 → 页面只剩占位符与长文本。
#
# 纪律：归一只做**不改语义**的确定性修复（同义词表/描述搬移/单元素包装/按列名 zip），
# 每条修复记入 repairs（提交响应与事件留痕，不静默改写）；修不了的保持原样，
# 由 parse/validate 照常拒绝——宽松只针对形状，语义校验（引用可解析/图完整性/
# 可比口径/不许编造流量）在归一之后原样执行，一条不放松。

#: 关系同义词 → 规范词（未收录的保留原文，前端如实显示，不硬译）
RELATION_SYNONYMS: dict[str, str] = {
    "供给": "supplies", "供应": "supplies", "提供": "supplies", "出售": "supplies",
    "销售": "supplies", "采购": "supplies", "供货": "supplies",
    "竞争": "competes", "对抗": "competes",
    "替代": "substitutes", "取代": "substitutes",
    "依赖": "depends_on", "需要": "depends_on", "受制于": "depends_on",
    "支撑": "enables", "支持": "enables", "赋能": "enables", "驱动": "enables",
    "使能": "enables", "奠基": "enables",
    "价值捕获": "value_flow", "价值流": "value_flow", "价值流转": "value_flow",
    "资金流": "value_flow", "现金流": "value_flow",
}

#: 时间线状态同义词 → 规范三态
STATUS_SYNONYMS: dict[str, str] = {
    "pending": "expected", "upcoming": "expected", "anticipated": "expected",
    "scheduled": "expected", "in_progress": "expected", "expected": "expected",
    "待验证": "expected", "待发生": "expected", "预期": "expected", "预计": "expected",
    "进行中": "expected",
    "occurred": "occurred", "happened": "occurred", "done": "occurred",
    "completed": "occurred", "已发生": "occurred", "已完成": "occurred",
    "已兑现": "occurred", "已落地": "occurred",
    "unknown": "unknown", "未知": "unknown", "不确定": "unknown", "": "unknown",
}

#: 分层显示名关键词 → 规范 key（按插入序匹配，前缀命中优先）
LAYER_SYNONYMS: dict[str, str] = {
    "上游": "upstream", "中游": "midstream", "下游": "downstream",
    "平台": "platform", "应用": "application", "基础设施": "infrastructure",
    "算力": "infrastructure", "需求": "demand", "学术": "demand", "政府": "demand",
    "终端": "demand", "科研": "demand",
}

TIER_SYNONYMS: dict[str, str] = {
    "入选": "included", "核心": "included", "纳入": "included",
    "观察": "watchlist", "跟踪": "watchlist", "观察名单": "watchlist",
    "淘汰": "excluded", "排除": "excluded",
    "待核实": "needs_review", "待复核": "needs_review", "需复核": "needs_review",
}

LISTING_SYNONYMS: dict[str, str] = {
    "已上市": "listed", "上市": "listed", "未上市": "private", "私有": "private",
    "子公司": "subsidiary", "附属公司": "subsidiary", "未知": "unknown",
}

_BOOL_TRUE = {"true", "yes", "y", "是", "有", "1"}
_BOOL_FALSE = {"false", "no", "n", "否", "无", "没有", "0", ""}


def _canon(value: Any, table: dict[str, str]) -> Any:
    """枚举字段同义词归一（strip + 查表）；未命中原样返回（由 Literal 拒绝）。"""
    if not isinstance(value, str):
        return value
    s = value.strip()
    return table.get(s, table.get(s.lower(), s))


def _canon_layer(value: Any) -> tuple[Any, str]:
    """分层名归一 → (规范 key 或原值, 捕获的显示名)。显示名含关键词即映射。"""
    if not isinstance(value, str):
        return value, ""
    s = value.strip()
    if s in {"upstream", "midstream", "downstream", "platform",
             "application", "infrastructure", "demand"}:
        return s, ""
    for kw, key in LAYER_SYNONYMS.items():
        if s.startswith(kw):
            return key, s
    for kw, key in LAYER_SYNONYMS.items():
        if kw in s:
            return key, s
    return s, ""


def _as_list(value: Any, repairs: list[str], where: str) -> Any:
    """字符串 → 单元素列表（模型把列表字段写成单句时的保守包装，不拆句）。"""
    if isinstance(value, str):
        s = value.strip()
        repairs.append(f"{where}: 字符串 → 单元素列表")
        return [s] if s else []
    return value


def _split_names(value: str) -> list[str]:
    """公司名枚举串 → 列表：只按顿号/分号切（中文枚举分隔符，不拆逗号从句）。"""
    parts = [p.strip() for p in value.replace("；", "、").split("、")]
    return [p for p in parts if p]


def _coerce_bool(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s in _BOOL_TRUE:
            return True
        if s in _BOOL_FALSE:
            return False
    return value


def normalize_structures(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """形状归一（确定性、不改语义、逐条留痕）：返回 (归一后 payload, repairs)。

    只处理已观察到的漂移模式（同义词/包装/搬移/zip）；其余原样交给
    parse/validate 拒绝。不发明引用、不改数值、不补内容。
    """
    out: dict[str, Any] = {}
    repairs: list[str] = []
    for kind, payload in (raw or {}).items():
        if not isinstance(payload, dict):
            out[kind] = payload
            continue
        p = dict(payload)
        if kind == "industry_map":
            _norm_industry_map(p, repairs)
        elif kind == "candidate_assessment":
            _norm_candidate_assessment(p, repairs)
        elif kind == "comparison_matrix":
            _norm_comparison_matrix(p, repairs)
        elif kind == "validation_timeline":
            _norm_validation_timeline(p, repairs)
        elif kind == "executive_summary":
            _norm_executive_summary(p, repairs)
        out[kind] = p
    return out, repairs


def _norm_industry_map(p: dict[str, Any], repairs: list[str]) -> None:
    labels: dict[str, str] = dict(p.get("layer_labels") or {})
    nodes = p.get("nodes")
    if isinstance(nodes, list):
        for i, node in enumerate(nodes):
            if not isinstance(node, dict):
                continue
            where = f"industry_map.nodes[{i}]"
            if isinstance(node.get("node_id"), str):
                node["node_id"] = node["node_id"].strip()
            # bottleneck：描述字符串 = 断言瓶颈存在且给了理由 → true + 描述搬入 note（不丢信息）
            b = node.get("bottleneck")
            if isinstance(b, str):
                s = b.strip()
                if s.lower() in _BOOL_TRUE:
                    node["bottleneck"] = True
                elif s.lower() in _BOOL_FALSE or s in {"非瓶颈", "不是瓶颈"}:
                    node["bottleneck"] = False
                elif s:
                    node["bottleneck"] = True
                    old = str(node.get("note") or "").strip()
                    node["note"] = f"瓶颈：{s}" + (f"；{old}" if old else "")
                    repairs.append(f"{where}.bottleneck: 描述字符串 → true，描述移入 note")
                else:
                    node["bottleneck"] = False
            key, display = _canon_layer(node.get("layer"))
            if display and key != node.get("layer"):
                repairs.append(f"{where}.layer: {node.get('layer')!r} → {key!r}（显示名存入 layer_labels）")
                labels.setdefault(str(key), display)
                node["layer"] = key
            for field in ("company_refs", "evidence_refs"):
                if isinstance(node.get(field), str):
                    node[field] = _as_list(node[field], repairs, f"{where}.{field}")
    edges = p.get("edges")
    if isinstance(edges, list):
        for i, edge in enumerate(edges):
            if not isinstance(edge, dict):
                continue
            where = f"industry_map.edges[{i}]"
            r = edge.get("relation")
            if isinstance(r, str):
                canon = RELATION_SYNONYMS.get(r.strip(), r.strip())
                if canon != r:
                    repairs.append(f"{where}.relation: {r!r} → {canon!r}")
                    edge["relation"] = canon
            if "flow_known" in edge:
                edge["flow_known"] = _coerce_bool(edge["flow_known"])
            if isinstance(edge.get("evidence_refs"), str):
                edge["evidence_refs"] = _as_list(edge["evidence_refs"], repairs, f"{where}.evidence_refs")
    # layers：显示名 → 规范 key；补齐遗漏的节点分层（否则节点在前端不可见）
    layers = p.get("layers")
    if isinstance(layers, list):
        canon_layers: list[str] = []
        for entry in layers:
            key, display = _canon_layer(entry)
            if display:
                labels.setdefault(str(key), display)
            key = str(key)
            if key not in canon_layers:
                canon_layers.append(key)
        if canon_layers != [str(x) for x in layers]:
            repairs.append("industry_map.layers: 显示名归一为规范 key，原名存入 layer_labels")
        node_keys: list[str] = []
        for node in nodes if isinstance(nodes, list) else []:
            if isinstance(node, dict):
                k = str(node.get("layer") or "")
                if k and k not in node_keys:
                    node_keys.append(k)
        missing = [k for k in node_keys if k not in canon_layers]
        if missing:
            canon_layers.extend(missing)
            repairs.append(f"industry_map.layers: 补齐节点已用但未列入的分层 {missing}（否则节点不可见）")
        p["layers"] = canon_layers
    if labels:
        p["layer_labels"] = labels
    routes = p.get("routes")
    if isinstance(routes, list):
        fixed = []
        for r in routes:
            if isinstance(r, str):
                fixed.append({"route": r})
                repairs.append("industry_map.routes: 字符串 → {route: …}")
            elif isinstance(r, dict):
                fixed.append({str(k): str(v) for k, v in r.items()})
            else:
                fixed.append(r)
        p["routes"] = fixed
    for field in ("bottlenecks", "limitations"):
        if isinstance(p.get(field), str):
            p[field] = _as_list(p[field], repairs, f"industry_map.{field}")


def _norm_candidate_assessment(p: dict[str, Any], repairs: list[str]) -> None:
    for field in ("criteria", "limitations"):
        if isinstance(p.get(field), str):
            p[field] = _as_list(p[field], repairs, f"candidate_assessment.{field}")
    cands = p.get("candidates")
    if not isinstance(cands, list):
        return
    for i, c in enumerate(cands):
        if not isinstance(c, dict):
            continue
        where = f"candidate_assessment.candidates[{i}]"
        if isinstance(c.get("entity_id"), str):
            stripped = c["entity_id"].strip()
            if stripped != c["entity_id"]:
                repairs.append(f"{where}.entity_id: 去除首尾空白")
            c["entity_id"] = stripped
        if isinstance(c.get("listing_status"), str):
            c["listing_status"] = _canon(c["listing_status"], LISTING_SYNONYMS)
        if isinstance(c.get("tier"), str):
            c["tier"] = _canon(c["tier"], TIER_SYNONYMS)
        if "investable" in c:
            c["investable"] = _coerce_bool(c["investable"])
        for field in ("moat_evidence", "commercial_evidence", "sustainability_evidence",
                      "counter_evidence", "evidence_refs", "company_refs"):
            if isinstance(c.get(field), str):
                c[field] = _as_list(c[field], repairs, f"{where}.{field}")


def _norm_comparison_matrix(p: dict[str, Any], repairs: list[str]) -> None:
    cols = p.get("columns")
    col_ids = [
        str(c.get("id")) for c in cols
        if isinstance(c, dict) and c.get("id")
    ] if isinstance(cols, list) else []
    rows = p.get("rows")
    if not isinstance(rows, list):
        return
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        where = f"comparison_matrix.rows[{i}]"
        # 数组 → 按列序 zip 成 dict（长度必须对齐，否则保留原样让校验给出明确拒绝）
        for field in ("cells", "observation_ids"):
            v = row.get(field)
            if isinstance(v, list) and col_ids and len(v) == len(col_ids):
                row[field] = {cid: val for cid, val in zip(col_ids, v, strict=True)}
                repairs.append(f"{where}.{field}: 数组按列序 zip 为 dict")
        if isinstance(row.get("cells"), dict):
            row["cells"] = {
                str(k): (None if val is None else str(val))
                for k, val in row["cells"].items()
            }
        if "comparable" in row:
            row["comparable"] = _coerce_bool(row["comparable"])
    if "chartable" in p:
        p["chartable"] = _coerce_bool(p["chartable"])
    # 不可比行存在时 chartable 强制降级（确定性执行既有纪律：不许硬画图，
    # 表格内容保留）——而不是整 kind 拒绝丢掉全表
    rows_list = p.get("rows")
    if p.get("chartable") is True and isinstance(rows_list, list) and any(
        isinstance(r, dict) and r.get("comparable") is False for r in rows_list
    ):
        p["chartable"] = False
        repairs.append("comparison_matrix.chartable: 存在不可比行 → 降级 false（只给表，不绘图）")


def _norm_validation_timeline(p: dict[str, Any], repairs: list[str]) -> None:
    if isinstance(p.get("limitations"), str):
        p["limitations"] = _as_list(p["limitations"], repairs, "validation_timeline.limitations")
    items = p.get("items")
    if not isinstance(items, list):
        return
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        where = f"validation_timeline.items[{i}]"
        s = item.get("status")
        if isinstance(s, str):
            canon = STATUS_SYNONYMS.get(s.strip(), STATUS_SYNONYMS.get(s.strip().lower(), s.strip()))
            if canon != s:
                repairs.append(f"{where}.status: {s!r} → {canon!r}")
                item["status"] = canon
        for field in ("company_refs", "evidence_refs"):
            if isinstance(item.get(field), str):
                item[field] = _as_list(item[field], repairs, f"{where}.{field}")


def _norm_executive_summary(p: dict[str, Any], repairs: list[str]) -> None:
    for field in ("main_basis", "limitations", "refs", "why_now", "thesis_breakers"):
        if isinstance(p.get(field), str):
            p[field] = _as_list(p[field], repairs, f"executive_summary.{field}")
    tiers = p.get("tiers")
    if isinstance(tiers, dict):
        fixed: dict[str, Any] = {}
        for k, v in tiers.items():
            key = str(_canon(k, TIER_SYNONYMS))
            if isinstance(v, str):
                names = _split_names(v)
                repairs.append(
                    f"executive_summary.tiers.{key}: 字符串 → {len(names)} 项列表（按顿号切分）"
                )
                fixed[key] = names
            else:
                fixed[key] = v
        p["tiers"] = fixed
    cred = p.get("credibility")
    if isinstance(cred, str):
        p["credibility"] = {"总体": cred.strip()}
        repairs.append("executive_summary.credibility: 字符串 → {总体: …}")
    elif isinstance(cred, list):
        p["credibility"] = {"总体": "；".join(str(x) for x in cred)}
        repairs.append("executive_summary.credibility: 列表 → {总体: 合并}")


def parse_structures_partial(
    raw: dict[str, Any],
) -> tuple[dict[str, BaseModel], dict[str, str], list[str]]:
    """逐 kind 归一 + 解析（部分接受）：返回 (accepted, failures{kind: 原因}, repairs)。

    一个 kind 形状非法不再拖死其余 kind（事故：comparison_matrix 的 cells 写成数组，
    同批完好的 candidate_assessment 等 4 个 kind 被连带拒绝）。
    """
    normalized, repairs = normalize_structures(raw or {})
    accepted: dict[str, BaseModel] = {}
    failures: dict[str, str] = {}
    for kind, payload in normalized.items():
        model = STRUCTURE_MODELS.get(kind)
        if model is None:
            failures[kind] = f"未知结构产物（可用：{sorted(STRUCTURE_MODELS)}）"
            continue
        if payload is None:
            continue
        try:
            accepted[kind] = model.model_validate(payload)
        except Exception as e:  # noqa: BLE001 - pydantic 错误转可读拒绝（按 kind 隔离）
            failures[kind] = f"{type(e).__name__}: {e}"
    return accepted, failures, repairs


def parse_structures(raw: dict[str, Any]) -> dict[str, BaseModel]:
    """合成器提交的结构候选 → 类型化对象（严格模式：任一 kind 非法即拒）。

    先过确定性形状归一（normalize_structures）再验证；未知 kind 直接拒，不静默丢弃。
    """
    accepted, failures, _repairs = parse_structures_partial(raw)
    if failures:
        kind, reason = next(iter(failures.items()))
        raise StructureError(f"{kind} 结构非法：{reason}")
    return accepted


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
    "parse_structures", "parse_structures_partial", "normalize_structures",
    "validate_structures", "structures_payload", "StructureKind",
    "CANONICAL_RELATIONS", "RELATION_SYNONYMS", "STATUS_SYNONYMS", "LAYER_SYNONYMS",
]

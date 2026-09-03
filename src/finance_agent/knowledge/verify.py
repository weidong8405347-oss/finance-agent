"""知识准入质检（verify gate）：不是所有研究产出都配进知识库。

两层防线（对应验收事故「完整度 100% 但点进去没内容」的整改）：

1. **写侧硬门禁**（``assert_value_admissible``，ProfileWriter 在落库前调用，
   fail-closed）：空值 / 占位符 / 值是序列化 JSON 字符串 / 结构化字段类型违例
   → 直接拒写。这类值没有任何争议空间，属于客观残次品。
2. **读侧质量投影**（``verify_entity``，纯投影无状态——原则 3）：对每个必填字段
   跑软检查（内容过短 / 缺数值锚点 / 仅 C 级证据 / 开放冲突 / 陈旧），汇总成
   per-field status、quality_score 与实体级 status（verified / draft）。
   UI 与 HTML 存档的状态 pill 都从这里投影——「完整度」从此不再单独撒谎。

质检结果是投影而非事件：同一档案在任何时刻重算都得到同一结论（可复现），
且随新事实写入自动更新，不需要额外的「验收通过」人工动作来维持。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from .errors import KnowledgeQualityError
from .models import Evidence, FactRecord, PitGrade
from .schema import SCHEMAS
from .store import BitemporalStore

#: 结构化字段注册表（single source；research/tools.py 的写侧早期校验从这里导入）：
#: 值必须是 list[dict] 且每条含必备键——2026-09-01 实测模型把 player_landscape
#: 写成 1491 字符的 JSON 字符串，下游读到一坨不可解析文本。
STRUCTURED_LIST_FIELDS: dict[str, tuple[str, ...]] = {
    "player_landscape": ("ticker", "evidence_ids"),
    "sub_sectors": ("name",),
}

#: 数值锚点字段：值里至少要出现一个数字（定性散文允许没有数字）
NUMERIC_ANCHOR_FIELDS: frozenset[str] = frozenset(
    {"revenue_fy", "net_income_fy", "cash_flow", "valuation", "market_size", "growth_rate", "market_share"}
)

#: 散文类字段的有效内容下限（字符）。低于此长度视为「写了等于没写」。
PROSE_MIN_CHARS = 12

#: 占位符：值整体就是这些词之一 = 没写（精确匹配，避免误杀「风险未知因素包括…」）
_PLACEHOLDERS = frozenset(
    {"待补充", "待定", "暂无", "未知", "无", "n/a", "na", "tbd", "todo", "unknown", "none", "-", "—"}
)

#: 字段状态：ok 通过；weak 有软瑕疵；stale 陈旧；conflict 开放冲突；missing 缺失
FieldStatus = Literal["ok", "weak", "stale", "conflict", "missing"]
#: 实体状态：verified = 必填字段全部 ok 且无开放冲突；draft = 其余（含空档案）
EntityStatus = Literal["verified", "draft"]

#: 字段状态 → 质量分权重（实体 quality_score = 必填字段均值）
_STATUS_WEIGHT: dict[FieldStatus, float] = {
    "ok": 1.0,
    "weak": 0.6,
    "stale": 0.5,
    "conflict": 0.3,
    "missing": 0.0,
}

_STATUS_RANK: dict[FieldStatus, int] = {
    "missing": 0, "conflict": 1, "stale": 2, "weak": 3, "ok": 4,
}


class FieldQuality(BaseModel):
    field: str
    status: FieldStatus
    issues: list[str] = []
    required: bool = True


class EntityQuality(BaseModel):
    """实体级质量投影：UI 状态 pill 与存档 header 的数据源。"""

    entity_kind: str
    entity_id: str
    as_of: datetime
    fields: list[FieldQuality]
    quality_score: float  # 0..1，必填字段状态权重均值
    status: EntityStatus
    issues: list[str]  # 实体级人类可读摘要（Weak/冲突/陈旧计数）


# ---------------- 写侧硬门禁 ----------------


def looks_like_serialized_json(value: str) -> bool:
    """值整体是一段序列化 JSON（对象/数组）——模型偷懒dump，下游不可读。"""
    s = value.strip()
    if not s or s[0] not in "{[":
        return False
    try:
        return isinstance(json.loads(s), (dict, list))
    except (json.JSONDecodeError, ValueError):
        return False


def assert_value_admissible(field: str, value: Any) -> None:
    """字段值的准入硬门禁（fail-closed）；不合格抛 KnowledgeQualityError。

    只拦客观残次品，不做主观质量判断（那属于读侧投影与 rubric 软反馈）：
    - 空值 / 纯空白；
    - 占位符（值整体是「待补充」「TBD」之类）；
    - 值是序列化 JSON 字符串（结构化数据必须传对象，不许 dump 成字符串）；
    - 结构化字段（STRUCTURED_LIST_FIELDS）类型/必备键违例。
    """
    if value is None:
        raise KnowledgeQualityError(f"字段 {field} 的值为空（None）——写不出就留白，不要写空值")
    if isinstance(value, str):
        s = value.strip()
        if not s:
            raise KnowledgeQualityError(f"字段 {field} 的值是空白字符串——写不出就留白")
        if s.lower() in _PLACEHOLDERS:
            raise KnowledgeQualityError(f"字段 {field} 的值是占位符「{s}」——占位符不算研究产出")
        if looks_like_serialized_json(s):
            raise KnowledgeQualityError(
                f"字段 {field} 的值是序列化 JSON 字符串——结构化数据请传对象/数组，不要 dump 成字符串"
            )
    if isinstance(value, (list, dict)) and not value:
        raise KnowledgeQualityError(f"字段 {field} 的值是空容器——写不出就留白")

    spec = STRUCTURED_LIST_FIELDS.get(field)
    if spec is not None:
        if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
            raise KnowledgeQualityError(
                f"字段 {field} 的值必须是 list[object]（收到 {type(value).__name__}）"
            )
        bad = [i for i, v in enumerate(value) if any(k not in v for k in spec)]
        if bad:
            raise KnowledgeQualityError(f"字段 {field} 的第 {bad} 条缺必备键 {spec}")


# ---------------- 读侧软检查（投影） ----------------


def field_issues(field: str, rec: FactRecord, evidences: list[Evidence]) -> list[str]:
    """单字段软检查（不落库、只投影）；返回人类可读 issue 列表（空 = 通过）。"""
    issues: list[str] = []
    v = rec.value
    if isinstance(v, str):
        if field in NUMERIC_ANCHOR_FIELDS and not any(ch.isdigit() for ch in v):
            issues.append("缺少数值锚点")
        elif field not in NUMERIC_ANCHOR_FIELDS and len(v.strip()) < PROSE_MIN_CHARS:
            issues.append("内容过短")
    elif isinstance(v, list) and not v:
        issues.append("空列表")
    if evidences and all(e.pit_grade is PitGrade.C for e in evidences):
        issues.append("仅 C 级证据（无 PIT 保证）")
    return issues


def verify_entity(
    store: BitemporalStore,
    entity_kind: str,
    entity_id: str,
    as_of: datetime,
    *,
    namespace: str = "prod",
) -> EntityQuality:
    """实体质量投影：每个必填字段跑软检查，汇总 quality_score 与实体 status。

    纯只读——同一 (as_of, namespace) 下结果确定，可随处重算。
    """
    schema = SCHEMAS.get(entity_kind)
    profile = store.view(entity_kind, entity_id, as_of, namespace=namespace)
    open_conflicts = {
        r.field for r in store.open_conflicts(entity_kind, entity_id, namespace=namespace)
    }

    fields: list[FieldQuality] = []
    entity_issues: list[str] = []
    required_fields = list(schema.required) if schema else []

    def _assess(field: str, *, required: bool) -> FieldQuality:
        rec = profile.get(field)
        if rec is None:
            return FieldQuality(field=field, status="missing", issues=["缺失"], required=required)
        issues: list[str] = []
        status: FieldStatus = "ok"
        if rec.conflict_flag or field in open_conflicts:
            status = "conflict"
            issues.append("开放冲突待裁决")
        policy = schema.required.get(field) if (schema and required) else (
            schema.optional.get(field) if schema else None
        )
        if (
            policy is not None
            and policy.max_age_days is not None
            and (as_of - rec.knowledge_time).days > policy.max_age_days
        ):
            issues.append(f"陈旧（>{policy.max_age_days}d 未刷新）")
            # 状态取最严重者（rank 越小越严重），不覆盖 conflict
            if _STATUS_RANK["stale"] < _STATUS_RANK[status]:
                status = "stale"
        evidences = []
        for eid in rec.evidence_ids:
            try:
                evidences.append(store.get_evidence(eid))
            except Exception:
                issues.append(f"证据 {eid} 缺失")
        soft = field_issues(field, rec, evidences)
        issues.extend(soft)
        if soft and _STATUS_RANK["weak"] < _STATUS_RANK[status]:
            status = "weak"
        return FieldQuality(field=field, status=status, issues=issues, required=required)

    for f in required_fields:
        fields.append(_assess(f, required=True))
    optional_present = [f for f in (schema.optional if schema else {}) if f in profile]
    for f in optional_present:
        fields.append(_assess(f, required=False))
    # schema 外的事实（如 thesis、扩展字段）：展示但不计入分
    extras = [f for f in profile if f not in {fq.field for fq in fields}]
    for f in sorted(extras):
        fields.append(_assess(f, required=False))

    if required_fields:
        score = sum(_STATUS_WEIGHT[fq.status] for fq in fields if fq.required) / len(required_fields)
    else:
        score = 0.0

    n_missing = sum(1 for fq in fields if fq.required and fq.status == "missing")
    n_weak = sum(1 for fq in fields if fq.required and fq.status == "weak")
    n_stale = sum(1 for fq in fields if fq.required and fq.status == "stale")
    n_conflict = sum(1 for fq in fields if fq.status == "conflict")
    if schema is None:
        entity_issues.append("未知实体类型，无 schema 验收基准")
    elif not profile:
        entity_issues.append("尚无事实落库")
    if n_missing:
        entity_issues.append(f"{n_missing} 个必填字段缺失")
    if n_weak:
        entity_issues.append(f"{n_weak} 个字段质检存疑")
    if n_stale:
        entity_issues.append(f"{n_stale} 个字段陈旧")
    if n_conflict:
        entity_issues.append(f"{n_conflict} 个字段冲突待裁决")

    verified = bool(required_fields) and all(
        fq.status == "ok" for fq in fields if fq.required
    ) and n_conflict == 0
    return EntityQuality(
        entity_kind=entity_kind,
        entity_id=entity_id,
        as_of=as_of,
        fields=fields,
        quality_score=round(score, 4),
        status="verified" if verified else "draft",
        issues=entity_issues,
    )

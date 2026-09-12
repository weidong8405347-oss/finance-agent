"""候选四象限坐标（升级方案 §20）：离散阶段的规范序映射 → 网格 2D 散点。

纪律：
- 坐标来自**离散分类的规范序映射**（technology_stage / commercial_stage 是分类，
  不是评分），不是连续打分——序只表达「阶段先后」，不表达「好坏程度」；
- 无法映射的公司进 unpositioned 列（如实标注原因），不塞进图里、不猜坐标；
- 同义词表只收确定等价词（canonical 英文 + 已在前端使用的中文对照），
  语义含混的一律不映射；
- 纯函数、确定性：同一候选集必得同一坐标集。
"""

from __future__ import annotations

from typing import Any

#: 技术/护城河验证轴（X）规范序：与前端 viz.CANONICAL_STAGE_ORDER 同源
TECHNOLOGY_STAGE_ORDER: tuple[str, ...] = (
    "early", "preclinical", "clinical", "commercial", "mature",
)

#: 商业验证轴（Y）规范序（§20 Y=Commercial Validation）
COMMERCIAL_STAGE_ORDER: tuple[str, ...] = (
    "none", "pilot", "early_revenue", "scaling", "profitable",
)

#: 规范序 → 中文展示标签（前后端共用，前端不硬译未知值）
STAGE_LABELS_CN: dict[str, str] = {
    "early": "早期", "preclinical": "临床前", "clinical": "临床",
    "commercial": "商业化", "mature": "成熟",
    "none": "未商业化", "pilot": "试点/验证性收入", "early_revenue": "早期收入",
    "scaling": "规模化放量", "profitable": "盈利",
}

#: 同义词 → canonical（只收确定等价词；语义含混（如 poc/demo）不映射，进未定位）。
#: 两轴分表：同一词在不同轴上的语义不同（如技术轴的 commercial=技术已商用，
#: 相当于商业轴的 scaling；商业轴的 mature=持续盈利），按轴映射不混用。
_TECH_SYNONYMS: dict[str, str] = {
    "early": "early", "早期": "early",
    "preclinical": "preclinical", "临床前": "preclinical",
    "clinical": "clinical", "临床": "clinical",
    "commercial": "commercial", "商业化": "commercial", "已商用": "commercial",
    "mature": "mature", "成熟": "mature",
}
_COMMERCIAL_SYNONYMS: dict[str, str] = {
    "none": "none", "pre_revenue": "none", "pre-revenue": "none",
    "未商业化": "none", "无收入": "none", "未产生收入": "none",
    "pilot": "pilot", "试点": "pilot", "验证性收入": "pilot",
    "early_revenue": "early_revenue", "早期收入": "early_revenue",
    "初步商业化": "early_revenue", "商业化早期": "early_revenue",
    "scaling": "scaling", "规模化": "scaling", "放量": "scaling", "规模化放量": "scaling",
    "commercial": "scaling", "商业化": "scaling", "已商业化": "scaling",
    "profitable": "profitable", "盈利": "profitable", "已盈利": "profitable",
    "mature": "profitable", "成熟": "profitable",
}
_AXIS_SYNONYMS = {"technology": _TECH_SYNONYMS, "commercial": _COMMERCIAL_SYNONYMS}


def stage_ordinal(value: str | None, axis: str) -> tuple[int | None, str]:
    """阶段分类 → (规范序 0-4, canonical 名)；不可映射 → (None, "")。

    axis: "technology" | "commercial"。空值/未知词 → None（调用方进未定位列）。
    """
    if not value or not str(value).strip():
        return None, ""
    table = _AXIS_SYNONYMS[axis]
    canon = table.get(str(value).strip().lower()) or table.get(str(value).strip())
    if canon is None:
        return None, ""
    order = TECHNOLOGY_STAGE_ORDER if axis == "technology" else COMMERCIAL_STAGE_ORDER
    return order.index(canon), canon


def build_quadrant(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """候选列表 → 四象限 payload（points + unpositioned）。

    每点带 entity_id/name/tier/离散坐标/原始阶段值/证据计数；两轴任一不可映射
    即进 unpositioned（reason 注明哪一轴、原值是什么）。
    """
    points: list[dict[str, Any]] = []
    unpositioned: list[dict[str, Any]] = []
    for c in candidates:
        entity_id = str(c.get("entity_id") or "")
        name = str(c.get("name") or entity_id)
        tier = str(c.get("tier") or "")
        evidence_count = len(c.get("evidence_refs") or [])
        x, x_canon = stage_ordinal(c.get("technology_stage"), "technology")
        y, y_canon = stage_ordinal(c.get("commercial_stage"), "commercial")
        if x is None or y is None:
            reasons: list[str] = []
            if x is None:
                raw = str(c.get("technology_stage") or "").strip()
                reasons.append(
                    f"technology_stage {raw!r} 无法映射规范序" if raw else "technology_stage 未判定"
                )
            if y is None:
                raw = str(c.get("commercial_stage") or "").strip()
                reasons.append(
                    f"commercial_stage {raw!r} 无法映射规范序" if raw else "commercial_stage 未判定"
                )
            unpositioned.append({
                "entity_id": entity_id, "name": name, "tier": tier,
                "evidence_count": evidence_count, "reason": "；".join(reasons),
            })
            continue
        points.append({
            "entity_id": entity_id, "name": name, "tier": tier,
            "x": x, "y": y, "x_stage": x_canon, "y_stage": y_canon,
            "evidence_count": evidence_count,
        })
    return {
        "x_axis": {
            "key": "technology_stage", "title": "技术/护城河验证",
            "order": list(TECHNOLOGY_STAGE_ORDER),
            "labels": {k: STAGE_LABELS_CN[k] for k in TECHNOLOGY_STAGE_ORDER},
        },
        "y_axis": {
            "key": "commercial_stage", "title": "商业验证",
            "order": list(COMMERCIAL_STAGE_ORDER),
            "labels": {k: STAGE_LABELS_CN[k] for k in COMMERCIAL_STAGE_ORDER},
        },
        "points": points,
        "unpositioned": unpositioned,
    }


__all__ = [
    "TECHNOLOGY_STAGE_ORDER", "COMMERCIAL_STAGE_ORDER", "STAGE_LABELS_CN",
    "stage_ordinal", "build_quadrant",
]

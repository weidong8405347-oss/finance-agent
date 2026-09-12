"""候选四象限坐标验收（升级方案 §20）。

关键场景：离散阶段的规范序映射（不是评分）；同义词按轴归一；无法映射的公司
进 unpositioned 并注明原因（不塞进图里）；空阶段如实未定位；证据计数透传。
"""

from finance_agent.dossier.positioning import (
    COMMERCIAL_STAGE_ORDER,
    TECHNOLOGY_STAGE_ORDER,
    build_quadrant,
    stage_ordinal,
)


class TestStageOrdinal:
    def test_canonical_technology_order(self):
        assert stage_ordinal("early", "technology") == (0, "early")
        assert stage_ordinal("preclinical", "technology") == (1, "preclinical")
        assert stage_ordinal("clinical", "technology") == (2, "clinical")
        assert stage_ordinal("commercial", "technology") == (3, "commercial")
        assert stage_ordinal("mature", "technology") == (4, "mature")

    def test_canonical_commercial_order(self):
        assert stage_ordinal("none", "commercial") == (0, "none")
        assert stage_ordinal("pilot", "commercial") == (1, "pilot")
        assert stage_ordinal("early_revenue", "commercial") == (2, "early_revenue")
        assert stage_ordinal("scaling", "commercial") == (3, "scaling")
        assert stage_ordinal("profitable", "commercial") == (4, "profitable")

    def test_synonyms_normalized(self):
        assert stage_ordinal("临床", "technology") == (2, "clinical")
        assert stage_ordinal("商业化", "technology") == (3, "commercial")
        assert stage_ordinal("Pre_Revenue", "commercial") == (0, "none")
        assert stage_ordinal("放量", "commercial") == (3, "scaling")
        assert stage_ordinal("已盈利", "commercial") == (4, "profitable")

    def test_cross_axis_semantics(self):
        """同一词在不同轴语义不同：技术轴的「商业化」= 技术已商用（序 3）；
        商业轴的「商业化」= 规模化收入（scaling）；「成熟」在商业轴 = 盈利。"""
        assert stage_ordinal("商业化", "technology")[1] == "commercial"
        assert stage_ordinal("商业化", "commercial") == (3, "scaling")
        assert stage_ordinal("成熟", "commercial") == (4, "profitable")

    def test_unmappable_and_empty(self):
        assert stage_ordinal("", "technology") == (None, "")
        assert stage_ordinal(None, "commercial") == (None, "")
        assert stage_ordinal("poc", "technology") == (None, "")  # 语义含混不猜
        assert stage_ordinal("  ", "commercial") == (None, "")

    def test_orders_are_0_to_4(self):
        assert len(TECHNOLOGY_STAGE_ORDER) == 5
        assert len(COMMERCIAL_STAGE_ORDER) == 5


class TestBuildQuadrant:
    def test_points_and_unpositioned(self):
        candidates = [
            {"entity_id": "a", "name": "甲", "tier": "included",
             "technology_stage": "commercial", "commercial_stage": "scaling",
             "evidence_refs": ["ev-1", "ev-2"]},
            {"entity_id": "b", "name": "乙", "tier": "watchlist",
             "technology_stage": "clinical", "commercial_stage": "",  # 未判定 → 未定位
             "evidence_refs": []},
            {"entity_id": "c", "name": "丙", "tier": "needs_review",
             "technology_stage": "火星阶段", "commercial_stage": "pilot",  # 未知词 → 未定位
             "evidence_refs": ["ev-3"]},
        ]
        q = build_quadrant(candidates)
        assert len(q["points"]) == 1
        p = q["points"][0]
        assert p["entity_id"] == "a" and p["x"] == 3 and p["y"] == 3
        assert p["evidence_count"] == 2 and p["tier"] == "included"
        assert len(q["unpositioned"]) == 2
        b = next(u for u in q["unpositioned"] if u["entity_id"] == "b")
        assert "commercial_stage 未判定" in b["reason"]
        c = next(u for u in q["unpositioned"] if u["entity_id"] == "c")
        assert "technology_stage" in c["reason"] and "火星阶段" in c["reason"]

    def test_axes_contract(self):
        q = build_quadrant([])
        assert q["x_axis"]["key"] == "technology_stage"
        assert q["y_axis"]["key"] == "commercial_stage"
        assert q["x_axis"]["labels"]["clinical"] == "临床"
        assert q["y_axis"]["labels"]["profitable"] == "盈利"
        assert q["points"] == [] and q["unpositioned"] == []

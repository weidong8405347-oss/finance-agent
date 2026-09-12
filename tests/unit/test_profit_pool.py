"""Profit Pool 推导验收（升级方案 §22/§48.4）。

关键场景：只解析显式百分数；单一来源标 Estimated；份额合计偏离 100% 进 notes
而不归一化；区间/裸数字/金额不解析（不画视觉精确的图）；无边 → None（前端
维持定性展示）。
"""

from finance_agent.dossier.profit_pool import build_profit_pool


def _imap(edges):
    return {
        "nodes": [
            {"node_id": "compute", "label": "算力", "layer": "upstream"},
            {"node_id": "platform", "label": "平台", "layer": "midstream"},
            {"node_id": "app", "label": "应用", "layer": "downstream"},
        ],
        "edges": edges,
    }


class TestBuildProfitPool:
    def test_no_quantified_flow_returns_none(self):
        assert build_profit_pool(_imap([])) is None
        assert build_profit_pool(_imap([
            {"source": "compute", "target": "platform", "relation": "value_flow",
             "flow_known": False, "flow_value": None},
        ])) is None

    def test_percent_edges_parsed(self):
        pool = build_profit_pool(_imap([
            {"source": "compute", "target": "platform", "relation": "value_flow",
             "flow_known": True, "flow_value": "55%", "evidence_refs": ["ev-1", "ev-2"]},
            {"source": "platform", "target": "app", "relation": "value_flow",
             "flow_known": True, "flow_value": "45%", "evidence_refs": ["ev-3"]},
        ]))
        assert pool is not None
        assert pool["kind"] == "stacked_bar" and pool["unit"] == "percent"
        assert [e["share"] for e in pool["entries"]] == ["55", "45"]
        assert pool["total_share"] == "100"
        # ev-3 单源 → Estimated；ev-1+ev-2 双源 → 非 Estimated（§48.4）
        assert pool["entries"][0]["estimated"] is False
        assert pool["entries"][1]["estimated"] is True
        assert any("Estimated" in n for n in pool["notes"])

    def test_range_and_bare_number_rejected(self):
        """区间（55-65%）与裸数字/金额不解析——口径含糊不画精确图。"""
        for v in ("55-65%", "45", "1.2B USD", "约45%", "45 percent"):
            assert build_profit_pool(_imap([
                {"source": "compute", "target": "platform", "relation": "value_flow",
                 "flow_known": True, "flow_value": v},
            ])) is None, v

    def test_non_value_flow_relation_ignored(self):
        assert build_profit_pool(_imap([
            {"source": "compute", "target": "platform", "relation": "supplies",
             "flow_known": True, "flow_value": "55%"},
        ])) is None

    def test_deviation_from_100_noted_not_normalized(self):
        pool = build_profit_pool(_imap([
            {"source": "compute", "target": "platform", "relation": "value_flow",
             "flow_known": True, "flow_value": "55%", "evidence_refs": ["ev-1", "ev-2"]},
            {"source": "platform", "target": "app", "relation": "value_flow",
             "flow_known": True, "flow_value": "60%", "evidence_refs": ["ev-3", "ev-4"]},
        ]))
        assert pool["total_share"] == "115"
        assert any("115%" in n and "未做归一化" in n for n in pool["notes"])
        # 份额原样保留（不改写）
        assert [e["share"] for e in pool["entries"]] == ["55", "60"]

    def test_decimal_normalized_verbatim(self):
        pool = build_profit_pool(_imap([
            {"source": "compute", "target": "platform", "relation": "value_flow",
             "flow_known": True, "flow_value": "45.50%", "evidence_refs": ["ev-1", "ev-2"]},
        ]))
        assert pool["entries"][0]["share"] == "45.5"  # 十进制规范化去尾零

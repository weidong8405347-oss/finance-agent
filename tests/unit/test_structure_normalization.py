"""结构产物形状归一与按 kind 部分接受（profile 内容质量升级 §1）。

事故形态（live-a2cce641 synthesize，2026-09-09，事件 seq 204136-204197）：
合成器 6 次提交的结构内容完备、67 个引用全部可解析，却因形状漂移全军覆没——
bottleneck 给描述字符串、relation/status 给中文或近义词（「支撑」「pending」）、
layers 给显示名而 node.layer 是 key、cells 给数组、tiers/main_basis/credibility 给字符串。
旧契约一个字段非法 → 整 kind 拒 → 一个 kind 非法 → 整批拒 → 12 步预算烧光 →
产物冻结 structures=[]，行业页面只剩占位符与长文本。

本组测试锁死新契约：
- 归一只做确定性形状修复（同义词表/描述搬移/包装/zip），逐条留痕（repairs）；
- 语义纪律一条不放松：引用可解析、淘汰给原因、不许编造流量、不可比不硬画图；
- 按 kind 部分接受：坏的 kind 单独报错，好的 kind 照常冻结。
"""

import pytest

from finance_agent.dossier.structures import (
    StructureError,
    normalize_structures,
    parse_structures,
    parse_structures_partial,
    validate_structures,
)


def _imap(**over):
    base = {
        "nodes": [
            {"node_id": "up", "label": "上游算力", "layer": "upstream"},
            {"node_id": "mid", "label": "中游平台", "layer": "midstream"},
        ],
        "edges": [{"source": "up", "target": "mid", "relation": "supplies"}],
        "layers": ["upstream", "midstream"],
    }
    base.update(over)
    return {"industry_map": base}


class TestBottleneckCoercion:
    def test_descriptive_string_becomes_true_and_moves_to_note(self):
        """真实事故形态：bottleneck='国产芯片性能与生态差距' → true + 描述进 note（不丢信息）。"""
        raw = _imap(nodes=[
            {"node_id": "up", "label": "上游算力", "layer": "upstream",
             "bottleneck": "国产芯片性能与生态差距"},
        ])
        accepted, failures, repairs = parse_structures_partial(raw)
        assert not failures
        node = accepted["industry_map"].nodes[0]
        assert node.bottleneck is True
        assert "国产芯片性能与生态差距" in node.note
        assert any("bottleneck" in r for r in repairs)  # 修复留痕，不静默

    def test_existing_note_preserved(self):
        raw = _imap(nodes=[
            {"node_id": "up", "label": "x", "layer": "upstream",
             "bottleneck": "训练数据缺口", "note": "原有说明"},
        ])
        accepted, _, _ = parse_structures_partial(raw)
        note = accepted["industry_map"].nodes[0].note
        assert "训练数据缺口" in note and "原有说明" in note

    @pytest.mark.parametrize("word", ["无", "否", "false", "no", "", "非瓶颈"])
    def test_falsy_words_become_false(self, word):
        raw = _imap(nodes=[{"node_id": "up", "label": "x", "layer": "upstream",
                            "bottleneck": word}])
        accepted, failures, _ = parse_structures_partial(raw)
        assert not failures
        assert accepted["industry_map"].nodes[0].bottleneck is False

    def test_bool_untouched(self):
        raw = _imap(nodes=[{"node_id": "up", "label": "x", "layer": "upstream",
                            "bottleneck": True}])
        accepted, _, repairs = parse_structures_partial(raw)
        assert accepted["industry_map"].nodes[0].bottleneck is True
        assert not any("bottleneck" in r for r in repairs)


class TestRelationNormalization:
    @pytest.mark.parametrize("label,canon", [
        ("支撑", "enables"), ("支持", "enables"), ("赋能", "enables"),
        ("供给", "supplies"), ("采购", "supplies"), ("竞争", "competes"),
        ("替代", "substitutes"), ("依赖", "depends_on"), ("价值捕获", "value_flow"),
    ])
    def test_chinese_synonyms_mapped(self, label, canon):
        raw = _imap(edges=[{"source": "up", "target": "mid", "relation": label}])
        accepted, failures, repairs = parse_structures_partial(raw)
        assert not failures
        assert accepted["industry_map"].edges[0].relation == canon
        assert any("relation" in r for r in repairs)

    def test_unknown_label_passes_through_not_rejected(self):
        """未收录的关系保留原文（前端如实显示）——不硬译、不整批拒。"""
        raw = _imap(edges=[{"source": "up", "target": "mid", "relation": "数据回流"}])
        accepted, failures, _ = parse_structures_partial(raw)
        assert not failures
        assert accepted["industry_map"].edges[0].relation == "数据回流"


class TestLayerNormalization:
    def test_display_names_mapped_to_keys_with_labels(self):
        """真实事故形态：layers=显示名、node.layer=key → 前端空列。归一对齐并保留显示名。"""
        raw = _imap(
            nodes=[
                {"node_id": "up", "label": "算力", "layer": "upstream"},
                {"node_id": "mid", "label": "平台", "layer": "midstream"},
                {"node_id": "dmd", "label": "科研终端", "layer": "demand"},
            ],
            edges=[],
            layers=["上游算力与基础设施", "中游模型与平台", "学术与政府科研终端"],
        )
        accepted, failures, repairs = parse_structures_partial(raw)
        assert not failures
        imap = accepted["industry_map"]
        assert imap.layers == ["upstream", "midstream", "demand"]
        assert imap.layer_labels["upstream"] == "上游算力与基础设施"
        assert imap.layer_labels["demand"] == "学术与政府科研终端"
        assert any("layers" in r for r in repairs)

    def test_missing_node_layers_appended(self):
        """节点用了 layers 未列的层 → 补齐（否则该列节点在前端不可见）。"""
        raw = _imap(layers=["upstream"])
        accepted, _, repairs = parse_structures_partial(raw)
        assert accepted["industry_map"].layers == ["upstream", "midstream"]
        assert any("补齐" in r for r in repairs)

    def test_chinese_node_layer_canonicalized(self):
        raw = _imap(
            nodes=[{"node_id": "up", "label": "算力", "layer": "上游算力"}],
            edges=[], layers=["上游算力"],
        )
        accepted, _, _ = parse_structures_partial(raw)
        assert accepted["industry_map"].nodes[0].layer == "upstream"


class TestListDictCoercion:
    def test_routes_strings_become_dicts(self):
        raw = _imap(routes=["算力→平台→应用", "政策→终端"])
        accepted, failures, _ = parse_structures_partial(raw)
        assert not failures
        assert accepted["industry_map"].routes == [
            {"route": "算力→平台→应用"}, {"route": "政策→终端"}]

    def test_cells_list_zipped_with_column_ids(self):
        """真实事故形态：cells 给数组。与列同长时按列序 zip（确定性，非猜测）。"""
        raw = {"comparison_matrix": {
            "columns": [{"id": "revenue", "label": "收入"}, {"id": "yoy", "label": "增速"}],
            "rows": [{"label": "A 公司", "cells": ["106303", "287.2"],
                      "observation_ids": ["obs-1", "obs-2"]}],
        }}
        accepted, failures, _ = parse_structures_partial(raw)
        assert not failures
        row = accepted["comparison_matrix"].rows[0]
        assert row.cells == {"revenue": "106303", "yoy": "287.2"}
        assert row.observation_ids == {"revenue": "obs-1", "yoy": "obs-2"}

    def test_cells_length_mismatch_still_rejected(self):
        """长度对不上不硬 zip——按 kind 拒绝并给出可读原因。"""
        raw = {"comparison_matrix": {
            "columns": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
            "rows": [{"label": "X", "cells": ["1", "2", "3"]}],
        }}
        _, failures, _ = parse_structures_partial(raw)
        assert "comparison_matrix" in failures

    def test_chartable_downgraded_with_incomparable_row(self):
        raw = {"comparison_matrix": {
            "columns": [{"id": "a", "label": "A"}],
            "rows": [
                {"label": "X", "cells": {"a": "1"}, "comparable": True},
                {"label": "Y", "cells": {"a": "2"}, "comparable": False,
                 "incomparable_reason": "FY vs H1"},
            ],
            "chartable": True,
        }}
        accepted, failures, repairs = parse_structures_partial(raw)
        assert not failures
        assert accepted["comparison_matrix"].chartable is False
        assert any("chartable" in r for r in repairs)
        assert validate_structures(accepted) == []  # 降级后不再触发「不许硬画图」


class TestExecutiveSummaryCoercion:
    def test_tiers_string_split_by_enumeration_comma(self):
        raw = {"executive_summary": {
            "answer": "结论",
            "tiers": {"included": "晶泰控股、英矽智能", "excluded": "Isomorphic Labs"},
        }}
        accepted, failures, repairs = parse_structures_partial(raw)
        assert not failures
        assert accepted["executive_summary"].tiers["included"] == ["晶泰控股", "英矽智能"]
        assert accepted["executive_summary"].tiers["excluded"] == ["Isomorphic Labs"]
        assert any("tiers" in r for r in repairs)

    def test_main_basis_string_wrapped(self):
        raw = {"executive_summary": {"answer": "结论", "main_basis": "obs-1（$6M 成本）"}}
        accepted, _, _ = parse_structures_partial(raw)
        assert accepted["executive_summary"].main_basis == ["obs-1（$6M 成本）"]

    def test_credibility_string_becomes_dict(self):
        raw = {"executive_summary": {"answer": "结论", "credibility": "部分可信：价值链已核"}}
        accepted, _, _ = parse_structures_partial(raw)
        assert accepted["executive_summary"].credibility == {"总体": "部分可信：价值链已核"}

    def test_tear_sheet_fields_accepted(self):
        raw = {"executive_summary": {
            "answer": "结论", "stage": "商业兑现早期",
            "why_now": "大药企采纳加速", "value_capture": "平台层捕获 55-65%",
            "thesis_breakers": "首个 AI 药物 III 期失败",
        }}
        accepted, failures, _ = parse_structures_partial(raw)
        assert not failures
        es = accepted["executive_summary"]
        assert es.stage == "商业兑现早期"
        assert es.why_now == ["大药企采纳加速"]  # 字符串 → 单元素列表
        assert es.thesis_breakers == ["首个 AI 药物 III 期失败"]


class TestTimelineAndCandidateCoercion:
    def test_pending_status_mapped_to_expected(self):
        raw = {"validation_timeline": {"items": [
            {"event": "III 期读出", "status": "pending",
             "window_start": "2029-01-01", "window_end": "2029-12-31",
             "trigger_condition": "52 周主要终点公布"},
        ]}}
        accepted, failures, repairs = parse_structures_partial(raw)
        assert not failures
        assert accepted["validation_timeline"].items[0].status == "expected"
        assert any("status" in r for r in repairs)

    def test_candidate_enum_synonyms_and_id_strip(self):
        raw = {"candidate_assessment": {"candidates": [
            {"entity_id": " XtalPi ", "name": "晶泰", "listing_status": "已上市",
             "market": "HK", "tier": "入选", "investable": "是"},
        ]}}
        accepted, failures, _ = parse_structures_partial(raw)
        assert not failures
        c = accepted["candidate_assessment"].candidates[0]
        assert c.entity_id == "XtalPi"
        assert c.listing_status == "listed"
        assert c.tier == "included"
        assert c.investable is True

    def test_evidence_string_wrapped(self):
        raw = {"candidate_assessment": {"candidates": [
            {"entity_id": "X", "tier": "included", "moat_evidence": "ev-1"},
        ]}}
        accepted, _, _ = parse_structures_partial(raw)
        assert accepted["candidate_assessment"].candidates[0].moat_evidence == ["ev-1"]


class TestPartialAcceptance:
    def test_shape_bad_kind_does_not_sink_good_kinds(self):
        """核心回归（parse 层）：一个 kind 形状非法不再拖死整批（旧行为 = 内容全丢）。"""
        raw = {
            "validation_timeline": {"items": "不是列表"},   # 形状非法 → 单独拒
            "executive_summary": {"answer": "结论"},          # 完好 → 收
        }
        accepted, failures, _ = parse_structures_partial(raw)
        assert "executive_summary" in accepted
        assert "validation_timeline" in failures

    def test_semantic_bad_kind_attributable_by_prefix(self):
        """语义层（validate）：issue 前缀即 kind，提交工具据此按 kind 归组拒绝。"""
        raw = {
            "industry_map": {"nodes": [{"node_id": "a", "label": "A"}],
                             "edges": [{"source": "a", "target": "ghost"}],
                             "layers": ["upstream"]},   # 悬空边
            "executive_summary": {"answer": "结论"},
        }
        accepted, failures, _ = parse_structures_partial(raw)
        assert not failures  # 形状都合法
        issues = validate_structures(accepted)
        bad_kinds = {i.split(":", 1)[0].strip() for i in issues}
        assert bad_kinds == {"industry_map"}  # 好 kind 不被连带

    def test_unknown_kind_isolated(self):
        accepted, failures, _ = parse_structures_partial(
            {"sankey_chart": {"nodes": []}, "executive_summary": {"answer": "x"}}
        )
        assert "sankey_chart" in failures
        assert "executive_summary" in accepted

    def test_strict_parse_still_raises(self):
        """兼容旧契约：parse_structures 任一 kind 非法即抛（调用方选择宽松入口）。"""
        with pytest.raises(StructureError, match="未知结构产物"):
            parse_structures({"sankey_chart": {}})


class TestSemanticDisciplineUnchanged:
    """归一放宽的只是形状；语义纪律一条不减。"""

    def test_fabricated_flow_still_rejected(self):
        raw = _imap(edges=[{"source": "up", "target": "mid",
                            "flow_known": False, "flow_value": "obs-1"}])
        accepted, _, _ = parse_structures_partial(raw)
        assert any("flow_known" in i for i in validate_structures(accepted))

    def test_unresolvable_ref_still_rejected(self):
        raw = _imap(nodes=[{"node_id": "up", "label": "x", "layer": "upstream",
                            "evidence_refs": ["ev-ghost"]}])
        accepted, _, _ = parse_structures_partial(raw)
        issues = validate_structures(accepted, resolvable=lambda r: r != "ev-ghost")
        assert any("引用不可解析" in i for i in issues)

    def test_excluded_without_reason_still_rejected(self):
        raw = {"candidate_assessment": {"candidates": [
            {"entity_id": "X", "tier": "淘汰"},  # 同义词归一后仍是「淘汰无原因」
        ]}}
        accepted, _, _ = parse_structures_partial(raw)
        assert accepted["candidate_assessment"].candidates[0].tier == "excluded"
        assert any("淘汰必须给原因" in i for i in validate_structures(accepted))

    def test_expected_event_without_window_still_rejected(self):
        raw = {"validation_timeline": {"items": [
            {"event": "读出", "status": "pending", "trigger_condition": "x"},
        ]}}
        accepted, _, _ = parse_structures_partial(raw)
        assert any("缺时间范围" in i for i in validate_structures(accepted))


class TestNormalizeIdempotent:
    def test_canonical_payload_untouched(self):
        """规范输入零修复（归一不引入噪声）。"""
        raw = _imap()
        normalized, repairs = normalize_structures(raw)
        assert repairs == []
        assert normalized == raw

    def test_double_normalize_stable(self):
        raw = _imap(
            nodes=[{"node_id": "up", "label": "x", "layer": "上游算力",
                    "bottleneck": "训练数据缺口"}],
            edges=[{"source": "up", "target": "mid", "relation": "支撑"}],
            layers=["上游算力"],
            routes=["算力→平台"],
        )
        once, _ = normalize_structures(raw)
        twice, repairs2 = normalize_structures(once)
        assert twice == once
        assert repairs2 == []  # 第二次无新修复（幂等）


class TestRichCellUnpacking:
    """富单元格（dict 形态）拆包（2026-09-12 事故：str(dict) 吞成 repr 字符串，
    表格渲出 Python  repr 且 obs 引用困死在文本里）。"""

    def test_dict_cell_unpacked_value_note_observation(self):
        raw = {"comparison_matrix": {
            "columns": [{"id": "c1", "label": "FY25"}, {"id": "c2", "label": "FY26H1"}],
            "rows": [{
                "label": "英矽智能 总收入",
                "cells": {
                    "c1": {"value": "27456", "note": "HKEXnews 口径",
                           "observation_id": "obs-aaa111", "unit": "USD 千"},
                    "c2": {"value": "106303", "note": "同比 +287.2%",
                           "observation_id": "obs-bbb222"},
                },
                "observation_ids": {},
            }],
        }}
        accepted, failures, repairs = parse_structures_partial(raw)
        assert not failures
        row = accepted["comparison_matrix"].rows[0]
        # value+note 合成文本，observation_id 归位（不困在文本里）
        assert row.cells["c1"] == "27456（HKEXnews 口径）"
        assert row.cells["c2"] == "106303（同比 +287.2%）"
        assert row.observation_ids == {"c1": "obs-aaa111", "c2": "obs-bbb222"}
        assert any("富单元格拆包" in r for r in repairs)

    def test_plain_cells_untouched(self):
        raw = {"comparison_matrix": {
            "columns": [{"id": "c1"}],
            "rows": [{"label": "r", "cells": {"c1": "100"}, "observation_ids": {}}],
        }}
        accepted, failures, _ = parse_structures_partial(raw)
        assert not failures
        assert accepted["comparison_matrix"].rows[0].cells == {"c1": "100"}

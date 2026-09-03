"""verify 准入闸 + 实体 ID 归一化 + 重复档案合并的契约测试。

对应验收事故：「完整度 100% 但点进去没内容」「2228.HK 与 02228.HK 双档案」。
"""

from datetime import UTC, datetime, timedelta

import pytest

from finance_agent.eventstore.store import EventStore
from finance_agent.harness.manifest import RunManifest, RunMode
from finance_agent.knowledge.errors import KnowledgeQualityError
from finance_agent.knowledge.migrate import merge_entity
from finance_agent.knowledge.models import Evidence, Fact, PitGrade
from finance_agent.knowledge.normalize import normalize_entity_id
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.knowledge.verify import (
    field_issues,
    verify_entity,
)
from finance_agent.knowledge.writer import ProfileWriter

NOW = datetime(2026, 9, 3, tzinfo=UTC)
FRESH = NOW - timedelta(days=10)  # 所有 schema 新鲜度窗口内


def _manifest(run_id: str = "t") -> RunManifest:
    return RunManifest(run_id=run_id, mode=RunMode.LIVE)


def _seed_evidence(kb: BitemporalStore, eid: str = "ev-1", pit: PitGrade = PitGrade.A) -> None:
    kb.add_evidence(
        Evidence(
            evidence_id=eid, source_id="edgar", url="https://sec.gov/x",
            verbatim_quote="Total revenue was 1,473,856 thousand in fiscal 2025",
            retrieved_at=NOW, available_at=FRESH, pit_grade=pit,
        )
    )


def _write(kb: BitemporalStore, field: str, value, *, eid: str = "ev-1",
           entity_id: str = "BE", kind: str = "stock", kt=FRESH) -> str:
    return ProfileWriter(store=kb).write_fact(
        Fact(entity_kind=kind, entity_id=entity_id, field=field, value=value,
             knowledge_time=kt, evidence_ids=[eid], run_id="t"),
        run=_manifest(),
    )


# ---------------- 写侧硬门禁 ----------------


def test_gate_rejects_empty_and_placeholder_values(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    _seed_evidence(kb)
    for bad in (None, "", "   ", "待补充", "TBD", "未知", [], {}):
        with pytest.raises(KnowledgeQualityError):
            _write(kb, "moat", bad)


def test_gate_rejects_serialized_json_string(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    _seed_evidence(kb)
    with pytest.raises(KnowledgeQualityError, match="JSON 字符串"):
        _write(kb, "value_chain", '{"layers": [{"name": "上游"}]}')
    # 散文里提到 JSON 语义无妨——只有「整体就是一段 JSON」才拒
    _write(kb, "moat", "竞争壁垒来自网络效应，不是简单的 {key: value} 结构所能描述")


def test_gate_rejects_structured_field_type_violation(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    _seed_evidence(kb)
    with pytest.raises(KnowledgeQualityError, match="list"):
        _write(kb, "player_landscape", "晶泰控股、英矽智能")  # 字符串 → 拒
    with pytest.raises(KnowledgeQualityError, match="必备键"):
        _write(kb, "player_landscape", [{"name": "晶泰控股"}])  # 缺 ticker/evidence_ids
    _write(kb, "player_landscape", [{"ticker": "2228.HK", "evidence_ids": ["ev-1"]}])


def test_gate_verdict_event_emitted_on_rejection(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    events = EventStore(tmp_path / "e.db")
    _seed_evidence(kb)
    writer = ProfileWriter(store=kb, events=events)
    with pytest.raises(KnowledgeQualityError):
        writer.write_fact(
            Fact(entity_kind="stock", entity_id="BE", field="moat", value="",
                 knowledge_time=FRESH, evidence_ids=["ev-1"], run_id="t"),
            run=_manifest(),
        )
    verdicts = [e for e in events.read("t") if e.type == "hook/verdict"]
    assert verdicts and verdicts[0].payload["hook"] == "verify-gate"
    assert verdicts[0].payload["verdict"] == "rejected"


# ---------------- 读侧软检查与实体质量投影 ----------------


def test_field_issues_flags_thin_prose_and_missing_number(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    _seed_evidence(kb)
    _write(kb, "moat", "生态锁定")  # 散文 < 12 字符 → 内容过短
    rec = kb.view("stock", "BE", NOW)["moat"]
    assert "内容过短" in field_issues("moat", rec, [kb.get_evidence("ev-1")])

    _write(kb, "valuation", "估值偏高但暂无具体数据支撑")  # 数值锚点字段没数字
    rec = kb.view("stock", "BE", NOW)["valuation"]
    assert "缺少数值锚点" in field_issues("valuation", rec, [kb.get_evidence("ev-1")])


def test_field_issues_flags_c_grade_only_evidence(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    kb.add_evidence(
        Evidence(evidence_id="ev-c", source_id="web", verbatim_quote="网络搜索快照",
                 retrieved_at=NOW, pit_grade=PitGrade.C)
    )
    _write(kb, "moat", "机器人实验室覆盖大部分常见药化反应类型", eid="ev-c", kt=NOW)
    rec = kb.view("stock", "BE", NOW)["moat"]
    assert any("C 级证据" in i for i in field_issues("moat", rec, [kb.get_evidence("ev-c")]))


def test_verify_entity_status_and_score(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    _seed_evidence(kb)
    q0 = verify_entity(kb, "stock", "BE", NOW)
    assert q0.status == "draft" and q0.quality_score == 0.0
    assert any("缺失" in i or "尚无事实" in i for i in q0.issues)

    # 填满全部必填字段（长文 + A 级证据）→ verified
    prose = "这是一段足够长的定性分析文本，覆盖了该字段应有的实质内容。"
    for f in ("business_model", "moat", "risks", "peers"):
        _write(kb, f, prose)
    for f in ("revenue_fy", "net_income_fy", "cash_flow", "valuation"):
        _write(kb, f, "2025 财年对应数值为 1,473,856 thousand（证据原文口径）")
    q1 = verify_entity(kb, "stock", "BE", NOW)
    assert q1.status == "verified" and q1.quality_score == 1.0
    assert all(f.status == "ok" for f in q1.fields if f.required)


def test_verify_entity_conflict_downgrades(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    _seed_evidence(kb)
    kb.add_evidence(
        Evidence(evidence_id="ev-2", source_id="edgar",
                 verbatim_quote="revenue was 9,999 thousand",
                 retrieved_at=NOW, available_at=FRESH, pit_grade=PitGrade.A)
    )
    _write(kb, "revenue_fy", "1,473,856 thousand")
    _write(kb, "revenue_fy", "9,999 thousand", eid="ev-2")  # 同 event_time 不同值 → 冲突
    q = verify_entity(kb, "stock", "BE", NOW)
    rev = next(f for f in q.fields if f.field == "revenue_fy")
    assert rev.status == "conflict" and q.status == "draft"
    assert any("冲突" in i for i in q.issues)


# ---------------- 实体 ID 归一化 ----------------


def test_normalize_entity_id():
    assert normalize_entity_id("stock", "02228.HK") == "2228.HK"
    assert normalize_entity_id("stock", "700.HK") == "0700.HK"
    assert normalize_entity_id("stock", "2228.HK") == "2228.HK"  # 幂等
    assert normalize_entity_id("stock", "be") == "BE"
    assert normalize_entity_id("stock", "002837.SZ") == "002837.SZ"
    assert normalize_entity_id("industry", "  AI-For-Science ") == "ai-for-science"


def test_writer_normalizes_entity_id(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    _seed_evidence(kb)
    _write(kb, "moat", "足够长的护城河分析文本内容", entity_id="02228.HK")
    assert kb.view("stock", "2228.HK", NOW)  # 归一后落在规范 id 上
    assert not kb.view("stock", "02228.HK", NOW)


# ---------------- 重复档案合并 ----------------


def test_merge_entity_refuses_self_merge(tmp_path):
    """2026-09-03 事故防护：from_id == to_id（归一化误伤源 id）必须 fail-closed。"""
    kb = BitemporalStore(tmp_path / "kb.db")
    _seed_evidence(kb)
    _write(kb, "moat", "足够长的护城河分析文本内容", entity_id="2228.HK")
    with pytest.raises(ValueError, match="相同"):
        merge_entity(kb, "stock", "2228.HK", "2228.HK")
    assert kb.view("stock", "2228.HK", NOW)  # 档案毫发无损


def test_merge_entity_replays_and_deletes(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    _seed_evidence(kb)
    kb.add_evidence(
        Evidence(evidence_id="ev-2", source_id="edgar",
                 verbatim_quote="净亏损 15.149 亿元",
                 retrieved_at=NOW, available_at=FRESH - timedelta(days=365),
                 pit_grade=PitGrade.A)
    )
    _write(kb, "moat", "主档案的护城河分析内容", entity_id="2228.HK")
    _write(kb, "revenue_fy", "FY2025 收入 8.03 亿元", entity_id="2228.HK")
    # 存量遗留数据：归一化上线前写入的非规范 id（直插 store，绕过 writer 归一）
    kb.assert_fact(
        Fact(entity_kind="stock", entity_id="02228.HK", field="revenue_fy",
             value="FY2024 收入 2.664 亿元", knowledge_time=FRESH - timedelta(days=365),
             evidence_ids=["ev-2"], run_id="legacy")
    )

    report = merge_entity(kb, "stock", "02228.HK", "2228.HK")
    assert report["merged"] == 1 and report["deleted"] == 1
    view = kb.view("stock", "2228.HK", NOW)
    assert "8.03" in view["revenue_fy"].value  # 最新版本不变
    history = kb.history("stock", "2228.HK", "revenue_fy")
    assert len(history) == 2  # 旧档案的版本链并入
    assert not kb.view("stock", "02228.HK", NOW)  # 源实体已清除

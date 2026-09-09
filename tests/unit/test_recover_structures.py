"""结构产物回收脚本验收（profile 内容质量升级 §2）。

纪律：dry-run 只读；--apply 才写库且落审计事件；语义校验不过的 kind 保持拒绝
（不靠放松校验通过）；幂等（重跑无变化）；回收后重开快照（页面刷新即见）。
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from recover_structures import main  # noqa: E402

from finance_agent.eventstore.events import Event  # noqa: E402
from finance_agent.eventstore.store import EventStore  # noqa: E402
from finance_agent.knowledge.metric_store import MetricStore  # noqa: E402
from finance_agent.knowledge.models import Evidence, PitGrade  # noqa: E402
from finance_agent.knowledge.store import BitemporalStore  # noqa: E402

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)  # 过去时刻：artifacts_as_of(now) 可见

#: 真实事故形态：bottleneck 描述字符串 + relation 中文 + status pending + layers 显示名
DRIFTED = {
    "industry_map": {
        "nodes": [
            {"node_id": "up", "label": "上游算力", "layer": "upstream",
             "bottleneck": "国产芯片性能与生态差距", "evidence_refs": ["ev-1"]},
            {"node_id": "mid", "label": "中游平台", "layer": "midstream"},
        ],
        "edges": [{"source": "up", "target": "mid", "relation": "支撑"}],
        "layers": ["上游算力与基础设施", "中游模型与平台"],
        "routes": ["算力→平台"],
    },
    "validation_timeline": {
        "items": [{"event": "III 期读出", "status": "pending",
                   "window_start": "2029-01-01", "window_end": "2029-12-31",
                   "trigger_condition": "主要终点公布", "evidence_refs": ["ev-1"]}],
    },
    "executive_summary": {
        "answer": "平台层最先兑现", "tiers": {"included": "晶泰、英矽"},
        "main_basis": "分部收入放量", "credibility": "部分可信",
        "refs": ["ev-1"],
    },
}


@pytest.fixture()
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    kb = BitemporalStore(d / "kb.db")
    kb.add_evidence(Evidence(
        evidence_id="ev-1", source_id="web_search", url="https://x.com/a",
        verbatim_quote="平台收入放量", retrieved_at=NOW, available_at=NOW,
        pit_grade=PitGrade.B,
    ))
    metrics = MetricStore(d / "metrics.db")
    metrics.save_artifact(artifact_id="art-1", namespace="prod", payload={
        "artifact_id": "art-1", "entity_kind": "industry", "entity_id": "ai-x",
        "title": "研究报告", "status": "validated", "sufficiency": "partial",
        "purpose": "report", "created_at": NOW.isoformat(),
        "run_id": "live-s1--cmd-1-2-synthesize", "structures": {},
        "report_document": {"title": "t", "entity_kind": "industry",
                            "entity_id": "ai-x", "blocks": [], "limitations": []},
    })
    events = EventStore(d / "events.db")
    events.append(Event(
        run_id="live-s1--cmd-1-2-synthesize", type="tool/call",
        payload={"call_id": "submit_structures_9", "name": "submit_structures",
                 "arguments": {"structures": DRIFTED}},
    ))
    for store in (kb, metrics, events):
        store.close()
    return d


def _artifact(d):
    m = MetricStore(d / "metrics.db")
    a = m.get_artifact("art-1")
    m.close()
    return a


def _events_of(d, type_):
    e = EventStore(d / "events.db")
    rows = e._conn.execute(  # noqa: SLF001 - 测试断言只读
        "SELECT payload FROM events WHERE type = ?", (type_,)).fetchall()
    e.close()
    return [json.loads(r[0]) for r in rows]


def test_dry_run_reads_without_writing(data_dir, capsys):
    rc = main(["--data-dir", str(data_dir), "--entity", "industry:ai-x"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "可回收" in out and "DRY-RUN" in out
    assert _artifact(data_dir)["structures"] == {}  # 未写库
    assert not _events_of(data_dir, "research/structures_recovered")


def test_apply_recovers_normalizes_and_audits(data_dir, capsys):
    rc = main(["--data-dir", str(data_dir), "--entity", "industry:ai-x", "--apply"])
    assert rc == 0
    art = _artifact(data_dir)
    st = art["structures"]
    assert sorted(st) == ["executive_summary", "industry_map", "validation_timeline"]
    # 归一生效：bottleneck 描述搬进 note、relation 归一、layers 对齐、显示名保留
    imap = st["industry_map"]
    assert imap["nodes"][0]["bottleneck"] is True
    assert "国产芯片性能与生态差距" in imap["nodes"][0]["note"]
    assert imap["edges"][0]["relation"] == "enables"
    assert imap["layers"] == ["upstream", "midstream"]
    assert imap["layer_labels"]["upstream"] == "上游算力与基础设施"
    assert st["validation_timeline"]["items"][0]["status"] == "expected"
    assert st["executive_summary"]["tiers"]["included"] == ["晶泰", "英矽"]
    # 产物限制区披露回收来源（诚实：不是原始发布内容）
    assert any("recover_structures" in x for x in art["report_document"]["limitations"])
    # 审计事件：来源 seq/kind/修复清单可回溯
    evs = _events_of(data_dir, "research/structures_recovered")
    assert len(evs) == 1
    assert evs[0]["entity"] == "industry:ai-x"
    assert sorted(evs[0]["kinds"]) == ["executive_summary", "industry_map",
                                       "validation_timeline"]
    assert evs[0]["provenance"]["industry_map"]["run_id"] == "live-s1--cmd-1-2-synthesize"
    assert any("bottleneck" in r for r in evs[0]["repairs"])
    # 重开快照：structures 进投影（页面刷新即见）
    out = capsys.readouterr().out
    assert "已写入并重开快照 dossier-ai-x-" in out


def test_apply_is_idempotent(data_dir, capsys):
    main(["--data-dir", str(data_dir), "--entity", "industry:ai-x", "--apply"])
    capsys.readouterr()
    main(["--data-dir", str(data_dir), "--entity", "industry:ai-x", "--apply"])
    out = capsys.readouterr().out
    assert "无可回收" in out
    assert len(_events_of(data_dir, "research/structures_recovered")) == 1  # 不重复落事件


def test_never_valid_kind_stays_rejected(data_dir, capsys):
    """语义纪律不因回收放松：从未通过校验的 kind 保持拒绝，其余照常回收。"""
    e = EventStore(data_dir / "events.db")
    e.append(Event(
        run_id="live-s1--cmd-1-2-synthesize", type="tool/call",
        payload={"call_id": "submit_structures_10", "name": "submit_structures",
                 "arguments": {"structures": {"comparison_matrix": {
                     "columns": [{"id": "c1", "label": "收入"}],
                     "rows": [{"label": "A", "cells": {"c1": "100"},
                               "observation_ids": {"c1": "obs-ghost"}}],
                 }}}},
    ))
    e.close()
    rc = main(["--data-dir", str(data_dir), "--entity", "industry:ai-x", "--apply"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "仍被拒" in out and "引用不可解析" in out
    st = _artifact(data_dir)["structures"]
    assert "comparison_matrix" not in st          # 幽灵引用 → 拒
    assert "industry_map" in st                    # 其余 kind 照常回收
    evs = _events_of(data_dir, "research/structures_recovered")
    assert "comparison_matrix" in evs[0]["rejected"]


def test_later_ghost_does_not_overwrite_earlier_valid(data_dir):
    """同 kind 后续提交非法时，保留早先通过的版本（不被幽灵引用覆盖）。"""
    drifted = json.loads(json.dumps(DRIFTED))
    drifted["executive_summary"]["refs"] = ["ev-ghost"]
    e = EventStore(data_dir / "events.db")
    e.append(Event(
        run_id="live-s1--cmd-1-2-synthesize", type="tool/call",
        payload={"call_id": "submit_structures_11", "name": "submit_structures",
                 "arguments": {"structures": drifted}},
    ))
    e.close()
    main(["--data-dir", str(data_dir), "--entity", "industry:ai-x", "--apply"])
    st = _artifact(data_dir)["structures"]
    assert st["executive_summary"]["refs"] == ["ev-1"]  # 早先合法版本胜出
    assert "ev-ghost" not in json.dumps(st, ensure_ascii=False)


def test_entity_without_submissions_skipped(data_dir, capsys):
    rc = main(["--data-dir", str(data_dir), "--entity", "industry:nonexistent"])
    assert rc == 0
    assert "可回收" not in capsys.readouterr().out

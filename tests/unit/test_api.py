"""API 投影层契约：UI 是 EventStore/知识库的纯投影，每个字段可回指（DESIGN.md §7）。

端点覆盖五个一级页面：Sessions / Knowledge（含 as_of 时光机）/ Decisions / Evaluations。
"""

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from finance_agent.api.app import create_app
from finance_agent.decision.card import (
    Action,
    DecisionCard,
    Horizon,
    Position,
    Subject,
)
from finance_agent.decision.store import DecisionStore
from finance_agent.eventstore.events import Event
from finance_agent.eventstore.store import EventStore
from finance_agent.harness.manifest import RunMode
from finance_agent.knowledge.models import Evidence, Fact, PitGrade
from finance_agent.knowledge.snapshot import kb_snapshot_id
from finance_agent.knowledge.store import BitemporalStore

NOW = datetime(2024, 6, 1, tzinfo=UTC)
OLD = datetime(2023, 3, 1, tzinfo=UTC)


def seeded(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    events = EventStore(tmp_path / "events.db")
    decisions = DecisionStore(tmp_path / "decisions.db")
    kb.add_evidence(
        Evidence(
            evidence_id="ev-1",
            source_id="edgar",
            url="https://sec.gov/x",
            verbatim_quote="Total revenue 100",
            retrieved_at=NOW,
            available_at=OLD,
            pit_grade=PitGrade.A,
        )
    )
    kb.assert_fact(
        Fact(
            entity_kind="stock", entity_id="AAPL", field="revenue_fy", value=100,
            knowledge_time=OLD, evidence_ids=["ev-1"], run_id="run-1",
        )
    )
    events.append(Event(run_id="run-1", type="turn/start"))
    events.append(Event(run_id="run-1", type="user/message", payload={"content": "研究 AAPL"}))
    events.append(Event(run_id="run-1", type="fact/asserted", payload={"field": "revenue_fy"}))
    decisions.insert(
        DecisionCard(
            card_id="card-1",
            run_id="run-1",
            mode=RunMode.LIVE,
            subject=Subject(kind="stock", id="AAPL"),
            action=Action.BUY,
            conviction=4,
            horizon=Horizon.M6,
            rationale=["ev-1"],
            thesis_points=["revenue_fy"],
            invalidation=["营收失效条件"],
            position=Position(sizing_pct=0.1, max_loss_pct=0.05),
            kb_snapshot_id=kb_snapshot_id(kb, [("stock", "AAPL")], NOW),
            created_at=NOW,
        )
    )
    return kb, events, decisions


def make_client(tmp_path):
    kb, events, decisions = seeded(tmp_path)
    app = create_app(kb=kb, events=events, decisions=decisions, evals_dir=tmp_path / "evals")
    return TestClient(app)


def conflict_seeded(tmp_path):
    """同一 event_time 的两个竞争版本（100 vs 200）→ revenue_fy 处于开放冲突。"""
    kb = BitemporalStore(tmp_path / "kb.db")
    events = EventStore(tmp_path / "events.db")
    decisions = DecisionStore(tmp_path / "decisions.db")
    for eid, quote in (("ev-1", "Total revenue 100"), ("ev-2", "Total revenue 200")):
        kb.add_evidence(
            Evidence(
                evidence_id=eid,
                source_id="edgar",
                url=f"https://sec.gov/{eid}",
                verbatim_quote=quote,
                retrieved_at=NOW,
                available_at=OLD,
                pit_grade=PitGrade.A,
            )
        )
    for value, eid, known in ((100, "ev-1", OLD), (200, "ev-2", NOW)):
        kb.assert_fact(
            Fact(
                entity_kind="stock", entity_id="AAPL", field="revenue_fy", value=value,
                event_time=datetime(2023, 12, 31, tzinfo=UTC),
                knowledge_time=known, evidence_ids=[eid], run_id="run-1",
            )
        )
    app = create_app(kb=kb, events=events, decisions=decisions, evals_dir=tmp_path / "evals")
    return TestClient(app), kb, events


def test_sessions_list_and_events(tmp_path):
    client = make_client(tmp_path)
    runs = client.get("/api/sessions").json()
    assert runs == [
        {
            "run_id": "run-1",
            "title": None,
            "started_at": runs[0]["started_at"],
            "last_active": runs[0]["last_active"],
            "status": "running",
            "status_detail": None,
        }
    ]

    timeline = client.get("/api/sessions/run-1/events").json()
    assert [e["type"] for e in timeline] == ["turn/start", "user/message", "fact/asserted"]


def test_knowledge_entities_and_as_of_time_machine(tmp_path):
    client = make_client(tmp_path)
    entities = client.get("/api/knowledge/entities").json()
    assert len(entities) == 1
    e0 = entities[0]
    assert e0["kind"] == "stock" and e0["id"] == "AAPL" and e0["field_count"] == 1
    # 列表投影带档案健康度（完整度/陈旧/冲突/最近可知时刻）
    assert 0.0 <= e0["completeness"] <= 1.0 and e0["last_knowledge_time"].startswith("2023-03-01")
    assert "stale_count" in e0 and "conflict_count" in e0

    # 时光机：事实 knowledge_time=2023-03-01 → 之前不可见，之后可见
    before = client.get(
        "/api/knowledge/stock/AAPL", params={"as_of": "2022-06-01T00:00:00+00:00"}
    ).json()
    assert before["facts"] == {}
    after = client.get(
        "/api/knowledge/stock/AAPL", params={"as_of": "2024-01-01T00:00:00+00:00"}
    ).json()
    assert after["facts"]["revenue_fy"]["value"] == 100
    # 证据可回指
    assert after["facts"]["revenue_fy"]["evidence"][0]["verbatim_quote"] == "Total revenue 100"


def test_decisions_list(tmp_path):
    client = make_client(tmp_path)
    cards = client.get("/api/decisions").json()
    assert len(cards) == 1
    assert cards[0]["card_id"] == "card-1"
    assert cards[0]["action"] == "buy"
    assert cards[0]["kb_snapshot_id"].startswith("sha256:")


def test_evaluations_empty_dir(tmp_path):
    client = make_client(tmp_path)
    assert client.get("/api/evaluations").json() == []


# ---------------- 冲突人工裁决（backlog #2：详情页「以此版本为准」→ 版本链落裁决事件） ----------------


def test_resolve_conflict_endpoint_keeps_latest(tmp_path):
    client, _kb, events = conflict_seeded(tmp_path)
    facts = client.get("/api/knowledge/stock/AAPL").json()["facts"]
    assert facts["revenue_fy"]["conflict"] is True
    keep = facts["revenue_fy"]["fact_id"]  # 投影带 fact_id（裁决锚点）

    resp = client.post(
        "/api/knowledge/stock/AAPL/resolve",
        json={"field": "revenue_fy", "keep_fact_id": keep},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved"] == "revenue_fy" and body["cleared"] == 1
    assert body["new_fact_id"] is None  # keep 最新版 → 无需补写

    after = client.get("/api/knowledge/stock/AAPL").json()["facts"]["revenue_fy"]
    assert after["conflict"] is False and after["value"] == 200

    # 版本链落裁决事件：kb-* 维护 run 可审计，但不进会话列表
    resolutions = [e for e in events.read("kb-stock-AAPL") if e.type == "fact/conflict_resolved"]
    assert len(resolutions) == 1
    assert resolutions[0].payload["keep_fact_id"] == keep
    assert resolutions[0].payload["note"]
    sessions = [r["run_id"] for r in client.get("/api/sessions").json()]
    assert "kb-stock-AAPL" not in sessions


def test_resolve_conflict_keeps_older_version_rewrites_latest(tmp_path):
    """keep 非最新版本 → 经单写者 append-only 补写同值新版本，投影回到被裁决值。"""
    client, kb, _events = conflict_seeded(tmp_path)
    v1 = kb.history("stock", "AAPL", "revenue_fy")[0]

    resp = client.post(
        "/api/knowledge/stock/AAPL/resolve",
        json={"field": "revenue_fy", "keep_fact_id": v1.fact_id, "note": "旧版才对"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["new_fact_id"] is not None
    assert body["cleared"] == 2  # v2 的竞争标记 + 补写引发的标记，一并清除

    after = client.get("/api/knowledge/stock/AAPL").json()["facts"]["revenue_fy"]
    assert after["value"] == 100 and after["conflict"] is False and after["version"] == 3
    hist = kb.history("stock", "AAPL", "revenue_fy")
    assert len(hist) == 3
    assert hist[-1].value == 100 and hist[-1].evidence_ids == v1.evidence_ids  # 证据沿用被裁决版本


def test_resolve_conflict_unknown_target_returns_404(tmp_path):
    client, _kb, _events = conflict_seeded(tmp_path)
    r1 = client.post(
        "/api/knowledge/stock/AAPL/resolve",
        json={"field": "revenue_fy", "keep_fact_id": "fact-nope"},
    )
    assert r1.status_code == 404 and "版本链" in r1.json()["detail"]
    r2 = client.post(
        "/api/knowledge/stock/AAPL/resolve",
        json={"field": "nope", "keep_fact_id": "whatever"},
    )
    assert r2.status_code == 404 and "nope" in r2.json()["detail"]


def test_resolve_lands_kept_value_when_version_tail_diverges_from_projection(tmp_path):
    """合并重放残留：version 尾 ≠ 投影（kt 序错位）→ 仍按投影补写落版。

    旧触发看版本链尾：keep=尾 → 不补写 → 投影仍指 kt 更晚的旧值，
    裁决值不可见（2026-09-03 valuation 实测，当时靠人工补落版）。
    """
    client, kb, _events = conflict_seeded(tmp_path)
    # v3：version 更大但 knowledge_time 早于 v2 → 链尾是 v3、投影是 v2
    kb.assert_fact(
        Fact(
            entity_kind="stock", entity_id="AAPL", field="revenue_fy", value=200,
            event_time=datetime(2023, 12, 31, tzinfo=UTC),
            knowledge_time=OLD, evidence_ids=["ev-2"], run_id="run-replay",
        )
    )
    v3 = kb.history("stock", "AAPL", "revenue_fy")[-1]
    assert v3.knowledge_time == OLD

    resp = client.post(
        "/api/knowledge/stock/AAPL/resolve",
        json={"field": "revenue_fy", "keep_fact_id": v3.fact_id},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["new_fact_id"] is not None  # 旧触发在此返回 None，裁决值落不了地
    after = client.get("/api/knowledge/stock/AAPL").json()["facts"]["revenue_fy"]
    assert after["fact_id"] == body["new_fact_id"] and after["conflict"] is False


def test_resolve_rejects_gate_failing_kept_value_with_409(tmp_path):
    """keep 门禁上线前的序列化 JSON 字符串旧形态 → 409 明确出路，不报 500。

    裁决失败不留半截状态：冲突标记原样保留、无新版本落库。
    """
    client, kb, _events = conflict_seeded(tmp_path)
    kb.assert_fact(
        Fact(
            entity_kind="stock", entity_id="AAPL", field="revenue_fy",
            value='{"revenue": 100}',  # 写侧门禁上线前的旧形态
            event_time=datetime(2023, 12, 31, tzinfo=UTC),
            knowledge_time=OLD, evidence_ids=["ev-1"], run_id="run-legacy",
        )
    )
    v3 = kb.history("stock", "AAPL", "revenue_fy")[-1]

    resp = client.post(
        "/api/knowledge/stock/AAPL/resolve",
        json={"field": "revenue_fy", "keep_fact_id": v3.fact_id},
    )
    assert resp.status_code == 409
    assert "准入闸" in resp.json()["detail"]
    assert len(kb.history("stock", "AAPL", "revenue_fy")) == 3
    assert kb.open_conflicts("stock", "AAPL")

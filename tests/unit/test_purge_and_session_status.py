"""删除能力与会话状态验收（用户诉求：会话/knowledge 可删；运行中不得显示失败）。

三组判据：
1. 会话状态：运行中 > 错误 > 拦停 > 取消 > 完成 > 空闲；blocked 不冒充 error；
   进程被杀（有 turn/start 无 turn/end + turn/error）→ error 而不是永远 running；
   只有投影副作用事件的 run（migration-shadow / dossier）不进会话列表。
2. 会话删除：级联子 run + 报告目录；正在跑的默认拒删（409 语义），force 才删；
   删除留 `session/deleted` 审计。
3. 知识实体删除：tombstone（默认，可恢复，读路径全部过滤）与 hard（真删行 +
   孤儿证据 + 磁盘存档，不可恢复）；两者都留 `knowledge/purged` 审计。
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from finance_agent.api.app import create_app
from finance_agent.decision.store import DecisionStore
from finance_agent.eventstore.events import Event
from finance_agent.eventstore.store import EventStore
from finance_agent.knowledge.errors import MissingEvidenceError
from finance_agent.knowledge.metric_store import MetricStore
from finance_agent.knowledge.models import Evidence, Fact, PitGrade
from finance_agent.knowledge.purge import (
    PurgeError,
    preview_entity,
    purge_entity,
    purge_session,
)
from finance_agent.knowledge.store import BitemporalStore

NOW = datetime(2024, 6, 1, tzinfo=UTC)


@pytest.fixture()
def env(tmp_path: Path):
    kb = BitemporalStore(tmp_path / "kb.db")
    metrics = MetricStore(tmp_path / "m.db")
    events = EventStore(tmp_path / "e.db")
    decisions = DecisionStore(tmp_path / "d.db")
    kb.add_evidence(Evidence(
        evidence_id="ev-1", source_id="demo", verbatim_quote="shared evidence 100 million",
        retrieved_at=NOW, available_at=NOW, pit_grade=PitGrade.A,
    ))
    kb.add_evidence(Evidence(
        evidence_id="ev-2", source_id="demo", verbatim_quote="only-for-bad-entity 5 million",
        retrieved_at=NOW, available_at=NOW, pit_grade=PitGrade.A,
    ))
    for field, ev in (("market_size", "ev-1"), ("growth_rate", "ev-2")):
        kb.assert_fact(Fact(
            entity_kind="industry", entity_id="bad-industry", field=field,
            value=f"{field} 低质量内容", knowledge_time=NOW, evidence_ids=[ev],
        ))
    kb.assert_fact(Fact(
        entity_kind="stock", entity_id="GOOD", field="revenue_fy",
        value="收入 100 million（共用 ev-1）", knowledge_time=NOW, evidence_ids=["ev-1"],
    ))
    client = TestClient(create_app(
        kb=kb, events=events, decisions=decisions, evals_dir=tmp_path / "evals",
        metrics=metrics, reports_dir=tmp_path / "reports", knowledge_dir=tmp_path / "knowledge",
    ))
    return client, kb, metrics, events, decisions, tmp_path


def seed_session(events: EventStore, run_id: str, *, outcome: str | None = None,
                 open_command: bool = False, error_after_open: bool = False) -> None:
    seq = 0

    def emit(type_: str, payload: dict) -> None:
        nonlocal seq
        seq += 1
        events.append(Event(run_id=run_id, type=type_, payload=payload))

    emit("run/created", {"kind": "session"})
    emit("session/title", {"title": f"{run_id} 标题"})
    if open_command:
        emit("command/run", {"command_id": "cmd-old", "name": "research"})
        if outcome:
            emit("command/done", {"command_id": "cmd-old", "outcome": outcome,
                                  "summary": f"{outcome} 摘要"})
        emit("command/run", {"command_id": "cmd-new", "name": "research"})
        if error_after_open:
            emit("turn/error", {"reason": "provider 挂了"})
    elif outcome:
        emit("command/run", {"command_id": "cmd-1", "name": "research"})
        emit("command/done", {"command_id": "cmd-1", "outcome": outcome,
                              "summary": f"{outcome} 摘要"})


# ---------------- 1. 会话状态 ----------------


class TestSessionStatus:
    def test_running_wins_over_previous_blocked(self, env):
        """现象一：上一条命令 blocked，新命令在跑 → 必须显示运行中，不是失败。"""
        client, kb, metrics, events, decisions, tmp = env
        seed_session(events, "live-run", outcome="blocked", open_command=True)
        row = next(s for s in client.get("/api/sessions").json() if s["run_id"] == "live-run")
        assert row["status"] == "running"
        assert row["last_outcome"] == "blocked"
        assert row["running_commands"] == ["cmd-new"]
        assert row["last_blocked"] == "blocked 摘要"

    def test_blocked_is_not_error(self, env):
        client, kb, metrics, events, decisions, tmp = env
        seed_session(events, "live-blocked", outcome="blocked")
        row = next(s for s in client.get("/api/sessions").json()
                   if s["run_id"] == "live-blocked")
        assert row["status"] == "blocked"
        assert "blocked 摘要" in row["status_detail"]

    def test_error_status_for_real_failure(self, env):
        client, kb, metrics, events, decisions, tmp = env
        seed_session(events, "live-err", outcome="error")
        row = next(s for s in client.get("/api/sessions").json() if s["run_id"] == "live-err")
        assert row["status"] == "error"

    def test_dead_in_flight_is_error_not_running(self, env):
        """进程被杀/turn 报错：有未闭合 turn 但错误在其后 → error，不永远 running。"""
        client, kb, metrics, events, decisions, tmp = env
        seed_session(events, "live-dead", outcome=None, open_command=True,
                     error_after_open=True)
        row = next(s for s in client.get("/api/sessions").json() if s["run_id"] == "live-dead")
        assert row["status"] == "error"
        assert row["status_detail"] == "provider 挂了"

    def test_ghost_runs_are_not_sessions(self, env):
        """现象二：migration-shadow / dossier 这类只有投影事件的 run 不进列表。"""
        client, kb, metrics, events, decisions, tmp = env
        for run_id in ("migration-shadow", "dossier", "kb-stock-AAPL", "eval-replay-1"):
            events.append(Event(run_id=run_id, type="dossier/published",
                                payload={"snapshot_id": "s1", "entity": "stock:X"}))
        seed_session(events, "live-real", outcome="completed")
        ids = {s["run_id"] for s in client.get("/api/sessions").json()}
        assert "live-real" in ids
        assert not ({"migration-shadow", "dossier", "kb-stock-AAPL", "eval-replay-1"} & ids)

    def test_child_runs_are_not_listed_as_sessions(self, env):
        client, kb, metrics, events, decisions, tmp = env
        seed_session(events, "live-parent", outcome="completed")
        events.append(Event(run_id="live-parent--cmd-1-1-research", type="run/created",
                            payload={"parent_run_id": "live-parent", "kind": "step_agent"}))
        ids = {s["run_id"] for s in client.get("/api/sessions").json()}
        assert "live-parent" in ids
        assert "live-parent--cmd-1-1-research" not in ids

    def test_stale_running_is_flagged(self, env):
        """运行中但长时间无事件 → 诚实标「可能已中断」（不假装活着）。"""
        client, kb, metrics, events, decisions, tmp = env
        seed_session(events, "live-stale", open_command=True)
        # 把最后活动时间改到 2 小时前（直接改库：模拟进程被杀后无人写事件）
        old = (datetime.now(UTC).replace(microsecond=0) - __import__("datetime").timedelta(hours=2))
        events._conn.execute(  # noqa: SLF001 - 测试夹具
            "UPDATE events SET ts = ? WHERE run_id = ?", (old.isoformat(), "live-stale"))
        events._conn.commit()
        row = next(s for s in client.get("/api/sessions").json() if s["run_id"] == "live-stale")
        assert row["status"] == "running" and row["possibly_stale"] is True


# ---------------- 2. 会话删除 ----------------


class TestSessionDeletion:
    def test_delete_cascades_children_and_reports(self, env):
        client, kb, metrics, events, decisions, tmp = env
        seed_session(events, "live-del", outcome="completed")
        events.append(Event(run_id="live-del--cmd-1-1-research", type="run/created",
                            payload={"parent_run_id": "live-del", "kind": "step_agent"}))
        events.append(Event(run_id="live-del--cmd-1-1-research", type="assistant/message",
                            payload={"content": "x"}))
        report_dir = tmp / "reports" / "live-del--cmd-1-1-research"
        report_dir.mkdir(parents=True)
        (report_dir / "report.md").write_text("# x", encoding="utf-8")

        resp = client.delete("/api/sessions/live-del", params={"reason": "低质量历史"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total_events"] >= 4
        assert "live-del--cmd-1-1-research" in body["deleted_runs"]
        assert not report_dir.exists()
        # 事件真删 + 审计留痕
        assert events.read("live-del") == []
        audit = [e for e in events.read("system-purge") if e.type == "session/deleted"]
        assert audit and audit[0].payload["reason"] == "低质量历史"
        assert "live-del" not in {s["run_id"] for s in client.get("/api/sessions").json()}

    def test_running_session_refused_without_force(self, env):
        client, kb, metrics, events, decisions, tmp = env
        seed_session(events, "live-busy", open_command=True)
        resp = client.delete("/api/sessions/live-busy")
        assert resp.status_code == 409
        assert "仍在运行" in resp.json()["detail"]
        assert events.read("live-busy"), "拒删却已经删了事件"
        forced = client.delete("/api/sessions/live-busy", params={"force": "true"})
        assert forced.status_code == 200
        assert events.read("live-busy") == []

    def test_ghost_run_cannot_be_deleted_as_session(self, env):
        client, kb, metrics, events, decisions, tmp = env
        events.append(Event(run_id="migration-shadow", type="dossier/published",
                            payload={"snapshot_id": "s"}))
        resp = client.delete("/api/sessions/migration-shadow")
        assert resp.status_code == 422
        assert "不是会话" in resp.json()["detail"]
        assert events.read("migration-shadow"), "投影审计事件被误删"

    def test_purge_session_helper_rejects_unknown_mode(self, env):
        client, kb, metrics, events, decisions, tmp = env
        with pytest.raises(PurgeError):
            purge_session(events=events, run_id="does-not-exist")


# ---------------- 3. 知识实体删除 ----------------


class TestEntityDeletion:
    def test_preview_counts_without_writing(self, env):
        client, kb, metrics, events, decisions, tmp = env
        pv = preview_entity(kb=kb, metrics=metrics, decisions=decisions,
                            entity_kind="industry", entity_id="bad-industry")
        assert pv["counts"]["facts"] == 2 and pv["total_rows"] == 2
        assert pv["already_purged"] is False
        assert kb.view("industry", "bad-industry", NOW), "干跑却改了数据"

    def test_tombstone_hides_from_every_read_path(self, env):
        client, kb, metrics, events, decisions, tmp = env
        resp = client.delete("/api/knowledge/industry/bad-industry",
                             params={"reason": "旧口径重复档案"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["mode"] == "tombstone" and body["restorable"] is True
        assert body["counts"] == {}  # 墓碑不删行
        # 列表不再出现；详情 410；数据仍在（可审计）
        ids = {(e["kind"], e["id"]) for e in client.get("/api/knowledge/entities").json()}
        assert ("industry", "bad-industry") not in ids
        assert ("stock", "GOOD") in ids
        assert client.get("/api/knowledge/industry/bad-industry").status_code == 410
        assert kb.view("industry", "bad-industry", NOW), "墓碑模式不应删行"
        purged = client.get("/api/knowledge/purged").json()
        assert purged and purged[0]["entity_id"] == "bad-industry"
        audit = [e for e in events.read("system-purge") if e.type == "knowledge/purged"]
        assert audit and audit[0].payload["reason"] == "旧口径重复档案"

    def test_include_purged_shows_tombstones(self, env):
        client, kb, metrics, events, decisions, tmp = env
        client.delete("/api/knowledge/industry/bad-industry", params={"reason": "x"})
        rows = client.get("/api/knowledge/entities",
                          params={"include_purged": "true"}).json()
        bad = next(r for r in rows if r["id"] == "bad-industry")
        assert bad["purged"] is True

    def test_restore_brings_entity_back(self, env):
        client, kb, metrics, events, decisions, tmp = env
        client.delete("/api/knowledge/industry/bad-industry", params={"reason": "x"})
        assert client.get("/api/knowledge/industry/bad-industry").status_code == 410
        resp = client.post("/api/knowledge/industry/bad-industry/restore",
                           params={"reason": "误删"})
        assert resp.status_code == 200 and resp.json()["restored"] is True
        assert client.get("/api/knowledge/industry/bad-industry").status_code == 200
        ids = {(e["kind"], e["id"]) for e in client.get("/api/knowledge/entities").json()}
        assert ("industry", "bad-industry") in ids
        assert [e for e in events.read("system-purge") if e.type == "knowledge/restored"]

    def test_hard_delete_removes_rows_orphan_evidence_and_files(self, env):
        client, kb, metrics, events, decisions, tmp = env
        # 磁盘存档（hard 模式要清）
        archive = tmp / "knowledge" / "industries" / "bad-industry"
        archive.mkdir(parents=True)
        (archive / "latest.html").write_text("<html>低质量</html>", encoding="utf-8")
        # typed 库也有行
        metrics.save_claim(claim_id="claim-bad", namespace="prod", payload={
            "claim_id": "claim-bad", "entity_kind": "industry",
            "entity_id": "bad-industry", "kind": "inference", "status": "draft",
            "statement": "低质量结论", "created_at": NOW.isoformat(), "support_refs": []})
        assert kb.evidence_count() == 2

        resp = client.delete("/api/knowledge/industry/bad-industry",
                             params={"mode": "hard", "reason": "低质量内容，硬删"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["mode"] == "hard" and body["restorable"] is False
        assert body["counts"]["facts"] == 2
        assert body["counts"]["research_claims"] == 1
        # 孤儿证据被清（ev-2 只服务这个实体），共用证据保留（ev-1 还被 GOOD 引用）
        assert body["orphan_evidence_deleted"] == 1
        assert body["evidence_remaining"] == 1
        with pytest.raises(MissingEvidenceError):
            kb.get_evidence("ev-2")
        assert kb.get_evidence("ev-1").evidence_id == "ev-1"
        assert not archive.exists()
        assert str(archive) in body["files_removed"]
        # 硬删不可恢复
        restore = client.post("/api/knowledge/industry/bad-industry/restore")
        assert restore.status_code == 409

    def test_hard_delete_keeps_other_entity_intact(self, env):
        client, kb, metrics, events, decisions, tmp = env
        client.delete("/api/knowledge/industry/bad-industry",
                      params={"mode": "hard", "reason": "x"})
        profile = client.get("/api/knowledge/stock/GOOD")
        assert profile.status_code == 200
        assert "revenue_fy" in profile.json()["facts"]

    def test_unknown_mode_and_kind_rejected(self, env):
        client, kb, metrics, events, decisions, tmp = env
        assert client.delete("/api/knowledge/industry/bad-industry",
                             params={"mode": "wipe"}).status_code == 422
        assert client.delete("/api/knowledge/alien/bad-industry").status_code == 422

    def test_purge_helper_reports_missing_reason(self, env):
        client, kb, metrics, events, decisions, tmp = env
        report = purge_entity(kb=kb, metrics=metrics, decisions=decisions, events=events,
                              entity_kind="industry", entity_id="bad-industry", reason="")
        assert any("未给删除原因" in w for w in report.as_payload()["warnings"])


# ---------------- 4. 批量删除（用户诉求：一个个删太慢、每次多次点击） ----------------


def seed_second_entity(kb) -> None:
    kb.add_evidence(Evidence(
        evidence_id="ev-3", source_id="demo", verbatim_quote="second entity 7 million",
        retrieved_at=NOW, available_at=NOW, pit_grade=PitGrade.A,
    ))
    kb.assert_fact(Fact(
        entity_kind="stock", entity_id="BAD2", field="revenue_fy",
        value="第二个低质量档案", knowledge_time=NOW, evidence_ids=["ev-3"],
    ))


class TestBatchDelete:
    def test_batch_sessions_one_confirm_deletes_many(self, env):
        client, kb, metrics, events, decisions, tmp = env
        for rid in ("live-a", "live-b", "live-c"):
            seed_session(events, rid, outcome="completed")
            events.append(Event(run_id=f"{rid}--cmd-1-1-research", type="run/created",
                                payload={"parent_run_id": rid, "kind": "step_agent"}))
        resp = client.post("/api/sessions/batch_delete", json={
            "run_ids": ["live-a", "live-b", "live-c"],
            "reason": "批量清理", "force": False,
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["deleted"] == 3 and body["failed"] == 0
        assert body["total_events"] >= 6
        ids = {s["run_id"] for s in client.get("/api/sessions").json()}
        assert not ({"live-a", "live-b", "live-c"} & ids)
        # 每个成功删除各自留审计
        audit = [e for e in events.read("system-purge") if e.type == "session/deleted"]
        assert len(audit) == 3

    def test_batch_sessions_partial_failure_does_not_abort(self, env):
        """一个在跑且未 force → 该项失败，其余照删。"""
        client, kb, metrics, events, decisions, tmp = env
        seed_session(events, "live-busy2", open_command=True)
        seed_session(events, "live-ok2", outcome="completed")
        resp = client.post("/api/sessions/batch_delete", json={
            "run_ids": ["live-busy2", "live-ok2"], "reason": "批量", "force": False,
        })
        body = resp.json()
        assert body["deleted"] == 1 and body["failed"] == 1
        busy = next(r for r in body["results"] if r["run_id"] == "live-busy2")
        assert busy["ok"] is False and "仍在运行" in busy["error"]
        assert events.read("live-busy2"), "失败项不该被删"
        assert events.read("live-ok2") == []

    def test_batch_sessions_force_deletes_running(self, env):
        client, kb, metrics, events, decisions, tmp = env
        seed_session(events, "live-busy3", open_command=True)
        resp = client.post("/api/sessions/batch_delete", json={
            "run_ids": ["live-busy3"], "reason": "僵尸清理", "force": True,
        })
        assert resp.json()["deleted"] == 1
        assert events.read("live-busy3") == []

    def test_batch_entities_tombstone_and_per_item_reports(self, env):
        client, kb, metrics, events, decisions, tmp = env
        seed_second_entity(kb)
        resp = client.post("/api/knowledge/batch_delete", json={
            "entities": [{"kind": "industry", "id": "bad-industry"},
                         {"kind": "stock", "id": "BAD2"},
                         {"kind": "stock", "id": "GOOD"}],
            "mode": "tombstone", "reason": "批量清理低质量",
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # 只删前两个？不——批量按给定列表删；GOOD 也被删（调用方负责选）
        assert body["deleted"] == 3 and body["failed"] == 0
        assert body["total_rows_deleted"] == 0  # 墓碑不删行
        ids = {(e["kind"], e["id"]) for e in client.get("/api/knowledge/entities").json()}
        assert ids == set(), "批量墓碑后列表应全空"
        purged = client.get("/api/knowledge/purged").json()
        assert {p["entity_id"] for p in purged} == {"bad-industry", "BAD2", "GOOD"}
        # 每项回执可核对
        assert all(r["report"]["restorable"] is True for r in body["results"])

    def test_batch_entities_hard_cleans_orphans_across_items(self, env):
        client, kb, metrics, events, decisions, tmp = env
        seed_second_entity(kb)
        before = kb.evidence_count()
        resp = client.post("/api/knowledge/batch_delete", json={
            "entities": [{"kind": "industry", "id": "bad-industry"},
                         {"kind": "stock", "id": "BAD2"}],
            "mode": "hard", "reason": "批量硬删",
        })
        body = resp.json()
        assert body["deleted"] == 2
        # ev-2 只服务 bad-industry、ev-3 只服务 BAD2 → 两条孤儿都被清；
        # ev-1 被 GOOD 引用 → 保留
        assert body["orphan_evidence_deleted"] == 2
        assert kb.evidence_count() == before - 2
        assert kb.get_evidence("ev-1").evidence_id == "ev-1"

    def test_batch_rejects_invalid_entity_kind_per_item(self, env):
        client, kb, metrics, events, decisions, tmp = env
        resp = client.post("/api/knowledge/batch_delete", json={
            "entities": [{"kind": "alien", "id": "X"},
                         {"kind": "industry", "id": "bad-industry"}],
            "mode": "tombstone", "reason": "x",
        })
        body = resp.json()
        assert body["deleted"] == 1 and body["failed"] == 1
        bad = next(r for r in body["results"] if not r["ok"])
        assert "非法实体标识" in bad["error"]

    def test_batch_requires_nonempty_list(self, env):
        client, kb, metrics, events, decisions, tmp = env
        assert client.post("/api/sessions/batch_delete", json={"run_ids": []}).status_code == 422
        assert client.post("/api/knowledge/batch_delete",
                           json={"entities": []}).status_code == 422

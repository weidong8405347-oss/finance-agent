"""P4 投资判断层的契约测试（research-capability-upgrade §5.1）。

覆盖：F1.5 thesis 备忘录、投资委员会（四视角+空头+CIO）、估值快照 calc、
PDF/HTML 抓取分发、/decide 委员会上下文注入。
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_commands import make_deps

from finance_agent.commands.steps import (
    StepContext,
    step_committee,
    step_thesis,
)
from finance_agent.eventstore.events import Event
from finance_agent.llm.base import AssistantReply, ToolCall
from finance_agent.llm.mock import MockLLM


def _ctx(child_suffix: str = "-2-thesis", kind: str = "industry", ticker: str = "ai4s") -> StepContext:
    return StepContext(
        command_id="cmd-t", session_run_id="live-t",
        child_run_id=f"live-t--cmd-t{child_suffix}",
        ticker=ticker, objective="AI for Science 美股港股", config="",
        should_cancel=lambda: False, entity_kind=kind,
    )


def _tc(i: int, name: str, args: dict) -> ToolCall:
    return ToolCall(call_id=f"c{i}", name=name, arguments=args)


class TestThesisStep:
    def test_writes_thesis_field(self, tmp_path):
        script = [
            AssistantReply(content="", tool_calls=[_tc(0, "query_demo", {})]),
            AssistantReply(content="", tool_calls=[_tc(1, "register_evidence", {
                "evidence_id": "ev-t", "chunk_id": "chk-0001", "verbatim_quote": "10-K"})]),
            AssistantReply(content="", tool_calls=[_tc(2, "propose_fact", {
                "field": "thesis", "value": "瓶颈在验证层。", "evidence_ids": ["ev-t"]})]),
            AssistantReply(content="done"),
        ]
        deps, events, kb, _ = make_deps(tmp_path, {"research": [script]})
        c = _ctx()
        result = step_thesis(deps, c)
        assert result.status == "completed"
        view = kb.view("industry", "ai4s", datetime.now(UTC))
        assert "瓶颈在验证层" in view["thesis"].value
        # 版本哈希落事件（可复现性）
        pb = [e for e in events.read(c.child_run_id) if e.type == "research/playbook"]
        assert pb and pb[0].payload["name"] == "thesis" and pb[0].payload["version"]

    def test_missing_thesis_blocks(self, tmp_path):
        """thesis 是后续环节的锚——写不出来就 blocked（不许空转下去）。"""
        deps, _, _, _ = make_deps(tmp_path, {"research": [[AssistantReply(content="查不到")]]})
        result = step_thesis(deps, _ctx())
        assert result.status == "blocked"


class TestCommitteeStep:
    def test_produces_artifacts_and_notes(self, tmp_path):
        # 种子：闸口名单 + 标的档案 + 行业 thesis
        deps, events, kb, _ = make_deps(tmp_path, {"research": []})
        events.append(Event(run_id="live-t", type="industry/screen_result",
                            payload={"command_id": "cmd-t", "approved_tickers": ["BE"]}))
        from finance_agent.harness.manifest import RunManifest, RunMode
        from finance_agent.knowledge.models import Evidence, Fact, PitGrade

        old = datetime(2024, 3, 1, tzinfo=UTC)
        kb.add_evidence(Evidence(
            evidence_id="ev-seed", source_id="edgar", url="demo://x",
            verbatim_quote="x", retrieved_at=old, available_at=old, pit_grade=PitGrade.A))
        deps.writer.write_fact(
            Fact(entity_kind="stock", entity_id="BE", field="moat", value="生态锁定",
                 knowledge_time=old, evidence_ids=["ev-seed"], run_id="seed"),
            run=RunManifest(run_id="seed", mode=RunMode.LIVE))

        ctx = _ctx(child_suffix="-6-committee")
        result = step_committee(deps, ctx)
        assert result.status == "completed"
        # 委员会记录落 artifact（判断不落 KB）
        artifacts = list(Path(deps.reports_dir).glob("*/committee_BE.md"))
        assert artifacts, "每票一份委员会记录"
        text = artifacts[0].read_text()
        assert "CIO 综合" in text
        # 会话流有 committee_note 事件
        notes = [e for e in events.read("live-t") if e.type == "industry/committee_note"]
        assert notes and notes[0].payload["ticker"] == "BE"


class TestCommitteeFailureVisibility:
    def test_empty_perspective_marked(self, tmp_path):
        """视角产出为空（步数耗尽/模型空回）→ artifact 明示，不允许静默空白章节。
        2026-09-02 P4 验收实测：read_evidence 缺 schema → 空参调用烧光 8 步 → financial 章节空白。"""
        deps, events, kb, _ = make_deps(tmp_path, {
            "research": [[AssistantReply(content="", tool_calls=[])]],  # 第一个视角空回
        })
        events.append(Event(run_id="live-t", type="industry/screen_result",
                            payload={"command_id": "cmd-t", "approved_tickers": ["BE"]}))
        from finance_agent.harness.manifest import RunManifest, RunMode
        from finance_agent.knowledge.models import Evidence, Fact, PitGrade

        old = datetime(2024, 3, 1, tzinfo=UTC)
        kb.add_evidence(Evidence(
            evidence_id="ev-seed", source_id="edgar", url="demo://x",
            verbatim_quote="x", retrieved_at=old, available_at=old, pit_grade=PitGrade.A))
        deps.writer.write_fact(
            Fact(entity_kind="stock", entity_id="BE", field="moat", value="生态锁定",
                 knowledge_time=old, evidence_ids=["ev-seed"], run_id="seed"),
            run=RunManifest(run_id="seed", mode=RunMode.LIVE))

        result = step_committee(deps, _ctx(child_suffix="-6-committee"))
        assert result.status == "completed"  # 失败隔离：单视角空产出不熔断
        text = list(Path(deps.reports_dir).glob("*/committee_BE.md"))[0].read_text()
        assert "（该视角产出为空" in text  # 空白章节被标记
        assert "\n## \n" not in text.replace("（该视角产出为空：步数耗尽或模型未产出，CIO 综合时降权）", "")

    def test_read_evidence_schema_registered(self):
        """read_evidence 必须有 function schema（否则路由层回退空参 schema，模型 {} 空转）。"""
        from finance_agent.cli import _all_tool_schemas
        from finance_agent.research.tools import TOOL_SCHEMAS

        for table in (TOOL_SCHEMAS, _all_tool_schemas()):
            schema = table.get("read_evidence")
            assert schema, "read_evidence 缺 schema（路由将下发空参 schema）"
            assert "evidence_id" in schema["parameters"]["properties"]
            assert "evidence_id" in schema["parameters"]["required"]


class TestRankReportBriefIntegrity:
    """F5 brief 完整性（2026-09-02 验收事故回归）：全局截断把名单尾部整票丢掉，
    模型据残缺输入编造漏斗叙事。修法：逐票预算 + 漏斗事实 + 名单完整性纪律。"""

    def _seed(self, deps, kb, tickers):
        from finance_agent.harness.manifest import RunManifest, RunMode
        from finance_agent.knowledge.models import Evidence, Fact, PitGrade

        old = datetime(2024, 3, 1, tzinfo=UTC)
        kb.add_evidence(Evidence(
            evidence_id="ev-seed", source_id="edgar", url="demo://x",
            verbatim_quote="x", retrieved_at=old, available_at=old, pit_grade=PitGrade.A))
        run = RunManifest(run_id="seed", mode=RunMode.LIVE)
        # 标的池（行业档案）
        deps.writer.write_fact(
            Fact(entity_kind="industry", entity_id="ai4s", field="player_landscape",
                 value=[{"ticker": t, "evidence_ids": ["ev-seed"]} for t in tickers],
                 knowledge_time=old, evidence_ids=["ev-seed"], run_id="seed"), run=run)
        # 各票档案（BE 超长，老代码的全局截断会截掉 RXRX）
        deps.writer.write_fact(
            Fact(entity_kind="stock", entity_id="BE", field="moat",
                 value="BE 护城河哨兵 " + "长文本" * 3000,
                 knowledge_time=old, evidence_ids=["ev-seed"], run_id="seed"), run=run)
        deps.writer.write_fact(
            Fact(entity_kind="stock", entity_id="RXRX", field="moat", value="RXRX 护城河哨兵",
                 knowledge_time=old, evidence_ids=["ev-seed"], run_id="seed"), run=run)

    def test_brief_covers_all_approved_tickers(self, tmp_path):
        deps, events, kb, _ = make_deps(tmp_path, {
            "research": [[AssistantReply(content="# 排序报告正文")]],
        })
        self._seed(deps, kb, ["BE", "RXRX"])
        events.append(Event(run_id="live-t", type="industry/screen_result",
                            payload={"command_id": "cmd-t", "approved_tickers": ["BE", "RXRX"],
                                     "round": 1, "table": "| BE | 推荐 |\n| RXRX | 推荐 |"}))
        # 两票委员会记录（RXRX 的超长，验证逐票截断不丢票）
        cdir = Path(deps.reports_dir) / "live-t--cmd-t-6-committee"
        cdir.mkdir(parents=True)
        (cdir / "committee_BE.md").write_text("# BE 委员会记录哨兵")
        (cdir / "committee_RXRX.md").write_text("# RXRX 委员会记录哨兵\n" + "详述" * 7000)

        from finance_agent.commands.steps import step_rank_report
        ctx = _ctx(child_suffix="-7-rank_report")
        result = step_rank_report(deps, ctx)
        assert result.status == "completed"

        brief = [e.payload["content"] for e in events.read(ctx.child_run_id)
                 if e.type == "user/message"][0]
        # 两票档案都在（老代码 8000 全局截断会丢 RXRX）
        assert "BE 护城河哨兵" in brief and "RXRX 护城河哨兵" in brief
        # 两票委员会记录都在（老代码 6000 全局截断只剩第一票）
        assert "BE 委员会记录哨兵" in brief and "RXRX 委员会记录哨兵" in brief
        assert "本票委员会记录超长截断" in brief  # 超长逐票截断留痕，不静默
        # 漏斗事实 + 名单完整性纪律
        assert "标的池（F2 双通道挖掘，均绑归属证据）：2 只" in brief
        assert "| BE | 推荐 |" in brief  # 闸口筛分表进 brief
        assert "BE、RXRX（共 2 只）" in brief and "报告作废" in brief


class TestValuationSnapshot:
    def test_multiples(self):
        from finance_agent.research.calc import calc_tool

        r = json.loads(calc_tool({
            "op": "valuation_snapshot", "market_cap": 9.1e9, "net_debt": -0.5e9,
            "revenue": 3.55e9, "ebitda": 0.5e9, "fcf": 0.25e9,
            "price": 62.0, "eps": 2.15,
        })["content"])
        assert float(r["ev"]) == 8.6e9
        assert r["ev_sales"] and abs(float(r["ev_sales"]) - 2.42) < 0.01
        assert r["pe"] and abs(float(r["pe"]) - 28.84) < 0.05
        assert r["fcf_yield_pct"] and float(r["fcf_yield_pct"]) > 2

    def test_negative_ebitda_not_misleading(self):
        from finance_agent.research.calc import calc_tool

        r = json.loads(calc_tool({
            "op": "valuation_snapshot", "market_cap": 1e9, "ebitda": -50e6,
        })["content"])
        assert r["ev_ebitda"] == "N/M（负 EBITDA）"  # 负值不给假倍数


class TestFetchDispatch:
    def test_html_and_pdf_dispatch(self, monkeypatch):
        import finance_agent.gateway.fetch as fetch_mod

        class _Resp:
            def __init__(self, headers, text="", content=b""):
                self.headers = headers
                self.text = text
                self.content = content

            def raise_for_status(self):
                pass

        monkeypatch.setattr("httpx.get",
                            lambda *a, **k: _Resp({"content-type": "text/html"}, text="<p>正文</p>"))
        assert "正文" in fetch_mod.fetch_document("http://x/filing")

        # PDF 路径：内容类型 application/pdf → 走 pdf_to_text_paged（mock 掉真实解析）
        monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(
            {"content-type": "application/pdf"}, content=b"%PDF-1.4 fake"))
        monkeypatch.setattr(fetch_mod, "pdf_to_text_paged", lambda b, **k: ("PDF 正文", 3))
        assert fetch_mod.fetch_document("http://x/report.pdf") == "PDF 正文"

    def test_checked_fetch_reports_quality(self, monkeypatch):
        """抓取质量单独标记（audit §3.5）：PIT 等级表达不了乱码/扫描件。"""
        import finance_agent.gateway.fetch as fetch_mod

        class _Resp:
            def __init__(self, headers, text="", content=b""):
                self.headers, self.text, self.content = headers, text, content

            def raise_for_status(self):
                pass

        monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(
            {"content-type": "text/html"},
            text="<p>Total revenue was 1,234 million for fiscal 2024.</p>"))
        text, quality = fetch_mod.fetch_document_checked("http://x/filing")
        assert "1,234 million" in text
        assert quality.quality == "ok", quality.reasons

        # 乱码抽取 → garbled（不得当作可靠数字来源）
        monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(
            {"content-type": "text/html"},
            text="<p>" + "\ufffd\x02\x03\ufffd" * 40 + "</p>"))
        _, bad = fetch_mod.fetch_document_checked("http://x/garbled")
        assert bad.quality == "garbled"
        assert not bad.usable_for_metrics

        # 有页无字的扫描件 → needs_ocr
        monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(
            {"content-type": "application/pdf"}, content=b"%PDF-1.4 fake"))
        monkeypatch.setattr(fetch_mod, "pdf_to_text_paged", lambda b, **k: ("", 42))
        _, scanned = fetch_mod.fetch_document_checked("http://x/scan.pdf")
        assert scanned.quality == "needs_ocr"
        assert not scanned.usable_for_metrics


class TestDecideCommitteeContext:
    def test_context_note_reaches_decision_turn(self, tmp_path):
        from finance_agent.decision.loop import DecisionLoop
        from finance_agent.decision.service import DecisionService
        from finance_agent.decision.store import DecisionStore
        from finance_agent.eventstore.store import EventStore
        from finance_agent.harness.manifest import RunManifest, RunMode
        from finance_agent.knowledge.store import BitemporalStore

        events = EventStore(":memory:")
        kb = BitemporalStore(tmp_path / "kb.db")
        llm = MockLLM([AssistantReply(content="观望")])
        loop = DecisionLoop(
            kb=kb, events=events,
            decision_service=DecisionService(
                kb=kb, decisions=DecisionStore(tmp_path / "d.db"), events=events),
            llm=llm, manifest=RunManifest(run_id="t-dec", mode=RunMode.LIVE),
        )
        loop.run("stock", "BE", context_note="CIO 综合：偏多，注意现金流")
        msgs = [e for e in events.read("t-dec") if e.type == "user/message"]
        assert msgs and "CIO 综合：偏多" in msgs[0].payload["content"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

"""/industry 行业调研漏斗（P3 §4.1）的契约测试。

覆盖：
- /industry 解析（主题是自然语言整段）
- F1 赛道地图 → F2 标的池（三 worker 并集 + 证据绑定）→ F3 粗筛闸口（批准通过 /
  打回带反馈迭代 / 无反馈拒绝 blocked）→ F4 深研 fan-out → F5 排序报告
- 全链路走真实装配（fixture 数据源 + MockLLM），闸口用事件监听线程裁决
"""

import threading
import time
from datetime import UTC, datetime

from test_commands import RESEARCH_SCRIPT, make_deps, wait_for

from finance_agent.commands.registry import parse_command
from finance_agent.commands.runner import CommandRequest, CommandRunner
from finance_agent.llm.base import AssistantReply, ToolCall


def tc(i: int, name: str, args: dict) -> ToolCall:
    return ToolCall(call_id=f"c{i}", name=name, arguments=args)


# F1 行业研究脚本：query_demo → 读窗 → 逐字段登记证据+写事实（4 必填 + sub_sectors 可选）
def _industry_research_script(fields: list[str]) -> list:
    replies = [
        AssistantReply(content="", tool_calls=[tc(0, "query_demo", {"ticker": "x"})]),
        AssistantReply(content="", tool_calls=[tc(1, "read_edgar_filing",
                                                  {"chunk_id": "chk-0001", "query": "产能"})]),
    ]
    for i, f in enumerate(fields):
        replies += [
            AssistantReply(content="", tool_calls=[tc(10 + i * 2, "register_evidence", {
                "evidence_id": f"ev-{f}", "chunk_id": "chk-0002",
                "verbatim_quote": "产能 2GW 公告"})]),
            AssistantReply(content="", tool_calls=[tc(11 + i * 2, "propose_fact", {
                "field": f,
                "value": ([{"name": "AI 制药", "definition": "f"}]
                          if f == "sub_sectors" else f"{f} 内容"),  # 结构化字段必须 list[dict]
                "evidence_ids": [f"ev-{f}"]})]),
        ]
    replies.append(AssistantReply(content="industry map done"))
    return replies


# F2 挖掘 worker 脚本：检索 → 登记证据 → 提交两只候选
def _digger_script(ev_prefix: str) -> list:
    return [
        AssistantReply(content="", tool_calls=[tc(0, "query_demo", {"ticker": "x"})]),
        AssistantReply(content="", tool_calls=[tc(1, "register_evidence", {
            "evidence_id": f"{ev_prefix}-1", "chunk_id": "chk-0001",
            "verbatim_quote": "10-K"})]),
        AssistantReply(content="", tool_calls=[tc(2, "propose_candidates", {"candidates": [
            {"ticker": "BE", "name": "Bloom Energy", "market": "US", "sub_sector": "s1",
             "one_liner": "燃料电池", "listed": True, "evidence_ids": [f"{ev_prefix}-1"]},
            {"ticker": "RXRX", "name": "Recursion", "market": "US", "sub_sector": "s1",
             "one_liner": "AI 制药", "listed": True, "evidence_ids": [f"{ev_prefix}-1"]},
        ]})]),
        AssistantReply(content="dig done"),
    ]


# F3 粗调研卡 worker 脚本：登记证据 → 提交卡（BE 推荐 / RXRX 不推荐）
def _card_script(ticker: str, recommend: bool, reason: str) -> list:
    return [
        AssistantReply(content="", tool_calls=[tc(0, "query_demo", {"ticker": "x"})]),
        AssistantReply(content="", tool_calls=[tc(1, "register_evidence", {
            "evidence_id": f"ev-card-{ticker}", "chunk_id": "chk-0001",
            "verbatim_quote": "10-K"})]),
        AssistantReply(content="", tool_calls=[tc(2, "submit_card", {
            "one_liner": f"{ticker} 主业", "key_metrics": {"market_cap": "未知"},
            "highlights": ["亮点"], "risks": ["风险"], "richness": "B",
            "recommend": recommend, "reason": reason,
            "evidence_ids": [f"ev-card-{ticker}"]})]),
        AssistantReply(content="card done"),
    ]


# F1.5 thesis 脚本：登记证据 → 写 thesis 字段
THESIS_SCRIPT = [
    AssistantReply(content="", tool_calls=[tc(0, "query_demo", {})]),
    AssistantReply(content="", tool_calls=[tc(1, "register_evidence", {
        "evidence_id": "ev-th", "chunk_id": "chk-0001", "verbatim_quote": "10-K"})]),
    AssistantReply(content="", tool_calls=[tc(2, "propose_fact", {
        "field": "thesis", "value": "瓶颈在验证层。关键比率：BD 复购率。",
        "evidence_ids": ["ev-th"]})]),
    AssistantReply(content="thesis done"),
]

# F4.5 委员会脚本：strong_a（research 角色）承担 3 个视角各一次回复；CIO 一次
COMMITTEE_PERSPECTIVES_SCRIPT = [
    AssistantReply(content="商业模式：好生意。"),
    AssistantReply(content="行业竞争：格局清晰。"),
    AssistantReply(content="空头：最大风险是 X。"),
]
COMMITTEE_CIO_SCRIPT = [AssistantReply(content="## CIO 综合\n倾向：偏多。")]

F5_SCRIPT = [AssistantReply(content="## 摘要\nBE 领先。[ev-card-BE]\n## 对比矩阵\n……")]


def _approve_when_asked(events, approvals, plan=(None,)):
    """监听闸口并按 plan 顺序裁决：plan 元素 None=批准，字符串=打回带该反馈。
    单线程顺序处理多轮（防多监听线程抢同一 asked 事件的竞态）。"""

    def watch():
        handled = 0
        deadline = time.time() + 15
        while time.time() < deadline and handled < len(plan):
            asked = [e for e in events.read("live-s1") if e.type == "approval/asked"
                     and e.payload.get("detail", {}).get("op") == "industry_screen"]
            if len(asked) > handled:
                aid = asked[handled].payload["approval_id"]
                action = plan[handled]
                handled += 1
                if action is None:
                    approvals.decide(aid, True)
                else:
                    approvals.decide(aid, False, comment=action)
            time.sleep(0.02)

    threading.Thread(target=watch, daemon=True).start()


def _run_industry(deps, events, text="/industry AI for Science"):
    runner = CommandRunner(deps, approval_timeout_s=10)
    parsed = parse_command(text)
    command_id = runner.start(CommandRequest(session_run_id="live-s1", parsed=parsed))
    done = wait_for(
        events, "live-s1",
        lambda e: e.type == "command/done" and e.payload["command_id"] == command_id,
        timeout=30,
    )[0]
    return done


def test_parse_industry_theme():
    p = parse_command("/industry AI for Science 赛道")
    assert p is not None and p.name == "industry" and p.ticker == ""
    assert p.objective == "AI for Science 赛道"


def test_industry_funnel_end_to_end(tmp_path):
    """全链路：F1 行业档案 → F2 标的池（2 只）→ F3 闸口（BE 过）→ F4 深研 → F5 报告。"""
    scripts = {"research": [
        _industry_research_script(
            ["market_size", "growth_rate", "value_chain", "competition", "sub_sectors"]),
        THESIS_SCRIPT,
        _digger_script("ev-d1"),
        _card_script("BE", True, "质地好"),
        _card_script("RXRX", False, "估值太贵"),
        RESEARCH_SCRIPT,  # F4 深研 BE（round2 停滞 → stalled-with-progress，合法）
        COMMITTEE_PERSPECTIVES_SCRIPT,  # 委员会 strong_a 三视角
        COMMITTEE_CIO_SCRIPT,           # 委员会 CIO 综合
        F5_SCRIPT,
    ]}
    deps, events, _, approvals = make_deps(tmp_path, scripts)
    _approve_when_asked(events, approvals)
    done = _run_industry(deps, events)

    assert done.payload["outcome"] == "completed", done.payload["summary"]
    # F1：行业档案有内容
    view = deps.kb.view("industry", "ai-for-science", datetime.now(UTC))
    assert "sub_sectors" in view and "market_size" in view
    # F2：标的池落库且每票绑证据
    pool = view["player_landscape"].value
    assert {c["ticker"] for c in pool} == {"BE", "RXRX"}
    assert all(c["evidence_ids"] for c in pool)
    # F3：闸口通过名单只有 BE
    sr = [e for e in events.read("live-s1") if e.type == "industry/screen_result"]
    assert sr and sr[0].payload["approved_tickers"] == ["BE"]
    # F4：BE 档案有深研写入
    be = deps.kb.view("stock", "BE", datetime.now(UTC))
    assert "capacity" in be
    # F5：排序报告发布
    pub = [e for e in events.read("live-s1") if e.type == "report/published"]
    assert pub and pub[0].payload["kind"] == "industry_report"


def test_screen_gate_reject_with_feedback_iterates(tmp_path):
    """闸口打回带反馈 → 第二轮呈交带 feedback_addressed → 批准后通过。"""
    scripts = {"research": [
        _industry_research_script(
            ["market_size", "growth_rate", "value_chain", "competition", "sub_sectors"]),
        THESIS_SCRIPT,
        _digger_script("ev-d1"),
        _card_script("BE", True, "v1"),
        _card_script("RXRX", False, "v1"),
        _card_script("BE", True, "v2 已补现金流维度"),  # 第二轮重呈的新卡
        _card_script("RXRX", True, "v2 修正"),
        RESEARCH_SCRIPT,
        RESEARCH_SCRIPT,  # F4 两只都深研
        COMMITTEE_PERSPECTIVES_SCRIPT,  # 委员会 BE 三视角
        COMMITTEE_CIO_SCRIPT,
        COMMITTEE_PERSPECTIVES_SCRIPT,  # 委员会 RXRX 三视角
        COMMITTEE_CIO_SCRIPT,
        F5_SCRIPT,
    ]}
    deps, events, _, approvals = make_deps(tmp_path, scripts)

    asked_seen = []
    orig_request = approvals.request

    def spy(run_id, detail):
        if detail.get("op") == "industry_screen":
            asked_seen.append(detail)
        return orig_request(run_id, detail)

    approvals.request = spy  # type: ignore[assignment]
    _approve_when_asked(events, approvals, plan=("补充现金流维度", None))  # 打回→批准
    done = _run_industry(deps, events)

    assert done.payload["outcome"] == "completed", done.payload["summary"]
    assert len(asked_seen) == 2, "打回应触发第二轮呈交"
    assert asked_seen[1]["feedback_addressed"] == "补充现金流维度"
    # approval/decided 事件带 comment（打回反馈落事件可审计）
    decided = [e for e in events.read("live-s1") if e.type == "approval/decided"]
    assert decided[0].payload["comment"] == "补充现金流维度"


def _dup_digger_script() -> list:
    """同一公司两种港股写法（2228.HK / 02228.HK）同批提交——归一化后应合并为一票。"""
    return [
        AssistantReply(content="", tool_calls=[tc(0, "query_demo", {"ticker": "x"})]),
        AssistantReply(content="", tool_calls=[tc(1, "register_evidence", {
            "evidence_id": "ev-dup", "chunk_id": "chk-0001", "verbatim_quote": "10-K"})]),
        AssistantReply(content="", tool_calls=[tc(2, "propose_candidates", {"candidates": [
            {"ticker": "2228.HK", "name": "晶泰", "market": "HK", "sub_sector": "s",
             "one_liner": "x", "listed": True, "evidence_ids": ["ev-dup"]},
            {"ticker": "02228.HK", "name": "晶泰", "market": "HK", "sub_sector": "s",
             "one_liner": "x", "listed": True, "evidence_ids": ["ev-dup"]},
        ]})]),
        AssistantReply(content="dig done"),
    ]


def test_pool_ticker_normalization_merges_hk_duplicates(tmp_path):
    """F2 落库归一：2228.HK 与 02228.HK 合并为一票（02228.HK），证据并集。
    2026-09-02 验收残留：双胞胎代码并存导致同公司拆成两份档案/两张调研卡。"""
    from finance_agent.commands.steps import StepContext, step_candidate_pool

    scripts = {"research": [_dup_digger_script()]}
    deps, _, _, _ = make_deps(tmp_path, scripts)
    ctx = StepContext(
        command_id="cmd-n", session_run_id="live-s1",
        child_run_id="live-s1--cmd-n-2-candidate_pool",
        ticker="ai-for-science", objective="AI for Science", config="",
        should_cancel=lambda: False, entity_kind="industry",
    )
    result = step_candidate_pool(deps, ctx)
    assert result.status == "completed", result.summary
    view = deps.kb.view("industry", "ai-for-science", datetime.now(UTC))
    pool = view["player_landscape"].value
    hk = [c for c in pool if str(c["ticker"]).endswith(".HK")]
    assert len(hk) == 1 and hk[0]["ticker"] == "02228.HK", pool


def test_normalize_pool_ticker_unit():
    from finance_agent.commands.steps import _normalize_pool_ticker

    assert _normalize_pool_ticker("2228.HK") == "02228.HK"
    assert _normalize_pool_ticker("02228.HK") == "02228.HK"
    assert _normalize_pool_ticker("700.hk") == "00700.HK"  # 小写/短码同归一
    assert _normalize_pool_ticker(" BE ") == "BE"          # 美股不动（去空白/大写）
    assert _normalize_pool_ticker("ABSI") == "ABSI"


def test_screen_gate_reject_without_comment_blocks(tmp_path):
    """无反馈拒绝 → blocked（用户必须说清打回理由，否则终止）。"""
    scripts = {"research": [
        _industry_research_script(
            ["market_size", "growth_rate", "value_chain", "competition", "sub_sectors"]),
        THESIS_SCRIPT,
        _digger_script("ev-d1"),
        _card_script("BE", True, "v1"),
        _card_script("RXRX", False, "v1"),
    ]}
    deps, events, _, approvals = make_deps(tmp_path, scripts)

    def reject():
        deadline = time.time() + 10
        while time.time() < deadline:
            asked = [e for e in events.read("live-s1") if e.type == "approval/asked"
                     and e.payload.get("detail", {}).get("op") == "industry_screen"]
            if asked:
                approvals.decide(asked[0].payload["approval_id"], False)
                return
            time.sleep(0.02)

    threading.Thread(target=reject, daemon=True).start()
    done = _run_industry(deps, events)
    assert done.payload["outcome"] == "blocked"
    assert "打回" in done.payload["summary"]


def test_industry_usage_error_without_theme(tmp_path):
    deps, events, _, _ = make_deps(tmp_path, {})
    done = _run_industry(deps, events, "/industry")
    assert done.payload["outcome"] == "usage_error"


class TestStructuredFieldGuards:
    """2026-09-01 实测教训：player_landscape 被写成 JSON 字符串 → F3 读到 1491 个字符。"""

    def test_propose_player_landscape_rejects_string_value(self, tmp_path):
        # 写侧：字符串值被拒
        from finance_agent.llm.base import AssistantReply, ToolCall

        script = [
            AssistantReply(content="", tool_calls=[ToolCall(call_id="c0", name="query_demo", arguments={})]),
            AssistantReply(content="", tool_calls=[ToolCall(call_id="c1", name="register_evidence",
                arguments={"evidence_id": "ev-x", "chunk_id": "chk-0001", "verbatim_quote": "10-K"})]),
            AssistantReply(content="", tool_calls=[ToolCall(call_id="c2", name="propose_fact",
                arguments={"field": "player_landscape", "value": '[{"ticker": "BE"}]',  # JSON 字符串而非 list
                           "evidence_ids": ["ev-x"]})]),
            AssistantReply(content="done"),
        ]
        deps, events, kb, _ = make_deps(tmp_path, {"research": [script]})
        runner = CommandRunner(deps, approval_timeout_s=0.2)
        parsed = parse_command("/research industry:ai4s")
        cid = runner.start(CommandRequest(session_run_id="live-s1", parsed=parsed))
        wait_for(events, "live-s1",
                 lambda e: e.type == "command/done" and e.payload["command_id"] == cid)
        from datetime import UTC, datetime
        view = kb.view("industry", "ai4s", datetime.now(UTC))
        assert "player_landscape" not in view, "字符串形式的标的池必须被拒"

    def test_valid_pool_reader(self):
        from finance_agent.commands.steps import _valid_pool

        assert _valid_pool('[{"ticker": "BE"}]') == []  # 字符串不是池
        assert _valid_pool([{"ticker": "BE"}]) == [{"ticker": "BE"}]
        assert _valid_pool([{"no_ticker": 1}, "x", {"ticker": "RXRX"}]) == [{"ticker": "RXRX"}]
        assert _valid_pool(None) == []

"""leakage-audit hook：评估模式对模型输入投影的强制审计（防线的执行点）。"""

from datetime import UTC, datetime

import pytest

from finance_agent.eventstore.store import EventStore
from finance_agent.harness.manifest import RunManifest, RunMode
from finance_agent.llm.base import AssistantReply, ToolCall
from finance_agent.llm.mock import MockLLM
from finance_agent.loop.hooks import LeakageAuditHook, LeakageDetected
from finance_agent.loop.kernel import AgentKernel

T = datetime(2023, 6, 30, tzinfo=UTC)


def eval_manifest():
    return RunManifest(run_id="eval-1", mode=RunMode.EVAL, eval_as_of=T)


def tool_reply(content=""):
    return AssistantReply(content=content, tool_calls=[ToolCall(call_id="c1", name="t", arguments={})])


def run_with_tool(tmp_path, tool_fn, manifest):
    store = EventStore(tmp_path / "e.db")
    llm = MockLLM([tool_reply(), AssistantReply(content="done")])
    kernel = AgentKernel(
        store=store,
        llm=llm,
        manifest=manifest,
        tools={"t": tool_fn},
        hooks=[LeakageAuditHook(event_sink=store)],
    )
    return kernel, store


def test_eval_passes_with_pit_clean_provenance(tmp_path):
    kernel, store = run_with_tool(
        tmp_path,
        lambda args: {
            "content": "ok",
            "provenance": [
                {"source_id": "prices", "available_at": "2023-06-29T00:00:00+00:00", "pit_grade": "A"}
            ],
        },
        eval_manifest(),
    )
    assert kernel.run_turn("go") == "done"
    assert store.read("eval-1", types={"leakage/attempt"}) == []


def test_eval_rejects_tool_result_without_provenance(tmp_path):
    kernel, store = run_with_tool(tmp_path, lambda args: {"content": "裸数据，无溯源"}, eval_manifest())
    with pytest.raises(LeakageDetected):
        kernel.run_turn("go")
    leaks = store.read("eval-1", types={"leakage/attempt"})
    assert len(leaks) == 1 and leaks[0].payload["reason"] == "tool_result_missing_provenance"


def test_eval_rejects_future_provenance(tmp_path):
    """纵深防御：即使网关漏放了未来记录，hook 在模型调用前仍应拦截。"""
    kernel, store = run_with_tool(
        tmp_path,
        lambda args: {
            "content": "穿越数据",
            "provenance": [
                {"source_id": "prices", "available_at": "2023-07-15T00:00:00+00:00", "pit_grade": "A"}
            ],
        },
        eval_manifest(),
    )
    with pytest.raises(LeakageDetected):
        kernel.run_turn("go")
    assert store.read("eval-1", types={"leakage/attempt"})[0].payload["reason"] == "provenance_after_as_of"


def test_live_mode_does_not_require_provenance(tmp_path):
    kernel, store = run_with_tool(
        tmp_path, lambda args: {"content": "ok"}, RunManifest(run_id="live-1", mode=RunMode.LIVE)
    )
    assert kernel.run_turn("go") == "done"
    assert store.read("live-1", types={"leakage/attempt"}) == []

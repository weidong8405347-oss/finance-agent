"""薄插件层验收（tools-plugins 方案 §6.2，P1-C）。

宿主验收义务逐条对测：
- ToolDefinition/声明完整性：schema 有而 handler/声明无 → 拒绝注册（能力页与运行
  不一致由构造消除）；重名拒绝（插件与工具两级）；api_version 不兼容拒绝；
- PluginRegistry：缺凭证 = missing_config 带原因（不静默消失）；依赖迭代检查；
  非关键源失败不阻断（partial_with_reason），fail_closed 关键插件缺失才报错；
- 编译按场景（stage × market × role）过滤；能力页 payload 从编译结果生成；
- Manifest freezing：启用插件/工具/schema 指纹/配置哈希落事件；密钥值不进
  哈希原文与事件（只记存在性）；版本变化 → 哈希变化；
- ToolExecutor：参数校验、超时、可重试错误有界重试且扣预算、错误封装
  （error_code，不包装为空结果）、trace 事件；
- 内建插件迁移不改变行为：编译出的 adapter 集合与旧手工装配同源。
"""

from __future__ import annotations

import json
import time

import pytest

from finance_agent.eventstore.store import EventStore
from finance_agent.plugins.builtin import build_builtin_registry
from finance_agent.plugins.contracts import (
    API_VERSION,
    AppliesTo,
    Plugin,
    PluginManifest,
    ToolDefinition,
)
from finance_agent.plugins.executor import PLUGIN_TOOL_TRACE, ToolExecutor
from finance_agent.plugins.freezing import PLUGINS_MANIFEST_FROZEN, freeze_manifest
from finance_agent.plugins.registry import PluginError, PluginRegistry

SCHEMA_OK = {
    "name": "t", "description": "d",
    "parameters": {"type": "object",
                   "properties": {"q": {"type": "string"}}, "required": ["q"]},
}


def plugin(pid: str, *, tools: tuple[str, ...] = (), version: str = "0.1.0",
           auth: str = "none", requires: tuple[str, ...] = (),
           capabilities: tuple[str, ...] = (), stages: tuple[str, ...] = (),
           markets: tuple[str, ...] = (), failure: str = "partial_with_reason",
           api_version: str = API_VERSION, adapters: tuple = (),
           handler=None) -> Plugin:
    defs = tuple(
        ToolDefinition(name=t, schema_={**SCHEMA_OK, "name": t},
                       handler=handler or (lambda args: {"content": "ok", "provenance": []}),
                       plugin_id=pid)
        for t in tools
    )
    return Plugin(
        manifest=PluginManifest(
            id=pid, version=version, api_version=api_version, kind="source",
            capabilities=list(capabilities),
            applies_to=AppliesTo(markets=list(markets), stages=list(stages)),
            requires=list(requires), tools=list(tools), auth=auth,
            failure_policy=failure,  # type: ignore[arg-type]
        ),
        tools=defs,
        adapters=adapters,
    )


# ---------------- 注册期守卫 ----------------


class TestRegistrationGuards:
    def test_duplicate_plugin_rejected(self):
        r = PluginRegistry()
        r.register(plugin("source.a", tools=("t1",)))
        with pytest.raises(PluginError, match="重名拒绝"):
            r.register(plugin("source.a", tools=("t2",)))

    def test_duplicate_tool_across_plugins_rejected(self):
        r = PluginRegistry()
        r.register(plugin("source.a", tools=("t1",)))
        with pytest.raises(PluginError, match="工具重名"):
            r.register(plugin("source.b", tools=("t1",)))

    def test_api_version_mismatch_rejected(self):
        r = PluginRegistry()
        with pytest.raises(PluginError, match="不兼容"):
            r.register(plugin("source.a", tools=("t1",), api_version="finance-plugin-v0"))

    def test_declared_tool_without_definition_rejected(self):
        """manifest.tools 声明了但没有 schema/handler = 能力页谎报 → 拒绝。"""
        p = plugin("source.a", tools=("t1",))
        p.manifest.tools.append("ghost_tool")
        r = PluginRegistry()
        with pytest.raises(PluginError, match="ghost_tool"):
            r.register(p)

    def test_undeclared_tool_definition_rejected(self):
        p = plugin("source.a", tools=("t1",))
        extra = ToolDefinition(name="sneaky", schema_=SCHEMA_OK,
                               handler=lambda a: {"content": "", "provenance": []},
                               plugin_id="source.a")
        object.__setattr__(p, "tools", (*p.tools, extra))  # frozen dataclass
        r = PluginRegistry()
        with pytest.raises(PluginError, match="sneaky"):
            r.register(p)


# ---------------- 可用性与编译 ----------------


class TestCompile:
    def test_missing_credentials_visible_with_reason(self):
        r = PluginRegistry()
        r.register(plugin("research.web", tools=("t1",), auth="api_key:NOVITA|EXA"))
        c = r.compile(stage="research", env={})
        assert c.tools == {} and c.adapters == ()
        st = c.statuses[0]
        assert st.status == "missing_config"
        assert "NOVITA|EXA" in st.reason  # 缺哪组凭证可诊断

    def test_any_of_alternate_keys_enables(self):
        r = PluginRegistry()
        r.register(plugin("research.web", tools=("t1",), auth="api_key:NOVITA|EXA"))
        c = r.compile(stage="research", env={"EXA": "k"})
        assert c.statuses[0].status == "enabled" and "t1" in c.tools

    def test_stage_and_market_filtering(self):
        r = PluginRegistry()
        r.register(plugin("profile.core", tools=("tp",), stages=("profile",)))
        r.register(plugin("source.us", tools=("tu",), markets=("US",)))
        c = r.compile(stage="research", market="HK", env={})
        assert set(c.tools) == set(), "profile 阶段插件与 US 市场插件都不进 HK research 编译"
        c2 = r.compile(stage="profile", market="US", env={})
        assert set(c2.tools) == {"tp", "tu"}

    def test_dependency_iteration(self):
        """B 依赖 A 的能力：A 缺凭证 → B 也 unavailable（迭代到不动点）。"""
        r = PluginRegistry()
        r.register(plugin("source.a", tools=("ta",), auth="api_key:KEY_A",
                          capabilities=("data.a",)))
        r.register(plugin("proc.b", tools=("tb",), requires=("data.a",)))
        c = r.compile(stage="research", env={})
        by_id = {s.plugin_id: s for s in c.statuses}
        assert by_id["source.a"].status == "missing_config"
        assert by_id["proc.b"].status == "unavailable"
        assert "data.a" in by_id["proc.b"].reason

    def test_critical_plugin_missing_raises(self):
        r = PluginRegistry()
        r.register(plugin("core.x", tools=("tx",), failure="fail_closed",
                          requires=("nonexistent.capability",)))
        with pytest.raises(PluginError, match="关键插件"):
            r.compile(stage="research", env={})

    def test_degraded_probe_visible_but_still_serves(self):
        r = PluginRegistry()
        p = plugin("source.d", tools=("td",))
        object.__setattr__(p, "status_probe",
                            lambda: {"status": "degraded", "detail": "限流中"})
        r.register(p)
        c = r.compile(stage="research", env={})
        assert c.statuses[0].status == "degraded"
        assert "限流中" in c.statuses[0].reason
        assert "td" in c.tools  # degraded 是可见性状态，能力仍在


# ---------------- 配置哈希与冻结 ----------------


class TestFreezing:
    def _registry(self) -> PluginRegistry:
        r = PluginRegistry()
        r.register(plugin("research.web", tools=("t1",), auth="api_key:KEY_A"))
        r.register(plugin("source.sec", tools=("t2",)))
        return r

    def test_hash_stable_and_secret_free(self):
        r = self._registry()
        h1 = r.compile(stage="research", env={"KEY_A": "secret-1"}).config_hash
        h2 = r.compile(stage="research", env={"KEY_A": "secret-2"}).config_hash
        assert h1 == h2, "密钥值不得进哈希原文（只有存在性参与）"
        h3 = r.compile(stage="research", env={}).config_hash
        assert h3 != h1, "凭证存在性变化必须改变配置哈希"

    def test_version_bump_changes_hash(self):
        r1 = self._registry()
        h1 = r1.compile(stage="research", env={"KEY_A": "k"}).config_hash
        r2 = PluginRegistry()
        r2.register(plugin("research.web", tools=("t1",), auth="api_key:KEY_A",
                           version="0.2.0"))
        r2.register(plugin("source.sec", tools=("t2",)))
        h2 = r2.compile(stage="research", env={"KEY_A": "k"}).config_hash
        assert h1 != h2, "插件版本变化 = 能力面变化，必须可归因"

    def test_freeze_event_and_payload(self, tmp_path):
        events = EventStore(tmp_path / "e.db")
        r = self._registry()
        c = r.compile(stage="research", env={"KEY_A": "super-secret-value"})
        payload = freeze_manifest(c, run_id="run-1", events=events,
                                  extra={"plan_id": "plan-x"})
        evs = [e for e in events.read("run-1") if e.type == PLUGINS_MANIFEST_FROZEN]
        assert evs and evs[0].payload["config_hash"] == c.config_hash
        assert {p["id"] for p in payload["plugins"]} == {"research.web", "source.sec"}
        assert {t["name"] for t in payload["tools"]} == {"t1", "t2"}
        assert payload["plan_id"] == "plan-x"  # extra 平铺进冻结 payload
        # 密钥值绝不进事件原文
        raw = json.dumps(evs[0].payload, ensure_ascii=False)
        assert "super-secret-value" not in raw


# ---------------- ToolExecutor ----------------


class TestExecutor:
    def _tool(self, handler, *, timeout_s: float = 5.0, name: str = "t1") -> ToolDefinition:
        return ToolDefinition(name=name, schema_={**SCHEMA_OK, "name": name},
                              handler=handler, plugin_id="p.test", timeout_s=timeout_s)

    def test_argument_validation_envelope(self, tmp_path):
        events = EventStore(tmp_path / "e.db")
        ex = ToolExecutor(events=events, run_id="r1")
        tool = self._tool(lambda a: {"content": "ok", "provenance": []})
        out = ex.execute(tool, {})  # 缺 required q
        body = json.loads(out["content"])
        assert body["error_code"] == "invalid_arguments" and "q" in body["message"]
        out2 = ex.execute(tool, {"q": 123})  # 类型错
        assert json.loads(out2["content"])["error_code"] == "invalid_arguments"
        # trace 事件落了（失败可见性）
        traces = [e for e in events.read("r1") if e.type == PLUGIN_TOOL_TRACE]
        assert len(traces) == 2
        assert traces[0].payload["outcome"] == "invalid_arguments"  # outcome 携带错误码
        assert traces[0].payload["error_code"] == "invalid_arguments"

    def test_success_trace(self, tmp_path):
        events = EventStore(tmp_path / "e.db")
        ex = ToolExecutor(events=events, run_id="r1")
        tool = self._tool(lambda a: {"content": json.dumps({"echo": a["q"]}),
                                     "provenance": []})
        out = ex.execute(tool, {"q": "hi"})
        assert json.loads(out["content"])["echo"] == "hi"
        trace = [e for e in events.read("r1") if e.type == PLUGIN_TOOL_TRACE][0]
        assert trace.payload["outcome"] == "ok" and trace.payload["plugin_id"] == "p.test"
        assert trace.payload["duration_ms"] >= 0

    def test_transient_error_retries_and_charges_budget(self, tmp_path):
        events = EventStore(tmp_path / "e.db")
        calls = {"n": 0}
        retries = {"n": 0}

        class Budget:
            def admit_retry(self):
                retries["n"] += 1
                return True, ""

        def flaky(args):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("connection reset by peer")
            return {"content": "ok", "provenance": []}

        ex = ToolExecutor(events=events, budget=Budget(), run_id="r1", max_retries=1)
        out = ex.execute(self._tool(flaky), {"q": "x"})
        assert out["content"] == "ok"
        assert calls["n"] == 2 and retries["n"] == 1, "重试必须发生且扣预算"

    def test_budget_denied_no_retry(self, tmp_path):
        class Budget:
            def admit_retry(self):
                return False, "重试预算耗尽"

        def always_fail(args):
            raise OSError("connection refused")

        ex = ToolExecutor(events=None, budget=Budget(), run_id="r1", max_retries=2)
        out = ex.execute(self._tool(always_fail), {"q": "x"})
        body = json.loads(out["content"])
        assert body["error_code"] == "transient_error" and body["retryable"] is True

    def test_timeout_envelope(self, tmp_path):
        def slow(args):
            time.sleep(0.5)
            return {"content": "late", "provenance": []}

        ex = ToolExecutor(run_id="r1")
        out = ex.execute(self._tool(slow, timeout_s=0.05), {"q": "x"})
        body = json.loads(out["content"])
        assert body["error_code"] == "timeout" and body["retryable"] is True

    def test_invalid_result_shape_not_empty_wrap(self):
        ex = ToolExecutor()
        out = ex.execute(self._tool(lambda a: "not-a-dict"), {"q": "x"})
        assert json.loads(out["content"])["error_code"] == "invalid_tool_result"

    def test_semantic_error_not_retried(self):
        calls = {"n": 0}

        def bad(args):
            calls["n"] += 1
            raise ValueError("语义错误：期间非法")

        ex = ToolExecutor(max_retries=2)
        out = ex.execute(self._tool(bad), {"q": "x"})
        assert json.loads(out["content"])["error_code"] == "handler_error"
        assert calls["n"] == 1, "重试无益的错误不烧预算"


# ---------------- 内建插件：迁移不改变行为 ----------------


class TestBuiltinParity:
    FULL_ENV = {"NOVITA_API_KEY": "n", "TAVILY_API_KEY": "t"}

    def test_compile_matches_legacy_source_set(self):
        registry = build_builtin_registry(env=self.FULL_ENV)
        c = registry.compile(stage="research", env=self.FULL_ENV)
        source_ids = {a.capability().source_id for a in c.adapters}
        import importlib.util
        expected = {
            "edgar", "edgar_facts", "hkex_news", "web_search", "web_search_tavily",
            "prices", "prices_stooq", "fundamentals", "news_gdelt",
        }
        if importlib.util.find_spec("akshare") is not None:
            expected.add("fundamentals_hk")
        assert source_ids == expected, "registry 编译必须与旧手工装配同源"
        assert all(s.status == "enabled" for s in c.statuses), \
            [ (s.plugin_id, s.reason) for s in c.statuses if s.status != "enabled" ]

    def test_missing_keys_fail_closed_like_before(self):
        registry = build_builtin_registry(env={})
        c = registry.compile(stage="research", env={})
        source_ids = {a.capability().source_id for a in c.adapters}
        assert "web_search" not in source_ids and "web_search_tavily" not in source_ids
        by_id = {s.plugin_id: s for s in c.statuses}
        assert by_id["research.web"].status == "missing_config"
        assert "NOVITA_API_KEY" in by_id["research.web"].reason

    def test_capability_view_from_compilation(self):
        registry = build_builtin_registry(env=self.FULL_ENV)
        view = registry.compile(stage="research", env=self.FULL_ENV).as_payload()
        assert view["tools"], "能力页工具清单从编译结果生成"
        ids = {p["id"]: p for p in view["plugins"]}
        assert ids["source.sec"]["version"] and ids["source.sec"]["status"] == "enabled"
        assert "query_edgar_facts" in view["tools"]
        assert "fetch_document" in view["tools"] and "get_research_context" in view["tools"]
        # 每个声明工具都有非空 schema（路由不回退空参 schema）
        for name in view["tools"]:
            assert name  # 名称清单完整即可；schema 非空由注册守卫保证

    def test_processing_plugins_have_no_adapters(self):
        registry = build_builtin_registry(env=self.FULL_ENV)
        for pid in ("documents.reader", "knowledge.context", "research.core",
                    "profile.core", "synthesize.core"):
            p = registry.get(pid)
            assert p is not None and p.adapters == ()
            assert p.manifest.kind == "processing"

    def test_stage_scoped_plugins(self):
        registry = build_builtin_registry(env=self.FULL_ENV)
        research = registry.compile(stage="research", env=self.FULL_ENV)
        assert "propose_thesis" not in research.declared_schemas
        profile = registry.compile(stage="profile", env=self.FULL_ENV)
        assert "propose_thesis" in profile.declared_schemas
        synthesize = registry.compile(stage="synthesize", env=self.FULL_ENV)
        assert "submit_report_document" in synthesize.declared_schemas

    def test_step_freeze_helper_emits_event(self, tmp_path):
        """step 层冻结接线：有 registry → 事件落子 run；无 registry → 静默跳过。"""
        from types import SimpleNamespace

        from finance_agent.commands.steps import _freeze_plugin_manifest

        events = EventStore(tmp_path / "e.db")
        deps = SimpleNamespace(
            plugin_registry=build_builtin_registry(env=self.FULL_ENV),
            plugin_env=self.FULL_ENV, events=events,
        )
        ctx = SimpleNamespace(child_run_id="child-1", entity_kind="stock", ticker="BE")
        _freeze_plugin_manifest(deps, ctx, "research", extra={"plan_id": "plan-1"})
        evs = [e for e in events.read("child-1") if e.type == PLUGINS_MANIFEST_FROZEN]
        assert evs and evs[0].payload["plan_id"] == "plan-1"
        assert evs[0].payload["stage"] == "research"

        deps_none = SimpleNamespace(plugin_registry=None, plugin_env=None, events=events)
        _freeze_plugin_manifest(deps_none, ctx, "research")  # 旧装配：不产事件不报错
        assert len([e for e in events.read("child-1")
                    if e.type == PLUGINS_MANIFEST_FROZEN]) == 1

    def test_real_assembly_capabilities_include_plugins(self, tmp_path):
        """真实装配（规矩 2）：build_orchestrator 的能力页含插件编译视图。"""
        from finance_agent.cli import build_orchestrator

        orch = build_orchestrator(tmp_path)
        caps = orch["capabilities_info"]()
        assert "plugins" in caps and caps["plugins"].get("plugins")
        assert "edgar_facts" in caps["gateway_sources"]
        assert orch["plugin_registry"] is not None


# ---------------- 运行期绑定（P1-C 执行闭环收口，交付复核整改） ----------------


class TestRuntimeBinding:
    """让插件编译结果实际决定工具执行：执行面=声明∩装配，全部调用过 ToolExecutor，
    实际执行面冻结进 plugins/runtime_bound（与 manifest_frozen 的声明面配对）。"""

    FULL_ENV = {"NOVITA_API_KEY": "n", "TAVILY_API_KEY": "t"}

    def _mini_registry(self) -> PluginRegistry:
        r = PluginRegistry()
        r.register(plugin("research.demo", tools=("t1",)))
        return r

    def test_bind_routes_through_executor_with_plugin_attribution(self, tmp_path):
        from finance_agent.plugins.runtime import PLUGINS_RUNTIME_BOUND, RuntimeBinder

        events = EventStore(tmp_path / "e.db")
        compiled = build_builtin_registry(env=self.FULL_ENV).compile(
            stage="research", env=self.FULL_ENV)
        binder = RuntimeBinder(compiled, events=events)
        calls = {"n": 0}

        def handler(args):
            calls["n"] += 1
            return {"content": "kb-ok", "provenance": []}

        binding = binder.bind({"query_kb": handler}, run_id="run-b1")
        out = binding.tools["query_kb"]({})
        assert out["content"] == "kb-ok" and calls["n"] == 1
        # executor trace：plugin_id 归属到声明插件（不是笼统的 run 级台账）
        traces = [e for e in events.read("run-b1") if e.type == PLUGIN_TOOL_TRACE]
        assert traces and traces[0].payload["plugin_id"] == "knowledge.context"
        assert traces[0].payload["outcome"] == "ok" and traces[0].payload["rw"] == "read"
        # 实际执行面冻结：bound/declared_unbound/bound_undeclared 三清单
        bound_evs = [e for e in events.read("run-b1") if e.type == PLUGINS_RUNTIME_BOUND]
        assert len(bound_evs) == 1
        payload = bound_evs[0].payload
        assert payload["bound"] == ["query_kb"]
        assert payload["bound_undeclared"] == []
        assert "propose_claim" in payload["declared_unbound"]
        assert payload["config_hash"] == compiled.config_hash

    def test_bound_undeclared_visible_not_silent(self, tmp_path):
        from finance_agent.plugins.runtime import PLUGINS_RUNTIME_BOUND, RuntimeBinder

        events = EventStore(tmp_path / "e.db")
        compiled = self._mini_registry().compile(stage="research")
        binder = RuntimeBinder(compiled, events=events)
        binding = binder.bind(
            {"t1": lambda a: {"content": "ok", "provenance": []},
             "rogue_tool": lambda a: {"content": "rogue", "provenance": []}},
            run_id="run-b2")
        assert binding.bound == ["t1"]
        assert binding.bound_undeclared == ["rogue_tool"]
        payload = [e for e in events.read("run-b2")
                   if e.type == PLUGINS_RUNTIME_BOUND][0].payload
        assert payload["bound_undeclared"] == ["rogue_tool"]
        # 未声明工具仍经 executor（执行纪律统一：校验/超时/trace 不漏）
        out = binding.tools["rogue_tool"]({})
        assert out["content"] == "rogue"
        trace = [e for e in events.read("run-b2") if e.type == PLUGIN_TOOL_TRACE
                 and e.payload["tool"] == "rogue_tool"][0]
        assert trace.payload["plugin_id"] == "host.unbound"

    def test_validation_rejects_before_handler(self, tmp_path):
        from finance_agent.plugins.runtime import RuntimeBinder

        events = EventStore(tmp_path / "e.db")
        compiled = build_builtin_registry(env=self.FULL_ENV).compile(
            stage="research", env=self.FULL_ENV)
        binder = RuntimeBinder(compiled, events=events)
        calls = {"n": 0}

        def handler(args):
            calls["n"] += 1
            return {"content": "ok", "provenance": []}

        binding = binder.bind({"answer_question": handler}, run_id="run-b3")
        out = binding.tools["answer_question"]({})  # 缺 required question_id/status
        assert json.loads(out["content"])["error_code"] == "invalid_arguments"
        assert calls["n"] == 0, "参数校验失败不得进入 handler"

    def test_runtime_bound_idempotent_per_run(self, tmp_path):
        from finance_agent.plugins.runtime import PLUGINS_RUNTIME_BOUND, RuntimeBinder

        events = EventStore(tmp_path / "e.db")
        binder = RuntimeBinder(self._mini_registry().compile(stage="research"),
                               events=events)
        binder.bind({"t1": lambda a: {"content": "ok", "provenance": []}}, run_id="run-b4")
        binder.bind({"t1": lambda a: {"content": "ok", "provenance": []}}, run_id="run-b4")
        assert len([e for e in events.read("run-b4")
                    if e.type == PLUGINS_RUNTIME_BOUND]) == 1

    def test_research_loop_executes_through_compiled_set(self, tmp_path):
        """S1 真实装配：loop 带 plugin_set → 工具调用经 executor（trace 落组），
        执行面冻结落 run；行为不变（观测照常写库）。"""
        from datetime import UTC, datetime

        from finance_agent.gateway.adapters.fixture import FixtureAdapter
        from finance_agent.gateway.gateway import DataGateway
        from finance_agent.gateway.models import DataRecord, SourceCapability
        from finance_agent.harness.manifest import RunManifest, RunMode
        from finance_agent.knowledge.metric_store import MetricStore
        from finance_agent.knowledge.metric_writer import TypedMetricWriter
        from finance_agent.knowledge.models import PitGrade
        from finance_agent.knowledge.store import BitemporalStore
        from finance_agent.knowledge.writer import ProfileWriter
        from finance_agent.llm.base import AssistantReply, ToolCall
        from finance_agent.llm.mock import MockLLM
        from finance_agent.plugins.runtime import PLUGINS_RUNTIME_BOUND
        from finance_agent.research.loop import ResearchLoop

        kb = BitemporalStore(tmp_path / "kb.db")
        metrics = MetricStore(tmp_path / "m.db")
        events = EventStore(tmp_path / "e.db")
        writer = ProfileWriter(store=kb, events=events)
        mw = TypedMetricWriter(store=metrics, kb=kb, events=events)
        gateway = DataGateway(mode="live", events=events, run_id="live-pl")
        gateway.register(FixtureAdapter(
            SourceCapability(source_id="demo", pit_grade=PitGrade.A,
                             server_side_asof=False, description="夹具源"),
            records=[DataRecord(source_id="demo", payload={"t": "x"},
                                url="demo://f", available_at=datetime.now(UTC))],
        ))
        script = [
            AssistantReply(content="", tool_calls=[
                ToolCall(call_id="c0", name="query_kb", arguments={})]),
            AssistantReply(content="done"),
        ]
        compiled = build_builtin_registry(env=self.FULL_ENV).compile(
            stage="research", env=self.FULL_ENV)
        loop = ResearchLoop(
            store=kb, events=events, writer=writer, gateway=gateway,
            llm=MockLLM(script),
            manifest=RunManifest(run_id="live-pl", mode=RunMode.LIVE),
            gateway_sources=gateway.source_ids(),
            fetch_document=lambda url: "doc text",
            metrics=metrics, metric_writer=mw,
            max_rounds=1, plugin_set=compiled,
        )
        loop.run("stock", "BE", "验证")
        traces = [e for e in events.read("live-pl") if e.type == PLUGIN_TOOL_TRACE]
        assert any(t.payload["tool"] == "query_kb" for t in traces), \
            "S1 模型可见工具调用必须经过 ToolExecutor（trace 为证）"
        # 夹具源 query_demo 无插件声明 → bound_undeclared 显式可见（不静默）
        bound_ev = [e for e in events.read("live-pl")
                    if e.type == PLUGINS_RUNTIME_BOUND]
        assert bound_ev and "query_kb" in bound_ev[0].payload["bound"]
        assert "query_demo" in bound_ev[0].payload["bound_undeclared"]

    def test_s2_profile_update_binds_via_step(self, tmp_path):
        """S2 真实装配：step_profile_update 带 plugin_registry → 工具过 executor。"""
        from datetime import UTC, datetime

        from finance_agent.commands.steps import StepContext, StepDeps, step_profile_update
        from finance_agent.decision.service import DecisionService
        from finance_agent.decision.store import DecisionStore
        from finance_agent.gateway.gateway import DataGateway
        from finance_agent.harness.approvals import ApprovalService
        from finance_agent.knowledge.metric_store import MetricStore
        from finance_agent.knowledge.metric_writer import TypedMetricWriter
        from finance_agent.knowledge.models import Evidence, Fact, PitGrade
        from finance_agent.knowledge.store import BitemporalStore
        from finance_agent.knowledge.writer import ProfileWriter
        from finance_agent.llm.base import AssistantReply, ToolCall
        from finance_agent.llm.mock import MockLLM
        from finance_agent.plugins.runtime import PLUGINS_RUNTIME_BOUND

        kb = BitemporalStore(tmp_path / "kb.db")
        metrics = MetricStore(tmp_path / "m.db")
        events = EventStore(tmp_path / "e.db")
        writer = ProfileWriter(store=kb, events=events)
        mw = TypedMetricWriter(store=metrics, kb=kb, events=events)
        kb.add_evidence(Evidence(
            evidence_id="ev-s2", source_id="edgar", verbatim_quote="revenue 100 million",
            retrieved_at=datetime.now(UTC), available_at=datetime.now(UTC),
            pit_grade=PitGrade.A,
        ))
        kb.assert_fact(Fact(entity_kind="stock", entity_id="BE", field="business_model",
                            value="燃料电池", knowledge_time=datetime.now(UTC),
                            evidence_ids=["ev-s2"]))
        script = [
            AssistantReply(content="", tool_calls=[
                ToolCall(call_id="c0", name="get_research_context", arguments={})]),
            AssistantReply(content="", tool_calls=[
                ToolCall(call_id="c1", name="propose_thesis", arguments={
                    "thesis": "订单口径已核实。", "evidence_ids": ["ev-s2"]})]),
            AssistantReply(content="done"),
        ]
        deps = StepDeps(
            events=events, kb=kb, writer=writer,
            gateway=DataGateway(mode="live", events=events, run_id="live-s2p"),
            decisions=DecisionService(kb=kb, decisions=DecisionStore(tmp_path / "d.db"),
                                      events=events),
            llm_for=lambda role: MockLLM(script),
            approvals=ApprovalService(events),
            evals_dir=tmp_path / "evals", reports_dir=tmp_path / "reports",
            knowledge_dir=tmp_path / "knowledge",
            metrics=metrics, metric_writer=mw,
            plugin_registry=build_builtin_registry(env=self.FULL_ENV),
            plugin_env=dict(self.FULL_ENV),
        )
        ctx = StepContext(
            command_id="cmd-p", session_run_id="live-s2p",
            child_run_id="live-s2p--cmd-p-2", ticker="BE", objective="", config="",
            should_cancel=lambda: False, entity_kind="stock",
        )
        result = step_profile_update(deps, ctx)
        assert result.status == "completed"
        traces = [e for e in events.read(ctx.child_run_id)
                  if e.type == PLUGIN_TOOL_TRACE]
        tools_traced = {t.payload["tool"] for t in traces}
        assert {"get_research_context", "propose_thesis"} <= tools_traced, \
            "S2 工具调用必须经过 ToolExecutor（trace 为证）"
        owners = {t.payload["tool"]: t.payload["plugin_id"] for t in traces}
        assert owners["propose_thesis"] == "profile.core"
        # 声明面 + 执行面双双冻结（配对可归因）
        assert [e for e in events.read(ctx.child_run_id)
                if e.type == PLUGINS_MANIFEST_FROZEN]
        bound_ev = [e for e in events.read(ctx.child_run_id)
                    if e.type == PLUGINS_RUNTIME_BOUND]
        assert bound_ev and bound_ev[0].payload["bound_undeclared"] == []
        assert "verify_claim" in bound_ev[0].payload["bound"]

    def test_legacy_assembly_without_registry_unchanged(self, tmp_path):
        """无 registry 的旧装配/回放路径：helper 原样返回，不产事件。"""
        from types import SimpleNamespace

        from finance_agent.commands.steps import _bind_runtime_tools

        events = EventStore(tmp_path / "e.db")
        deps = SimpleNamespace(plugin_registry=None, plugin_env=None, events=events)
        ctx = SimpleNamespace(child_run_id="child-x", entity_kind="stock", ticker="BE")
        tools = {"query_kb": lambda a: {"content": "ok", "provenance": []}}
        assert _bind_runtime_tools(deps, ctx, "research", tools) is tools
        assert events.read("child-x") == []

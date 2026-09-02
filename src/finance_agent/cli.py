"""CLI 入口：python -m finance_agent research --ticker AAPL [--mock]

mock 模式：脚本化 LLM + 空数据源，验证装配（离线可跑）。
真实模式：EDGAR + 行情数据源 + env 配置的 LLM provider。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .eventstore.store import EventStore
from .gateway.gateway import DataGateway
from .harness.manifest import RunManifest, RunMode
from .knowledge.gaps import GapAnalyzer
from .knowledge.store import BitemporalStore
from .knowledge.writer import ProfileWriter
from .research.loop import ResearchLoop


def _build(
    tmp: Path, llm, *, sources: list[str], register_adapters: bool = False, fetch_document=None
) -> tuple[ResearchLoop, BitemporalStore, EventStore]:
    events = EventStore(tmp / "events.db")
    kb = BitemporalStore(tmp / "kb.db")
    writer = ProfileWriter(store=kb, events=events)
    gateway = DataGateway(mode="live", events=events, run_id="live-cli")
    if register_adapters:
        from .gateway.adapters.edgar import EdgarAdapter
        from .gateway.adapters.prices import YFinancePricesAdapter
        from .gateway.adapters.stooq import StooqPricesAdapter

        gateway.register(EdgarAdapter())
        gateway.register(YFinancePricesAdapter())
        gateway.register(StooqPricesAdapter())
    manifest = RunManifest(run_id="live-cli", mode=RunMode.LIVE)
    loop = ResearchLoop(
        store=kb,
        events=events,
        writer=writer,
        gateway=gateway,
        llm=llm,
        manifest=manifest,
        max_rounds=3,
        completeness_target=0.8,
        gateway_sources=sources,
        fetch_document=fetch_document,
    )
    return loop, kb, events


#: 代理固化只认这两个变量（与 handoff 的真实运行约定一致）
_PROXY_ENV_KEYS = ("HTTPS_PROXY", "HTTP_PROXY")


def _apply_dotenv_proxy(dotenv_path: str | Path = ".env") -> list[str]:
    """.env 代理固化（serve 启动时调用）：进程环境未显式设置代理变量时，从 .env 注入。

    显式环境变量优先（不覆盖；显式空串也算显式设置）。不在代码里探测系统代理——
    代理配置必须显式（环境变量或 .env），可复现、可审计。
    返回本次注入的变量名（启动横幅可见）。
    """
    from .llm.router import _read_dotenv  # 复用同一份 .env 解析（不写第二份）

    dotenv = _read_dotenv(str(dotenv_path))
    injected: list[str] = []
    for key in _PROXY_ENV_KEYS:
        if key not in os.environ and dotenv.get(key):
            os.environ[key] = dotenv[key]
            injected.append(key)
    return injected


def _router(data_dir: Path | None = None):
    """provider 配置来源（可测试接缝）：自有配置 > pi 配置 > .env 兜底。

    自有配置 = data_dir/llm-providers.json（前端可自配，§4.4）；每次调用重读文件，
    改配置即热生效，无需重启。
    """
    from .llm.router import LLMRouter

    if data_dir is not None:
        cfg = Path(data_dir) / "llm-providers.json"
        if cfg.exists():
            return LLMRouter.from_config(cfg)
    pi_dir = Path.home() / ".pi" / "agent"
    if (pi_dir / "models.json").exists() and (pi_dir / "auth.json").exists():
        return LLMRouter.from_pi()  # 复用 pi 配置（单一真相源）
    return LLMRouter.from_env()


def _all_tool_schemas() -> dict:
    """全部工具的 schema 合集（真实 provider 的 function calling 下发用）。"""
    from .commands.steps import PROFILE_TOOL_SCHEMAS
    from .decision.loop import DECISION_TOOL_SCHEMAS
    from .gateway.tools import GATEWAY_TOOL_SCHEMAS
    from .main_agent import MAIN_AGENT_TOOL_SCHEMAS
    from .research.tools import TOOL_SCHEMAS as RESEARCH_TOOL_SCHEMAS

    return {
        **RESEARCH_TOOL_SCHEMAS,
        **GATEWAY_TOOL_SCHEMAS,
        **MAIN_AGENT_TOOL_SCHEMAS,
        **PROFILE_TOOL_SCHEMAS,
        **DECISION_TOOL_SCHEMAS,
    }


def _real_llm(role: str = "research"):
    return _router().get(role, tool_schemas=_all_tool_schemas())


def _ensure_frontend_built(dist: Path) -> bool:
    """dist 缺失且本机有 npm 时自动构建（一条命令体验，参考 dsh web）。"""
    import shutil
    import subprocess

    if (dist / "index.html").exists():
        return True
    frontend_dir = dist.parent
    if not (frontend_dir / "package.json").exists() or shutil.which("npm") is None:
        return False
    print(f"[finance-agent] 前端未构建，自动执行 npm install && npm run build（{frontend_dir}）…")
    try:
        subprocess.run(["npm", "install", "--no-fund", "--no-audit"], cwd=frontend_dir, check=True)
        subprocess.run(["npm", "run", "build"], cwd=frontend_dir, check=True)
    except subprocess.CalledProcessError:
        return False
    return (dist / "index.html").exists()


def build_orchestrator(data_dir: Path):
    """真实装配（serve 与测试共用同一条路径——规矩 2）：
    EventStore/KB/Gateway/DecisionService + ChatService（主 agent）+ CommandRunner（command 派发）。

    LLM 与网络是唯一外部边界；provider 缺失在 turn/step 执行时以
    turn/error + step_agent/end(error) 三通道浮出（不静默）。
    """
    from .chat.service import ChatService
    from .commands.runner import CommandRunner
    from .commands.steps import StepDeps
    from .decision.service import DecisionService
    from .decision.store import DecisionStore
    from .gateway.adapters.edgar import EdgarAdapter, fetch_filing_text
    from .gateway.adapters.exa_search import ExaSearchAdapter
    from .gateway.adapters.fundamentals import AkshareHKFundamentalsAdapter, YFinanceFundamentalsAdapter
    from .gateway.adapters.gdelt import GdeltNewsAdapter
    from .gateway.adapters.prices import YFinancePricesAdapter
    from .gateway.adapters.stooq import StooqPricesAdapter
    from .harness.approvals import ApprovalService
    from .main_agent import MainAgent

    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    events = EventStore(data_dir / "events.db")
    kb = BitemporalStore(data_dir / "kb.db")
    writer = ProfileWriter(store=kb, events=events)
    gateway = DataGateway(mode="live", events=events, run_id="live-gateway")
    gateway.register(EdgarAdapter())
    gateway.register(YFinancePricesAdapter())
    gateway.register(StooqPricesAdapter())
    # P2 数据源扩展（research-capability-upgrade §4.5；全部走 gateway 纪律）
    gateway.register(YFinanceFundamentalsAdapter())  # C 级快照：美股基本面
    gateway.register(GdeltNewsAdapter())  # B 级：全球新闻含中文媒体
    from .gateway.adapters.hkexnews import HKEXNewsAdapter

    gateway.register(HKEXNewsAdapter())  # A 级：港股披露原文（spike 已验证端点）
    # 数据源 key 解析约定与 LLMRouter 一致：.env 打底、环境变量优先
    from .llm.router import _read_dotenv

    _dotenv = _read_dotenv()

    def _key(name: str) -> str | None:
        return os.environ.get(name) or _dotenv.get(name)

    exa = ExaSearchAdapter(api_key=_key("EXA_API_KEY"))
    if exa.configured:
        gateway.register(exa)  # B 级：web 语义搜索（定性维度命脉）
    else:
        print("[finance-agent] EXA_API_KEY 未配置：web 搜索源未注册（定性维度研究能力受限）")
    from .gateway.adapters.tavily import TavilySearchAdapter

    tavily = TavilySearchAdapter(api_key=_key("TAVILY_API_KEY"))
    if tavily.configured:
        gateway.register(tavily)  # C 级：Exa 的备份/并集搜索源
    else:
        print("[finance-agent] TAVILY_API_KEY 未配置：Tavily 搜索源未注册")
    import importlib.util

    if importlib.util.find_spec("akshare") is not None:
        gateway.register(AkshareHKFundamentalsAdapter())  # C 级：港股基本面快照
    else:
        print("[finance-agent] akshare 未安装（uv sync --extra data）：港股基本面源未注册")
    decisions = DecisionService(kb=kb, decisions=DecisionStore(data_dir / "decisions.db"), events=events)
    approvals = ApprovalService(events)
    evals_dir = data_dir / "evals"

    def llm_for(role: str):
        return _router(data_dir).get(role, tool_schemas=_all_tool_schemas())

    def worker_llm_for(n: int):
        """维度并行 worker 池（P3 §4.4）：n 个 flash LLM（三源轮转；未配置 → fast 兜底）。"""
        router = _router(data_dir)
        schemas = _all_tool_schemas()
        out = []
        for i in range(n):
            role = f"research-worker-{(i % 3) + 1}"
            if not router.has_role(role):
                role = "fast"
            out.append(router.get(role, tool_schemas=schemas))
        return out

    def eval_runner(*, config_name: str, child_run_id: str):
        """S4 独立效果评估的真实装配（mandate 文件 → ReplayEngine）。"""
        from .evaluation.config import EvalConfig
        from .evaluation.prices import PriceBook
        from .evaluation.replay import ReplayEngine

        cfg = EvalConfig.from_json(evals_dir / "mandates" / f"{config_name}.json")
        # 对账行情：按回退次序装配；全缺 → 显式失败（空 PriceBook 会把预算烧成全 incomplete）
        price_book = PriceBook.from_gateway(gateway, cfg.tickers, require=True)

        def gateway_factory(as_of, run_id):
            g = DataGateway(
                mode="eval", eval_as_of=as_of, allow_pit_b=cfg.allow_pit_b,
                events=events, run_id=run_id,
            )
            g.register(EdgarAdapter())
            g.register(YFinancePricesAdapter())
            g.register(StooqPricesAdapter())
            return g

        engine = ReplayEngine(
            kb=kb,
            events=events,
            decision_service=decisions,
            llm_agent=llm_for("research"),
            llm_baseline=llm_for("fast"),
            price_book=price_book,
            artifacts_dir=evals_dir,
            gateway_factory=gateway_factory,
            fetch_document=fetch_filing_text,  # eval 研究同样需要读申报正文（filing 不可变，PIT 安全）
        )
        report = engine.run(cfg, eval_run_id=child_run_id)
        return {
            "verdict": report.verdict,
            "eval_run_id": report.eval_run_id,
            "mean_net_return": report.aggregate.mean_net_return,
            "kb_delta": report.aggregate.kb_delta,
            "leakage_events": report.leakage_events,
            "n_complete": report.aggregate.n_complete,
            "hit_rate": report.aggregate.hit_rate,
            "report_path": str(evals_dir / report.eval_run_id / "report.json"),
        }

    deps = StepDeps(
        events=events,
        kb=kb,
        writer=writer,
        gateway=gateway,
        decisions=decisions,
        llm_for=llm_for,
        worker_llm_for=worker_llm_for,
        approvals=approvals,
        evals_dir=evals_dir,
        reports_dir=data_dir / "reports",
        knowledge_dir=data_dir / "knowledge",  # 档案 HTML 存档（自包含于数据目录）
        eval_runner=eval_runner,
        fetch_document=fetch_filing_text,
    )
    command_runner = CommandRunner(deps)  # wake 在 chat_service 建成后接线
    knowledge_dir = data_dir / "knowledge"

    def make_main_agent(run_id: str) -> MainAgent:
        return MainAgent(
            run_id=run_id, events=events, kb=kb, gateway=gateway,
            llm=llm_for("research"), commands=command_runner,
            evals_dir=evals_dir,  # show_eval_config（评估配置对话式微调的「读出」一步）
        )

    chat_service = ChatService(events=events, make_main_agent=make_main_agent)
    command_runner.set_wake(chat_service.wake)

    def capabilities_info() -> dict:
        """能力目录的动态部分：模型名与数据源（装配时已知，查询时读最新配置）。"""
        models: dict[str, str] = {}
        try:
            router = _router(data_dir)
            for role in ("research", "fast"):
                try:
                    llm = router.get(role)
                    name = getattr(llm, "model_name", "?")
                    effort = getattr(getattr(llm, "spec", None), "effort", None)
                    timeout = getattr(llm, "_timeout", None)  # noqa: SLF001 - 展示用
                    models[role] = name + (f"@{effort}" if effort else "") + (
                        f"（{timeout:.0f}s）" if timeout else ""
                    )
                except Exception:
                    models[role] = "未配置"
        except Exception:
            pass
        return {"models": models, "gateway_sources": gateway.source_ids()}

    return {
        "events": events,
        "kb": kb,
        "decisions": decisions,
        "approvals": approvals,
        "chat_service": chat_service,
        "command_runner": command_runner,
        "evals_dir": evals_dir,
        "knowledge_dir": knowledge_dir,
        "reports_dir": data_dir / "reports",
        "capabilities_info": capabilities_info,
    }


def _serve(data_dir: Path, host: str, port: int, *, open_browser: bool, auto_build: bool) -> int:
    import uvicorn

    from .api.app import create_app
    from .logging_setup import mirror_events_to_logging, setup_logging

    logger = setup_logging()
    injected = _apply_dotenv_proxy()
    if injected:
        print(f"[finance-agent] 代理固化：从 .env 注入 {', '.join(injected)}（显式环境变量优先）")
    repo_root = Path(__file__).resolve().parents[2]
    dist = repo_root / "frontend" / "dist"
    if auto_build:
        _ensure_frontend_built(dist)

    url = f"http://{host}:{port}"
    print(f"[finance-agent] 数据目录: {data_dir}")
    print(f"[finance-agent] 服务启动: {url}")
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    orch = build_orchestrator(data_dir)
    mirror_events_to_logging(orch["events"], logger)  # 错误类事件自动镜像到日志（规矩 1 通道 2）

    app = create_app(
        kb=orch["kb"],
        events=orch["events"],
        decisions=orch["decisions"].decisions,
        evals_dir=orch["evals_dir"],
        chat_service=orch["chat_service"],
        command_runner=orch["command_runner"],
        approvals=orch["approvals"],
        static_dir=dist,
        knowledge_dir=orch["knowledge_dir"],
        reports_dir=orch["reports_dir"],
        capabilities_info=orch["capabilities_info"],
        data_dir=data_dir,
        router_factory=lambda: _router(data_dir),  # P5 自配页：有效配置视图（每次新建=热生效）
    )
    uvicorn.run(app, host=host, port=port)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="finance-agent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    research = sub.add_parser("research", help="对标的跑迭代研究（生产模式）")
    research.add_argument("--ticker", required=True)
    research.add_argument("--mock", action="store_true", help="离线演示（脚本化 LLM）")
    research.add_argument("--data-dir", default=None)
    serve = sub.add_parser("serve", help="启动服务（API + UI 同源，一条命令）")
    serve.add_argument("--data-dir", default="./data")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    serve.add_argument("--no-build", action="store_true", help="不自动构建前端")
    args = parser.parse_args(argv)

    if args.cmd == "serve":
        return _serve(
            Path(args.data_dir), args.host, args.port,
            open_browser=not args.no_open, auto_build=not args.no_build,
        )

    if args.cmd == "research":
        if args.mock:
            from .llm.base import AssistantReply, ToolCall
            from .llm.mock import MockLLM

            # mock：query_demo 返回一条 filing 记录（chunk chk-0001），read_edgar_filing
            # 抓回正文窗口（chk-0002），证据从窗口逐字摘录——与真实路径同一纪律。
            llm = MockLLM(
                [
                    AssistantReply(
                        content="",
                        tool_calls=[ToolCall(call_id="c0", name="query_demo", arguments={})],
                    ),
                    AssistantReply(
                        content="",
                        tool_calls=[
                            ToolCall(
                                call_id="c1",
                                name="read_edgar_filing",
                                arguments={"chunk_id": "chk-0001", "query": "revenue"},
                            )
                        ],
                    ),
                    AssistantReply(
                        content="",
                        tool_calls=[
                            ToolCall(
                                call_id="c2",
                                name="register_evidence",
                                arguments={
                                    "evidence_id": "ev-demo",
                                    "chunk_id": "chk-0002",
                                    "verbatim_quote": "demo revenue 100 in fy2024",
                                },
                            )
                        ],
                    ),
                    AssistantReply(
                        content="",
                        tool_calls=[
                            ToolCall(
                                call_id="c3",
                                name="propose_fact",
                                arguments={
                                    "field": "business_model",
                                    "value": "demo revenue 100 in fy2024",
                                    "evidence_ids": ["ev-demo"],
                                },
                            )
                        ],
                    ),
                    AssistantReply(content="demo round done"),
                    # round 2：无新发现 → 停滞收敛
                    AssistantReply(content="no new findings"),
                ]
            )
            sources = ["demo"]
            fetch_document = lambda url: "demo revenue 100 in fy2024. demo business model."  # noqa: E731
        else:
            from .llm.router import ProviderConfigError

            try:
                llm = _real_llm()
            except ProviderConfigError as e:
                print(
                    f"未配置 LLM provider：{e}。请在项目根目录 .env 配置三件套"
                    "（OPENAI_API_KEY/OPENAI_BASE_URL/OPENAI_MODEL），或配置 pi 的 ~/.pi/agent/",
                    file=sys.stderr,
                )
                return 2
            sources = ["edgar", "prices", "prices_stooq"]
            from .gateway.adapters.edgar import fetch_filing_text

            fetch_document = fetch_filing_text

        data_dir = Path(args.data_dir or tempfile.mkdtemp(prefix="finance-agent-"))
        if args.mock:
            from .gateway.adapters.fixture import FixtureAdapter
            from .gateway.models import DataRecord, SourceCapability
            from .knowledge.models import PitGrade

            loop, kb, _events = _build(
                data_dir, llm, sources=sources, register_adapters=False,
                fetch_document=fetch_document,
            )
            loop._gateway.register(  # noqa: SLF001 - mock 演示夹具
                FixtureAdapter(
                    SourceCapability(
                        source_id="demo", pit_grade=PitGrade.C,
                        server_side_asof=False, description="mock 演示源",
                    ),
                    records=[
                        DataRecord(
                            source_id="demo",
                            payload={"form": "10-K", "accession": "demo-1"},
                            url="demo://filing",
                        )
                    ],
                )
            )
        else:
            loop, kb, _events = _build(
                data_dir, llm, sources=sources, register_adapters=True,
                fetch_document=fetch_document,
            )
        reports = loop.run("stock", args.ticker.upper(), objective=f"深度研究 {args.ticker.upper()}")
        gaps = GapAnalyzer(kb).analyze("stock", args.ticker.upper(), datetime.now(UTC))
        print(json.dumps({
            "stop_reason": loop.stop_reason,
            "rounds": [r.model_dump(mode="json") for r in reports],
            "final_gaps": gaps.model_dump(mode="json"),
            "data_dir": str(data_dir),
        }, ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())

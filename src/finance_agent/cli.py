"""CLI 入口：python -m finance_agent research --ticker AAPL [--mock]

mock 模式：脚本化 LLM + 空数据源，验证装配（离线可跑）。
真实模式：EDGAR + 行情数据源 + env 配置的 LLM provider。
"""

from __future__ import annotations

import argparse
import json
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

        gateway.register(EdgarAdapter())
        gateway.register(YFinancePricesAdapter())
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


def _router():
    """provider 配置来源（可测试接缝）：pi 配置优先，.env 兜底。"""
    from .llm.router import LLMRouter

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
    from .gateway.adapters.prices import YFinancePricesAdapter
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
    decisions = DecisionService(kb=kb, decisions=DecisionStore(data_dir / "decisions.db"), events=events)
    approvals = ApprovalService(events)
    evals_dir = data_dir / "evals"

    def llm_for(role: str):
        return _router().get(role, tool_schemas=_all_tool_schemas())

    def eval_runner(*, config_name: str, child_run_id: str):
        """S4 独立效果评估的真实装配（mandate 文件 → ReplayEngine）。"""
        from .evaluation.config import EvalConfig
        from .evaluation.prices import PriceBook
        from .evaluation.replay import ReplayEngine

        cfg = EvalConfig.from_json(evals_dir / "mandates" / f"{config_name}.json")
        records = []
        for t in cfg.tickers:
            records += [
                r.payload
                for r in gateway.query("prices", {"ticker": t, "start": "2000-01-01", "end": "2100-01-01"})
            ]

        def gateway_factory(as_of, run_id):
            g = DataGateway(
                mode="eval", eval_as_of=as_of, allow_pit_b=cfg.allow_pit_b,
                events=events, run_id=run_id,
            )
            g.register(EdgarAdapter())
            g.register(YFinancePricesAdapter())
            return g

        engine = ReplayEngine(
            kb=kb,
            events=events,
            decision_service=decisions,
            llm_agent=llm_for("research"),
            llm_baseline=llm_for("fast"),
            price_book=PriceBook.from_records(records),
            artifacts_dir=evals_dir,
            gateway_factory=gateway_factory,
        )
        report = engine.run(cfg, eval_run_id=child_run_id)
        return {"verdict": report.verdict, "eval_run_id": report.eval_run_id}

    deps = StepDeps(
        events=events,
        kb=kb,
        writer=writer,
        gateway=gateway,
        decisions=decisions,
        llm_for=llm_for,
        approvals=approvals,
        evals_dir=evals_dir,
        reports_dir=data_dir / "reports",
        eval_runner=eval_runner,
        fetch_document=fetch_filing_text,
    )
    command_runner = CommandRunner(deps)  # wake 在 chat_service 建成后接线

    def make_main_agent(run_id: str) -> MainAgent:
        return MainAgent(
            run_id=run_id, events=events, kb=kb, gateway=gateway,
            llm=llm_for("research"), commands=command_runner,
        )

    chat_service = ChatService(events=events, make_main_agent=make_main_agent)
    command_runner.set_wake(chat_service.wake)
    return {
        "events": events,
        "kb": kb,
        "decisions": decisions,
        "approvals": approvals,
        "chat_service": chat_service,
        "command_runner": command_runner,
        "evals_dir": evals_dir,
    }


def _serve(data_dir: Path, host: str, port: int, *, open_browser: bool, auto_build: bool) -> int:
    import uvicorn

    from .api.app import create_app
    from .logging_setup import mirror_events_to_logging, setup_logging

    logger = setup_logging()
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
            sources = ["edgar", "prices"]
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

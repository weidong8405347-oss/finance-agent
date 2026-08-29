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

from .eventstore.events import Event
from .eventstore.store import EventStore
from .gateway.gateway import DataGateway
from .harness.manifest import RunManifest, RunMode
from .knowledge.gaps import GapAnalyzer
from .knowledge.store import BitemporalStore
from .knowledge.writer import ProfileWriter
from .research.loop import ResearchLoop


def _build(
    tmp: Path, llm, *, sources: list[str], register_adapters: bool = False
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
    )
    return loop, kb, events


GATEWAY_TOOL_SCHEMAS = {
    "query_edgar": {
        "name": "query_edgar",
        "description": "查询 SEC EDGAR 披露（filingDate 为 PIT 可知时刻）",
        "parameters": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "forms": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["ticker"],
        },
    },
    "query_prices": {
        "name": "query_prices",
        "description": "查询日线行情（available_at = 交易日 +1d）",
        "parameters": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
            },
            "required": ["ticker"],
        },
    },
}


def _router():
    """provider 配置来源（可测试接缝）：pi 配置优先，.env 兜底。"""
    from .llm.router import LLMRouter

    pi_dir = Path.home() / ".pi" / "agent"
    if (pi_dir / "models.json").exists() and (pi_dir / "auth.json").exists():
        return LLMRouter.from_pi()  # 复用 pi 配置（单一真相源）
    return LLMRouter.from_env()


def _real_llm(role: str = "research"):
    from .research.tools import TOOL_SCHEMAS

    return _router().get(role, tool_schemas={**TOOL_SCHEMAS, **GATEWAY_TOOL_SCHEMAS})


def make_decision_runner(data_dir: Path):
    """生产决策 runner（真实装配）。失败三通道：事件 + 日志 +（API 投影）。"""

    def decision_runner(run_id, ticker, events):
        from .decision.loop import DecisionLoop
        from .decision.service import DecisionService
        from .decision.store import DecisionStore
        from .logging_setup import setup_logging

        logger = setup_logging()
        try:
            kb = BitemporalStore(data_dir / "kb.db")
            svc = DecisionService(
                kb=kb, decisions=DecisionStore(data_dir / "decisions.db"), events=events
            )
            manifest = RunManifest(run_id=run_id, mode=RunMode.LIVE)
            card_id = DecisionLoop(
                kb=kb, events=events, decision_service=svc, llm=_real_llm(), manifest=manifest
            ).run("stock", ticker.upper())
            events.append(
                Event(run_id=run_id, type="decision/completed", payload={"card_id": card_id})
            )
        except Exception as e:
            logger.error("decision failed run=%s: %s", run_id, e)
            events.append(Event(run_id=run_id, type="decision/error", payload={"reason": str(e)}))

    return decision_runner


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


def make_research_runner(data_dir: Path):
    """生产研究 runner（真实装配）。LLM 与网络是唯一外部边界。

    失败纪律（RCA 规矩 1 三通道）：事件落库 + 日志输出 +（由 API 层投影）用户可见。
    """
    from .logging_setup import setup_logging

    logger = setup_logging()

    def research_runner(run_id, ticker, objective, events):
        from .gateway.adapters.edgar import EdgarAdapter
        from .gateway.adapters.prices import YFinancePricesAdapter

        kb = BitemporalStore(data_dir / "kb.db")
        writer = ProfileWriter(store=kb, events=events)
        gateway = DataGateway(mode="live", events=events, run_id=run_id)
        gateway.register(EdgarAdapter())
        gateway.register(YFinancePricesAdapter())
        manifest = RunManifest(run_id=run_id, mode=RunMode.LIVE)
        try:
            llm = _real_llm()
            loop = ResearchLoop(
                store=kb, events=events, writer=writer, gateway=gateway, llm=llm,
                manifest=manifest, max_rounds=3, completeness_target=0.8,
                gateway_sources=gateway.source_ids(),
            )
            reports = loop.run("stock", ticker.upper(), objective or f"深度研究 {ticker.upper()}")
        except Exception as e:  # 失败不得静默：事件 + 日志（API 层负责状态投影）
            logger.error("research failed run=%s: %s", run_id, e)
            events.append(
                Event(run_id=run_id, type="research/error", payload={"reason": str(e)})
            )
            return
        events.append(
            Event(
                run_id=run_id,
                type="research/completed",
                payload={
                    "rounds": len(reports),
                    "stop_reason": loop.stop_reason,
                    "completeness": reports[-1].completeness_after if reports else 0.0,
                },
            )
        )
        logger.info("research completed run=%s rounds=%d", run_id, len(reports))

    return research_runner


def _research_preflight() -> str | None:
    """研究前置校验：无 provider → 返回可操作指引（422 快速失败，不静默）。"""
    from .llm.router import ProviderConfigError

    try:
        _real_llm()
    except ProviderConfigError as e:
        return (
            f"未配置 LLM provider（{e}）。请在项目根目录 .env 配置三件套，"
            "例如：OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL"
        )
    return None


def _serve(data_dir: Path, host: str, port: int, *, open_browser: bool, auto_build: bool) -> int:
    import uvicorn

    from .api.app import create_app
    from .decision.store import DecisionStore
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

    data_dir.mkdir(parents=True, exist_ok=True)

    events = EventStore(data_dir / "events.db")
    mirror_events_to_logging(events, logger)  # 错误类事件自动镜像到日志（规矩 1 通道 2）

    app = create_app(
        kb=BitemporalStore(data_dir / "kb.db"),
        events=events,
        decisions=DecisionStore(data_dir / "decisions.db"),
        evals_dir=data_dir / "evals",
        research_runner=make_research_runner(data_dir),
        decision_runner=make_decision_runner(data_dir),
        research_preflight=_research_preflight,
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

            llm = MockLLM(
                [
                    # round 1：登记证据 + 写入一个非数值字段
                    AssistantReply(
                        content="",
                        tool_calls=[
                            ToolCall(
                                call_id="c1",
                                name="register_evidence",
                                arguments={
                                    "evidence_id": "ev-demo",
                                    "source_id": "demo",
                                    "verbatim_quote": "demo evidence",
                                    "pit_grade": "C",
                                },
                            )
                        ],
                    ),
                    AssistantReply(
                        content="",
                        tool_calls=[
                            ToolCall(
                                call_id="c2",
                                name="propose_fact",
                                arguments={
                                    "field": "business_model",
                                    "value": "demo 商业模式",
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
            sources: list[str] = []
        else:
            problem = _research_preflight()
            if problem is not None:
                print(problem, file=sys.stderr)
                return 2
            llm = _real_llm()
            sources = ["edgar", "prices"]

        data_dir = Path(args.data_dir or tempfile.mkdtemp(prefix="finance-agent-"))
        loop, kb, _events = _build(data_dir, llm, sources=sources, register_adapters=not args.mock)
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

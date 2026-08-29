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


def _build(tmp: Path, llm, *, sources: list[str]) -> tuple[ResearchLoop, BitemporalStore, EventStore]:
    events = EventStore(tmp / "events.db")
    kb = BitemporalStore(tmp / "kb.db")
    writer = ProfileWriter(store=kb, events=events)
    gateway = DataGateway(mode="live", events=events, run_id="live-cli")
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


def _real_llm():
    from .llm.router import LLMRouter

    return LLMRouter.from_env().get("research")


def _serve(data_dir: Path, host: str, port: int) -> int:
    import uvicorn

    from .api.app import create_app
    from .decision.store import DecisionStore

    data_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(
        kb=BitemporalStore(data_dir / "kb.db"),
        events=EventStore(data_dir / "events.db"),
        decisions=DecisionStore(data_dir / "decisions.db"),
        evals_dir=data_dir / "evals",
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
    serve = sub.add_parser("serve", help="启动 API 服务（UI 投影层）")
    serve.add_argument("--data-dir", default="./data")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    if args.cmd == "serve":
        return _serve(Path(args.data_dir), args.host, args.port)

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
            llm = _real_llm()
            sources = ["edgar", "prices"]

        data_dir = Path(args.data_dir or tempfile.mkdtemp(prefix="finance-agent-"))
        loop, kb, _events = _build(data_dir, llm, sources=sources)
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

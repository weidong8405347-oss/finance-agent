"""首批内建插件（方案 §6.4「先包装现有能力，再接少量高价值 API/MCP」）。

纪律：
- 这一层**不改变行为**——source 插件贡献的 adapter 与今天手工装配的完全同一批；
  processing 插件声明的工具 schema 与运行装配同源（同一份 dict 对象）；
- 内建强约束（时间准入/证据绑定/MetricSpec/单写者/快照隔离）留在宿主，
  manifest 里没有任何可以关闭它们的字段；
- 缺凭证 = missing_config（能力页可见原因），不静默消失；非关键源失败不阻断研究。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .contracts import AppliesTo, Plugin, PluginManifest
from .registry import PluginRegistry

#: 插件版本（冻结进 manifest/config_hash；行为变更必须升版本）
_V = "0.1.0"


def _schemas() -> dict[str, dict[str, dict]]:
    """工具 schema 的真相源（与路由绑定同一对象，避免抄写漂移）。"""
    from ..commands.steps import PROFILE_TOOL_SCHEMAS, SYNTHESIZE_TOOL_SCHEMAS
    from ..gateway.tools import GATEWAY_TOOL_SCHEMAS
    from ..research.context_tools import CONTEXT_TOOL_SCHEMAS
    from ..research.tools import TOOL_SCHEMAS as RESEARCH_TOOL_SCHEMAS

    return {
        "gateway": GATEWAY_TOOL_SCHEMAS,
        "research": RESEARCH_TOOL_SCHEMAS,
        "context": CONTEXT_TOOL_SCHEMAS,
        "profile": PROFILE_TOOL_SCHEMAS,
        "synthesize": SYNTHESIZE_TOOL_SCHEMAS,
    }


def build_builtin_registry(env: Mapping[str, str] | None = None) -> PluginRegistry:
    """构造并注册全部内建插件。

    env：凭证可见性来源（.env 打底、进程环境优先——与 cli 的 _key 约定一致）；
    adapter 构造用显式 key 注入（不依赖 os.environ 的隐式状态）。
    """
    import importlib.util

    from ..gateway.adapters.edgar import EdgarAdapter
    from ..gateway.adapters.edgar_facts import EdgarFactsAdapter
    from ..gateway.adapters.exa_search import ExaSearchAdapter
    from ..gateway.adapters.fundamentals import (
        AkshareHKFundamentalsAdapter,
        YFinanceFundamentalsAdapter,
    )
    from ..gateway.adapters.gdelt import GdeltNewsAdapter
    from ..gateway.adapters.hkexnews import HKEXNewsAdapter
    from ..gateway.adapters.prices import YFinancePricesAdapter
    from ..gateway.adapters.stooq import StooqPricesAdapter
    from ..gateway.adapters.tavily import TavilySearchAdapter

    _akshare_ok = importlib.util.find_spec("akshare") is not None

    environ: Mapping[str, str] = env if env is not None else {}
    s = _schemas()
    registry = PluginRegistry()

    def reg(
        *, id: str, kind: str, tools: list[str], schemas: dict[str, dict],
        capabilities: list[str], version: str = _V, markets: list[str] | None = None,
        stages: list[str] | None = None, requires: list[str] | None = None,
        temporal: str = "unknown", auth: str = "none", network: str = "local",
        failure: str = "partial_with_reason", tests: str = "",
        adapters: tuple[Any, ...] = (),
        status_probe: Any | None = None,
    ) -> None:
        registry.register(Plugin(
            manifest=PluginManifest(
                id=id, version=version, kind=kind,  # type: ignore[arg-type]
                capabilities=capabilities,
                applies_to=AppliesTo(markets=markets or [], stages=stages or []),
                requires=requires or [], tools=tools,
                temporal_policy=temporal, auth=auth, network_policy=network,
                failure_policy=failure,  # type: ignore[arg-type]
                contract_tests=tests,
            ),
            tool_schemas={name: schemas[name] for name in tools if name in schemas},
            adapters=adapters,
            status_probe=status_probe,
        ))

    # ---------------- 数据插件（source）：与现有手工装配同一批 adapter ----------------

    reg(
        id="source.sec", kind="source", version="0.2.0",
        tools=["query_edgar"], schemas=s["gateway"],
        capabilities=["filings.search", "documents.fetch"],
        markets=["US"], temporal="per_record_verified",
        auth="configured_user_agent", network="sec_endpoints",
        tests="tests/unit/test_edgar_facts.py",
        adapters=(EdgarAdapter(),),
    )
    reg(
        id="source.sec_facts", kind="source",
        tools=["query_edgar_facts"], schemas=s["gateway"],
        capabilities=["financials.xbrl", "filings.search"],
        markets=["US"], temporal="per_record_verified",
        auth="configured_user_agent", network="sec_endpoints",
        tests="tests/unit/test_edgar_facts.py",
        adapters=(EdgarFactsAdapter(),),
    )
    reg(
        id="source.hk_disclosures", kind="source",
        tools=["query_hkex_news"], schemas=s["gateway"],
        capabilities=["filings.search", "documents.fetch"],
        markets=["HK"], temporal="per_record_verified",
        network="hkexnews_endpoints",
        tests="tests/unit/test_data_sources_p2.py",
        adapters=(HKEXNewsAdapter(),),
    )
    reg(
        id="research.web", kind="source",
        tools=["query_web_search"], schemas=s["gateway"],
        capabilities=["web.search"], temporal="per_record_verified",
        auth="api_key:NOVITA_API_KEY|EXA_API_KEY", network="exa_api_via_gateway",
        tests="tests/unit/test_data_sources_p2.py",
        adapters=(ExaSearchAdapter(
            novita_api_key=environ.get("NOVITA_API_KEY") or None,
            api_key=environ.get("EXA_API_KEY") or None,
        ),),
    )
    reg(
        id="research.web_backup", kind="source",
        tools=["query_web_search_tavily"], schemas=s["gateway"],
        capabilities=["web.search"], temporal="none",
        auth="api_key:TAVILY_API_KEY", network="tavily_api",
        tests="tests/unit/test_data_sources_p2.py",
        adapters=(TavilySearchAdapter(api_key=environ.get("TAVILY_API_KEY") or None),),
    )
    reg(
        id="market.prices", kind="source",
        tools=["query_prices", "query_prices_stooq"], schemas=s["gateway"],
        capabilities=["market.prices"], temporal="per_record_verified",
        network="yahoo_and_stooq",
        tests="tests/unit/test_prices_fallback.py",
        adapters=(YFinancePricesAdapter(), StooqPricesAdapter()),
    )
    reg(
        id="vendor.fundamentals", kind="source",
        tools=["query_fundamentals"], schemas=s["gateway"],
        capabilities=["fundamentals.snapshot"], markets=["US"], temporal="none",
        network="vendor_libraries",
        tests="tests/unit/test_data_sources_p2.py",
        adapters=(YFinanceFundamentalsAdapter(),),
    )
    reg(
        id="vendor.fundamentals_hk", kind="source",
        tools=["query_fundamentals_hk"], schemas=s["gateway"],
        capabilities=["fundamentals.snapshot"], markets=["HK"], temporal="none",
        network="vendor_libraries",
        tests="tests/unit/test_data_sources_p2.py",
        # akshare 未安装（--extra data 缺）时不供 adapter：能力页 degraded 可见，
        # 与旧装配的「未安装则不注册 + 启动提示」行为一致
        adapters=(AkshareHKFundamentalsAdapter(),) if _akshare_ok else (),
        status_probe=None if _akshare_ok else (
            lambda: {"status": "degraded",
                     "detail": "akshare 未安装（uv sync --extra data）：港股基本面源不供数"}
        ),
    )
    reg(
        id="news.gdelt", kind="source",
        tools=["query_news_gdelt"], schemas=s["gateway"],
        capabilities=["news.search"], temporal="per_record_verified",
        network="gdelt_api",
        tests="tests/unit/test_data_sources_p2.py",
        adapters=(GdeltNewsAdapter(),),
    )

    # ---------------- 处理插件（processing）：包装现有 run 装配能力 ----------------

    reg(
        id="documents.reader", kind="processing",
        tools=["fetch_document", "read_document", "search_document", "read_edgar_filing"],
        schemas={**s["research"]},
        capabilities=["documents.fetch", "documents.paged_read", "documents.search"],
        requires=["documents.store", "evidence.registry"],
        network="document_fetch",
        tests="tests/unit/test_document_read_v2.py",
    )
    reg(
        id="knowledge.context", kind="processing",
        tools=["get_research_context", "query_observations", "query_claims",
               "query_calculations", "read_evidence", "list_conflicts",
               "adjudicate_conflict", "query_kb"],
        schemas={**s["context"], **s["research"]},
        capabilities=["knowledge.read", "knowledge.conflicts"],
        requires=["kb.bitemporal", "metrics.typed"],
        tests="tests/unit/test_context_tools.py",
    )
    reg(
        id="research.core", kind="processing",
        tools=["register_evidence", "read_chunk", "propose_fact", "resolve_conflict",
               "calc", "propose_metric", "propose_claim", "answer_question",
               "calculate_metric", "submit_question_result", "track_sub_question"],
        schemas={**s["research"]},
        capabilities=["evidence.binding", "facts.write", "metrics.write",
                      "claims.write", "calculations.controlled", "questions.sub_tracking"],
        requires=["evidence.registry", "writers.single", "kb.bitemporal"],
        tests="tests/unit/test_p0_trust_remediation.py",
    )
    reg(
        id="research.verifier", kind="processing",
        tools=["verify_claim"], schemas={**s["research"]},
        capabilities=["claims.content_verification", "claims.counter_evidence_loop"],
        requires=["metrics.typed", "evidence.registry"],
        tests="tests/unit/test_claim_verifier.py",
    )
    reg(
        id="profile.core", kind="processing",
        tools=["propose_thesis"], schemas=s["profile"],
        capabilities=["profile.thesis"], stages=["profile"],
        requires=["writers.single"],
        tests="tests/unit/test_context_tools.py",
    )
    reg(
        id="profile.consolidator", kind="processing",
        tools=["prepare_profile_update", "commit_profile_update"], schemas=s["profile"],
        capabilities=["profile.consolidation", "profile.dependency_invalidation",
                      "profile.semantic_diff"],
        stages=["profile"],
        requires=["metrics.typed", "kb.bitemporal", "snapshot.isolation"],
        tests="tests/unit/test_profile_consolidator.py",
    )
    reg(
        id="synthesize.core", kind="processing",
        tools=["submit_report_document", "submit_structures"], schemas=s["synthesize"],
        capabilities=["report.compose"], stages=["synthesize"],
        requires=["metrics.typed", "writers.single"],
        tests="tests/integration/test_research_dossier_e2e.py",
    )
    return registry


__all__ = ["build_builtin_registry"]

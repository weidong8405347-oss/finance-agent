"""把 DataGateway 包装成 agent tool：工具结果自动携带 provenance。

provenance 是 leakage-audit hook 的审计锚点（DESIGN.md §4.3 防线 3 的机械部分）。
"""

from __future__ import annotations

import json
from typing import Any

from .gateway import DataGateway


def make_gateway_tool(
    gateway: DataGateway, source_id: str, chunk_store=None, *,
    budget=None, cache: dict | None = None, max_record_chars: int | None = None,
):
    """生成一个工具函数：query(source 固定, request=arguments) → ToolResult dict。

    传入 chunk_store 时，每条记录同时落检索台账（EvidenceDesk）：返回项带 chunk_id，
    供 register_evidence 引用——「模型可见的记录才可引为证据」由此闭环。

    audit §3.3 新增三个可选接缝：
    - budget（RunBudget）：检索调用在网关入口真实扣减，耗尽即拒（不发请求）；
    - cache：同 run 内重复请求直接复用上次结果（不重复扣预算、不重复撑大上下文）；
    - max_record_chars：单条记录正文上限，超出则截断并告知用 read_chunk(chunk_id)
      按需取全文（evidence bundle）——工具响应不再无条件灌满上下文。
    """

    def tool(arguments: dict[str, Any]) -> dict[str, Any]:
        cache_key = None
        if cache is not None:
            cache_key = (source_id, json.dumps(arguments, ensure_ascii=False, sort_keys=True,
                                               default=str))
            hit = cache.get(cache_key)
            if hit is not None:
                # 命中去重：不扣检索预算、不打外部源（重复资料不该再算一次成本）
                if budget is not None:
                    budget.record_duplicate_retrieval()
                return {**hit, "cached": True}
        if budget is not None:
            ok, reason = budget.admit_retrieval()
            if not ok:
                return {"content": f"rejected: {reason}", "provenance": [],
                        "budget_denied": True}
        records = gateway.query(source_id, arguments)
        items = []
        for r in records:
            item = {**r.payload, "url": r.url, "available_at": r.available_at.isoformat()
                    if r.available_at else None}
            if chunk_store is not None:
                from ..knowledge.models import PitGrade

                # chunk 文本必须与模型所见逐项一致（子串校验的基准）；
                # 台账存规范正文（_canonical_text），JSON 只负责传输（audit §3.5）
                item_text = canonical_record_text(item)
                # 逐条有效等级（2026-09-01 实测修复）：源级 B 但本条无 available_at
                # → 本条降级 C（Evidence 校验：A/B 级必须有时刻；不给就拒登记）
                grade = gateway_grade(gateway, r.source_id)
                effective = grade if r.available_at is not None else "C"
                item["chunk_id"] = chunk_store.add(
                    source_id=r.source_id,
                    text=item_text,
                    url=r.url,
                    available_at=r.available_at,
                    pit_grade=PitGrade(effective),
                )
            items.append(_clamp_item(item, max_record_chars))
        result = {
            "content": json.dumps(items, ensure_ascii=False, default=str),
            "provenance": [
                {
                    "source_id": r.source_id,
                    "available_at": r.available_at.isoformat() if r.available_at else None,
                    "pit_grade": gateway_grade(gateway, r.source_id),
                }
                for r in records
            ],
        }
        if cache is not None and cache_key is not None:
            cache[cache_key] = result
        return result

    return tool


def _clamp_item(item: dict[str, Any], max_chars: int | None) -> dict[str, Any]:
    """按需 evidence bundle（audit §3.3）：长正文截断，全文凭 chunk_id 再取。

    只截长字符串字段；截断处显式告知模型如何取全文，不静默丢语义。
    """
    if not max_chars or max_chars <= 0:
        return item
    out = dict(item)
    for key, value in item.items():
        if isinstance(value, str) and len(value) > max_chars:
            out[key] = value[:max_chars]
            out[f"{key}_truncated"] = True
            if out.get("chunk_id"):
                out[f"{key}_full_text_via"] = f"read_chunk(chunk_id={out['chunk_id']})"
    return out


def canonical_record_text(item: dict[str, Any]) -> str:
    """检索台账的规范正文（audit §3.5）：模型引用的原文基准。

    旧实现把 JSON 序列化字符串当正文存进台账，而校验用原文子串匹配——
    换行/引号被转义后，模型复制真实正文也可能被拒（本次 44 次拒绝中至少 9 次
    属此类）。改为：文本字段原文拼接（保留换行与引号），非文本字段以
    `key=value` 附在后面（仍可引），JSON 只用于工具传输。
    """
    texts: list[str] = []
    scalars: list[str] = []
    for key, value in item.items():
        if key == "chunk_id" or key.startswith("_"):
            # "_" 前缀 = 内部元数据约定（如 SearchBroker 的转载族标记）：
            # 传输可见但不是证据原文，不进台账（子串校验的基准不受污染）
            continue
        if isinstance(value, str) and value.strip():
            texts.append(value)
        elif value is not None and not isinstance(value, (dict, list)):
            scalars.append(f"{key}={value}")
        elif isinstance(value, (dict, list)):
            scalars.append(f"{key}={json.dumps(value, ensure_ascii=False, default=str)}")
    parts = [*texts, *scalars]
    return "\n".join(p for p in parts if p)


def gateway_grade(gateway: DataGateway, source_id: str) -> str:
    adapter = gateway._adapters.get(source_id)  # noqa: SLF001 - 内部协作函数
    return adapter.capability().pit_grade.value if adapter else "C"


def make_search_broker_tool(
    broker: Any, chunk_store: Any, *,
    budget: Any | None = None, cache: dict | None = None,
    max_record_chars: int | None = None,
):
    """SearchBroker 的 agent 工具（方案 §5.1 search_sources）。

    与 make_gateway_tool 同一纪律：检索预算真实扣减（每个实际调用的引擎一次，
    broker 内部执行）、同 run 去重缓存（命中不重复扣预算）、逐条落检索台账
    （chunk_id 供 register_evidence 引用，PIT 按记录各自来源判定）、
    长正文凭 chunk_id 按需回读。错误与空结果区分：引擎全失败返回 error，
    无命中返回空清单 + trace。
    """

    def tool(arguments: dict[str, Any]) -> dict[str, Any]:
        mode = str(arguments.get("mode") or "auto")
        if mode not in ("auto", "primary", "dual"):
            return {"content": f"error: 未知 mode {mode!r}（auto/primary/dual）",
                    "provenance": []}
        cache_key = None
        if cache is not None:
            cache_key = ("search_sources", json.dumps(
                arguments, ensure_ascii=False, sort_keys=True, default=str))
            hit = cache.get(cache_key)
            if hit is not None:
                if budget is not None:
                    budget.record_duplicate_retrieval()
                return {**hit, "cached": True}
        from ..knowledge.models import PitGrade

        result = broker.search(arguments, mode=mode)
        items = []
        for r in result.records:
            broker_meta = r.payload.pop("_broker", {})  # 内部元数据不进台账正文
            item = {**r.payload, "url": r.url,
                    "available_at": r.available_at.isoformat()
                    if r.available_at else None}
            if chunk_store is not None:
                item_text = canonical_record_text(item)
                grade = gateway_grade(broker.gateway, r.source_id)
                effective = grade if r.available_at is not None else "C"
                item["chunk_id"] = chunk_store.add(
                    source_id=r.source_id, text=item_text, url=r.url,
                    available_at=r.available_at, pit_grade=PitGrade(effective),
                )
            if broker_meta:
                item["broker"] = broker_meta  # 传输层可见：origin/family_size/canonical
            items.append(_clamp_item(item, max_record_chars))
        out = {
            "content": json.dumps(
                {"items": items, "broker_trace": result.trace,
                 "note": "搜索用于发现来源；关键断言转入原文读取后才可引为证据"
                 if items else "空结果不等于不存在：可换检索式/来源或标 unavailable"},
                ensure_ascii=False, default=str,
            ),
            "provenance": [
                {"source_id": r.source_id,
                 "available_at": r.available_at.isoformat() if r.available_at else None,
                 "pit_grade": gateway_grade(broker.gateway, r.source_id)}
                for r in result.records
            ],
        }
        if cache is not None and cache_key is not None:
            cache[cache_key] = out
        return out

    return tool


def maybe_make_search_broker(
    gateway: DataGateway, *, events: Any | None = None, run_id: str = "",
    budget: Any | None = None,
) -> Any | None:
    """装配辅助：网关注册了任一搜索源才产 broker（缺凭证 fail-closed 源不在其列）。"""
    from .search_broker import SearchBroker

    broker = SearchBroker(gateway, events=events, run_id=run_id, budget=budget)
    return broker if broker.available() else None


#: 网关工具 schema（供 LLMRouter 绑定；按数据源 source_id 逐个生成）
SEARCH_SOURCES_SCHEMA: dict = {
    "name": "search_sources",
    "description": (
        "双源代理搜索（SearchBroker，方案 §5.1）：Exa 主源优先，主源报错/低召回自动合并 "
        "Tavily 备源（回退原因随结果可见）；canonical URL 去重 + 转载族归并——两个引擎"
        "返回同一公告只算一个原始来源（family_size 可见）。结果带 origin 标记与 chunk_id，"
        "重要断言须转入 fetch_document/read_document 读原文后才能引为证据。"
        "mode: auto（默认）/ primary（只打主源）/ dual（强制双源）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索式"},
            "num_results": {"type": "integer", "description": "每源上限（默认 8）"},
            "mode": {"type": "string", "enum": ["auto", "primary", "dual"]},
        },
        "required": ["query"],
    },
}

GATEWAY_TOOL_SCHEMAS: dict[str, dict] = {
    "search_sources": SEARCH_SOURCES_SCHEMA,
    "query_edgar": {
        "name": "query_edgar",
        "description": (
            "查询 SEC EDGAR 披露文件清单（filing 记录：表格/期间/原文链接）。"
            "公开时刻优先 acceptanceDateTime（分钟精度），缺失保守取 filingDate 日末，A 级 PIT。"
            "拿到记录后用 fetch_document/read_document 读原文；结构化数字首选 query_edgar_facts"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "forms": {"type": "array", "items": {"type": "string"}},
                "include_history": {"type": "boolean",
                                    "description": "遍历历史分段索引（默认只返回最近约千条）"},
                "max_history_files": {"type": "integer",
                                      "description": "历史分段上限（默认 4；每段一次额外请求）"},
            },
            "required": ["ticker"],
        },
    },
    "query_edgar_facts": {
        "name": "query_edgar_facts",
        "description": (
            "查询 SEC XBRL 结构化财务事实（companyfacts，A 级：acceptance 受理时刻为可知时刻）。"
            "返回原始 tag/unit/期间/filing 版本（accn）与原文链接——美股收入/利润/现金流/"
            "资产负债的正式口径首选，数字不依赖正文抽取。注意：分部 KPI/自定义口径可能"
            "缺失，需回 filing 原文（fetch_document）；结果按披露时间倒序、上限 limit 条，"
            "用 tags/forms/period 过滤缩小，截断不静默（换更窄过滤条件重查）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "cik": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"},
                         "description": "XBRL 概念过滤（如 Revenues / NetIncomeLoss / "
                                        "NetCashProvidedByUsedInOperatingActivities）"},
                "forms": {"type": "array", "items": {"type": "string"},
                          "description": "如 10-K / 10-Q / 8-K"},
                "units": {"type": "array", "items": {"type": "string"},
                          "description": "USD / shares / pure"},
                "period_start": {"type": "string", "description": "期间末不早于（YYYY-MM-DD）"},
                "period_end": {"type": "string", "description": "期间末不晚于（YYYY-MM-DD）"},
                "limit": {"type": "integer", "description": "默认 120，最大 400"},
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
    "query_prices_stooq": {
        "name": "query_prices_stooq",
        "description": "查询日线行情（Stooq 源，零依赖；available_at = 交易日 +1d）",
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
    "query_web_search": {
        "name": "query_web_search",
        "description": (
            "web 语义搜索（Exa，默认经 Novita 网关；B 级：publishedDate 为可知时刻，"
            "无日期的条目无 PIT 保证）。"
            "护城河/管理层/市场份额/行业空间等定性维度的主要证据源"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索词（中英文皆可，尽量具体）"},
                "num_results": {"type": "integer", "description": "条数（默认 8，上限 25）"},
            },
            "required": ["query"],
        },
    },
    "query_web_search_tavily": {
        "name": "query_web_search_tavily",
        "description": (
            "web 关键词搜索（Tavily，C 级：无逐条发布时间保证，评估模式不可用）。"
            "与 query_web_search（Exa）互为补充/并集，提高召回"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "description": "默认 8，上限 20"},
            },
            "required": ["query"],
        },
    },
    "query_fundamentals": {
        "name": "query_fundamentals",
        "description": (
            "查询美股基本面快照（市值/股本/TTM 财务，C 级：当前值无历史 PIT，"
            "评估模式不可用；严肃口径以 EDGAR 披露原文为准）"
        ),
        "parameters": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    "query_fundamentals_hk": {
        "name": "query_fundamentals_hk",
        "description": "查询港股基本面快照（akshare/东财，C 级；ticker 如 2228.HK 或 02228）",
        "parameters": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    "query_consensus_yf": {
        "name": "query_consensus_yf",
        "description": (
            "查询分析师一致预期快照（yfinance，C 级：当前值无历史 PIT，评估模式不可用）："
            "earnings_estimate（EPS 一致预期：avg/low/high/分析师数/去年同期货比）、"
            "revenue_estimate（收入一致预期）、eps_trend（7/30/60/90 天前对比值——"
            "revision 分析）、eps_revisions（近 7/30 天上修/下修家数）、growth_estimates。"
            "期间键 0q=本季度 +1q=下一季度 0y=本财年 +1y=下一财年。"
            "登记用 propose_metric(nature=\"consensus\", consensus={vendor:\"yfinance\","
            "snapshot_at=<当前时刻>}，metric_key 如 consensus_eps/consensus_revenue）"
        ),
        "parameters": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    "query_hkex_news": {
        "name": "query_hkex_news",
        "description": (
            "查询港股披露易公告（HKEXnews，A 级：披露时刻精确到分钟）。"
            "港股财报/公告的一手来源；ticker 如 2228.HK 或 02228"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "days": {"type": "integer", "description": "回看天数（默认 365）"},
            },
            "required": ["ticker"],
        },
    },
    "query_news_gdelt": {
        "name": "query_news_gdelt",
        "description": (
            "查询全球新闻（GDELT，B 级：seendate 收录时刻；含中文媒体）。"
            "催化剂/风险/舆情维度的证据源"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_records": {"type": "integer", "description": "默认 25，上限 250"},
                "timespan": {"type": "string", "description": "如 1d/1w/1m（默认 1m）"},
            },
            "required": ["query"],
        },
    },
    # 仅 eval canary 模式注册的源。模型侧描述必须与真源不可区分——诱饵的全部意义
    # 在于考验 agent 是否复述上下文；操作员侧的 capability 描述保持诚实标注（能力页）。
    "query_canary_news": {
        "name": "query_canary_news",
        "description": "查询市场新闻与公司公告（available_at = 发布时刻）",
        "parameters": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
            },
            "required": ["ticker"],
        },
    },
}

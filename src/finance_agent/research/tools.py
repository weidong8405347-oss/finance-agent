"""研究工具集：read_edgar_filing / register_evidence / propose_fact / query_kb。

关键纪律（DESIGN.md §4.1 + 2026-08-30 验收事故整改）：
- **证据只能登记自本 run 实际检索到的内容**（EvidenceDesk chunk）：
  网关记录自带 chunk_id；filing 正文用 read_edgar_filing 抓取切块；
- register_evidence 服务端校验 quote 是 chunk 的逐珠子串，来源/available_at 由
  chunk 元数据推导——模型自报的来源与时刻一律不信（堵参数记忆自编自引）；
- knowledge_time 由证据推导（= 所绑证据的最大 available_at），不信任模型自报；
- 落库必经 ProfileWriter（单写者），硬门禁在 writer 内执行；
- 工具结果携带 provenance，供评估模式的 leakage-audit 审计。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ..eventstore.store import EventStore
from ..harness.manifest import RunManifest
from ..knowledge.errors import KnowledgeError
from ..knowledge.models import Fact
from ..knowledge.normalize import normalize_entity_id
from ..knowledge.store import BitemporalStore
from ..knowledge.verify import STRUCTURED_LIST_FIELDS
from ..knowledge.writer import ProfileWriter
from .calc import CALC_TOOL_SCHEMA, calc_tool
from .evidence_desk import ChunkStore, EvidenceVerificationError, verify_and_build

#: 抓取函数的签名：filing URL → 纯文本正文（HTML 已剥离）
FetchDocument = Callable[[str], str]

_WS = re.compile(r"\s+")

#: 结构化字段校验注册表的唯一来源是 knowledge.verify.STRUCTURED_LIST_FIELDS
#: （2026-09-01 实测：模型把 player_landscape 写成 JSON 字符串，下游 F3 读到
#: 1491 个字符——类型不校验的静默腐化）。本层保留早期友好报错，硬门禁在 writer。
_STRUCTURED_LIST_FIELDS = STRUCTURED_LIST_FIELDS


class _Tracker:
    def __init__(self) -> None:
        self.written: list[str] = []
        self.rejected: list[dict] = []
        self.registered: list[str] = []  # 已登记证据 id（囤证据检测：登记多而写入少 = 空转）


def _windows(text: str, query: str, *, width: int = 1600, max_windows: int = 4) -> list[str]:
    """围绕 query 命中点切窗口；无命中/无 query → 文档开头两段窗口。"""
    if query:
        hits = [m.start() for m in re.finditer(re.escape(query), text, flags=re.IGNORECASE)]
        if hits:
            out: list[str] = []
            for pos in hits:
                start = max(0, pos - width // 3)
                window = text[start : start + width]
                if not out or window not in out[-1]:  # 粗略去重（重叠窗口）
                    out.append(window)
                if len(out) >= max_windows:
                    break
            return out
    return [text[:width], text[width : width * 2]] if len(text) > width else [text]


def make_research_tools(
    *,
    store: BitemporalStore,
    writer: ProfileWriter,
    manifest: RunManifest,
    entity_kind: str,
    entity_id: str,
    namespace: str = "prod",
    chunk_store: ChunkStore,
    fetch_document: FetchDocument | None = None,
    events: EventStore | None = None,  # noqa: ARG001 - 预留给工具级审计
) -> tuple[dict[str, Any], _Tracker]:
    tracker = _Tracker()

    def register_evidence(args: dict[str, Any]) -> dict[str, Any]:
        """证据登记：只接受 chunk_id + 逐字摘录（服务端子串校验，fail-closed）。"""
        try:
            ev = verify_and_build(
                chunk_store,
                chunk_id=str(args.get("chunk_id") or ""),
                verbatim_quote=str(args.get("verbatim_quote") or ""),
                evidence_id=args.get("evidence_id"),
            )
        except EvidenceVerificationError as e:
            tracker.rejected.append({"evidence": str(args.get("chunk_id")), "reason": str(e)})
            return {"content": f"rejected: {e}", "provenance": []}
        store.add_evidence(ev)
        tracker.registered.append(ev.evidence_id)
        return {
            "content": json.dumps({"evidence_id": ev.evidence_id}, ensure_ascii=False),
            "provenance": [
                {
                    "source_id": ev.source_id,
                    "available_at": ev.available_at.isoformat() if ev.available_at else None,
                    "pit_grade": ev.pit_grade.value,
                }
            ],
        }

    def propose_fact(args: dict[str, Any]) -> dict[str, Any]:
        field = args["field"]
        evidence_ids: list[str] = args["evidence_ids"]
        # 实体 ID 归一（写入口纵深防御；writer 层还有兜底）：同标的只允许一个档案
        canonical_id = normalize_entity_id(entity_kind, entity_id)
        # 结构化字段类型校验（写侧 fail-loud，防 JSON 字符串腐化下游）
        if field in _STRUCTURED_LIST_FIELDS:
            required_keys = _STRUCTURED_LIST_FIELDS[field]
            value = args.get("value")
            if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
                return {"content": f"rejected: 字段 {field} 的值必须是 list[object]（收到 "
                                   f"{type(value).__name__}）；逐条给对象，不要拼 JSON 字符串",
                        "provenance": []}
            bad = [i for i, v in enumerate(value)
                   if any(k not in v for k in required_keys)]
            if bad:
                return {"content": f"rejected: 字段 {field} 的第 {bad} 条缺必备键 {required_keys}",
                        "provenance": []}
        try:
            evidences = [store.get_evidence(e) for e in evidence_ids]
            # knowledge_time 推导：所绑证据的最晚可知时刻；全无（C 级）则取当前（生产模式）
            known = [e.available_at for e in evidences if e.available_at is not None]
            knowledge_time = max(known) if known else datetime.now(UTC)
            fact = Fact(
                entity_kind=entity_kind,
                entity_id=canonical_id,
                field=field,
                value=args["value"],
                event_time=datetime.fromisoformat(args["event_time"]) if args.get("event_time") else None,
                knowledge_time=knowledge_time,
                evidence_ids=evidence_ids,
                run_id=manifest.run_id,
            )
            fact_id = writer.write_fact(fact, run=manifest, namespace=namespace)
        except (KnowledgeError, KeyError, ValueError) as e:
            tracker.rejected.append({"field": field, "reason": str(e)})
            return {"content": f"rejected: {e}", "provenance": []}
        tracker.written.append(field)
        return {
            "content": json.dumps({"fact_id": fact_id, "field": field}, ensure_ascii=False),
            "provenance": [
                {
                    "source_id": e.source_id,
                    "available_at": e.available_at.isoformat() if e.available_at else None,
                    "pit_grade": e.pit_grade.value,
                }
                for e in evidences
            ],
        }

    def query_kb(args: dict[str, Any]) -> dict[str, Any]:
        del args
        profile = store.as_of(entity_kind, entity_id, datetime.now(UTC), namespace=namespace)
        return {
            "content": json.dumps(
                {
                    f: {"value": r.value, "knowledge_time": r.knowledge_time.isoformat()}
                    for f, r in profile.items()
                },
                ensure_ascii=False,
                default=str,
            ),
            "provenance": [
                {"source_id": "kb", "available_at": r.knowledge_time.isoformat(), "pit_grade": "A"}
                for r in profile.values()
            ],
        }

    def resolve_conflict(args: dict[str, Any]) -> dict[str, Any]:
        """裁决字段冲突（Q2）：采集到更强证据后调用，清除该字段的竞争版本标记。"""
        field = str(args["field"])
        keep = str(args["keep_evidence_id"])
        n = writer.resolve_conflict(
            entity_kind, entity_id, field,
            keep_fact_id="",  # 以证据为准的裁决（keep_fact_id 仅作审计记录）
            note=str(args.get("note") or f"以证据 {keep} 为准"),
            run=manifest,
            namespace=namespace,
        )
        return {"content": json.dumps({"resolved": field, "cleared": n}, ensure_ascii=False),
                "provenance": []}

    tools: dict[str, Any] = {
        "register_evidence": register_evidence,
        "propose_fact": propose_fact,
        "query_kb": query_kb,
        "resolve_conflict": resolve_conflict,
        "calc": calc_tool,  # §4.7：数字保护双保险（逐字 + 计算一致性）
    }

    if fetch_document is not None:
        def read_edgar_filing(args: dict[str, Any]) -> dict[str, Any]:
            """抓取 filing 正文并按 query 切窗口，窗口落 ChunkStore 供 register_evidence 引用。"""
            record_chunk = chunk_store.get(str(args.get("chunk_id") or ""))
            if record_chunk is None:
                return {"content": "error: 未知 chunk_id（先 query_edgar 拿 filing 记录）", "provenance": []}
            if not record_chunk.url:
                return {"content": "error: 该记录没有可抓取的 url", "provenance": []}
            try:
                text = fetch_document(record_chunk.url)
            except Exception as e:
                return {"content": f"error: 抓取失败：{type(e).__name__}: {e}", "provenance": []}
            out = []
            for window in _windows(text, str(args.get("query") or "")):
                cid = chunk_store.add(
                    source_id=record_chunk.source_id,
                    text=window,
                    url=record_chunk.url,
                    available_at=record_chunk.available_at,  # PIT 元数据从 filing 记录继承
                    pit_grade=record_chunk.pit_grade,
                )
                out.append({"chunk_id": cid, "text": window})
            return {
                "content": json.dumps({"windows": out}, ensure_ascii=False),
                "provenance": [
                    {
                        "source_id": record_chunk.source_id,
                        "available_at": record_chunk.available_at.isoformat()
                        if record_chunk.available_at
                        else None,
                        "pit_grade": record_chunk.pit_grade.value,
                    }
                ],
            }

        tools["read_edgar_filing"] = read_edgar_filing

    return tools, tracker


TOOL_SCHEMAS: dict[str, dict] = {
    "calc": CALC_TOOL_SCHEMA,
    # F2/F3 漏斗专用工具（在 step 内动态注入 kernel；schema 登记在此供路由层绑定，
    # 否则模型看到空 schema 会以空参调用——2026-09-01 实测 submit_card({}) 空提事故）
    "propose_candidates": {
        "name": "propose_candidates",
        "description": "提交一批赛道候选标的（每只必须绑 ≥1 条已登记证据，否则拒收）",
        "parameters": {
            "type": "object",
            "properties": {
                "candidates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "ticker": {"type": "string"},
                            "name": {"type": "string"},
                            "market": {"type": "string", "description": "US/HK/CN"},
                            "sub_sector": {"type": "string"},
                            "one_liner": {"type": "string"},
                            "listed": {"type": "boolean"},
                            "evidence_ids": {"type": "array", "items": {"type": "string"},
                                             "minItems": 1},
                        },
                        "required": ["ticker", "evidence_ids"],
                    },
                }
            },
            "required": ["candidates"],
        },
    },
    "submit_card": {
        "name": "submit_card",
        "description": "提交一只标的的粗调研卡（F3 闸口筛选用；全部字段必填）",
        "parameters": {
            "type": "object",
            "properties": {
                "one_liner": {"type": "string", "description": "业务一句话"},
                "key_metrics": {"type": "object", "description": "市值/收入/现金等（查不到标未知）"},
                "highlights": {"type": "array", "items": {"type": "string"}},
                "risks": {"type": "array", "items": {"type": "string"}},
                "richness": {"type": "string", "enum": ["A", "B", "C"],
                              "description": "信息丰富度评级"},
                "recommend": {"type": "boolean", "description": "是否推荐进入深研"},
                "reason": {"type": "string", "description": "推荐/淘汰的一句话理由"},
                "evidence_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            },
            "required": ["one_liner", "key_metrics", "recommend", "reason", "evidence_ids"],
        },
    },
    "register_evidence": {
        "name": "register_evidence",
        "description": (
            "登记一条证据。quote 必须是所引 chunk 的逐字原文（服务端校验子串，不符即拒）；"
            "来源与可知时刻由系统从 chunk 推导，无需自报。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chunk_id": {"type": "string",
                             "description": "检索内容 chunk id（query_* 或 read_edgar_filing 返回）"},
                "verbatim_quote": {"type": "string", "description": "该 chunk 内的逐字原文摘录"},
                "evidence_id": {"type": "string", "description": "可选；不提供则自动生成"},
            },
            "required": ["chunk_id", "verbatim_quote"],
        },
    },
    "propose_fact": {
        "name": "propose_fact",
        "description": "把一条事实写入实体档案（必须绑 ≥1 条已登记证据；knowledge_time 由证据推导）",
        "parameters": {
            "type": "object",
            "properties": {
                "field": {"type": "string"},
                "value": {},
                "event_time": {"type": "string", "description": "事实何时为真（如财年截止日）"},
                "evidence_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            },
            "required": ["field", "value", "evidence_ids"],
        },
    },
    "query_kb": {
        "name": "query_kb",
        "description": "查询当前实体档案（as_of 现在的投影）",
        "parameters": {"type": "object", "properties": {}},
    },
    "resolve_conflict": {
        "name": "resolve_conflict",
        "description": "裁决字段的开放冲突：采集到更强证据后调用，清除该字段的竞争版本标记",
        "parameters": {
            "type": "object",
            "properties": {
                "field": {"type": "string"},
                "keep_evidence_id": {"type": "string", "description": "以哪条证据为准"},
                "note": {"type": "string", "description": "裁决理由"},
            },
            "required": ["field", "keep_evidence_id"],
        },
    },
    "read_edgar_filing": {
        "name": "read_edgar_filing",
        "description": (
            "抓取一条 EDGAR filing 记录的正文，按 query 关键词切出原文窗口"
            "（返回的窗口带新 chunk_id，供 register_evidence 引用）"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chunk_id": {"type": "string", "description": "query_edgar 返回的 filing 记录 chunk_id"},
                "query": {"type": "string", "description": "定位关键词（如 'total revenue'）"},
            },
            "required": ["chunk_id"],
        },
    },
    # read_evidence 在多个 step 内动态注入（synthesize/committee/rank_report）；
    # 缺 schema 时路由层回退空参 schema，模型会以 read_evidence({}) 空转——
    # 2026-09-02 P4 验收实测：委员会 financial 视角 8 步全烧在空参调用上
    "read_evidence": {
        "name": "read_evidence",
        "description": "读取一条已登记证据的原文（verbatim_quote）与来源，用于核对事实锚点",
        "parameters": {
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string", "description": "证据 id（ev- 前缀）"},
            },
            "required": ["evidence_id"],
        },
    },
}

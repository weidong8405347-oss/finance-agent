"""Manifest freezing：同一 run 中不静默切换版本（方案 §6.2）。

冻结的是「真实启用了什么」：编译结果里的插件/版本/工具/schema 指纹 + 配置哈希
（凭证只记存在性，密钥值绝不进事件与哈希原文）。冻结事件是事后归因的锚点：
一次研究的产出对应哪套能力面，可回放可审计。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from ..eventstore.events import Event
from ..eventstore.store import EventStore
from .registry import CompiledSet

#: 能力面冻结事件（run 级；同 run 重复冻结 = 装配缺陷，fail-loud 由调用方约束）
PLUGINS_MANIFEST_FROZEN = "plugins/manifest_frozen"


def freeze_payload(compiled: CompiledSet, *, run_id: str, extra: dict[str, Any] | None = None) -> dict:
    """冻结 payload（可独立用于产物嵌入/对照报告）。"""
    return {
        "run_id": run_id,
        "stage": compiled.stage,
        "market": compiled.market,
        "role": compiled.role,
        "config_hash": compiled.config_hash,
        "frozen_at": datetime.now(UTC).isoformat(),
        "plugins": [
            {"id": m.id, "version": m.version, "kind": m.kind,
             "fingerprint": m.schema_fingerprint(),
             "temporal_policy": m.temporal_policy,
             "failure_policy": m.failure_policy}
            for m in compiled.plugins
        ],
        "tools": [
            {"name": name,
             "plugin_id": next(
                 (t.plugin_id for t in compiled.tools.values() if t.name == name), ""
             ),
             "bound": name in compiled.tools,
             "schema_hash": hashlib.sha256(json.dumps(
                 schema, ensure_ascii=False, sort_keys=True, default=str
             ).encode("utf-8")).hexdigest()[:12]}
            for name, schema in sorted(compiled.declared_schemas.items())
        ],
        "blocked": [
            {"id": s.plugin_id, "status": s.status, "reason": s.reason}
            for s in compiled.blocked
        ],
        **(extra or {}),
    }


def freeze_manifest(
    compiled: CompiledSet, *, run_id: str, events: EventStore | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """落冻结事件并返回 payload（幂等由调用方保证：一个 run 冻结一次）。"""
    payload = freeze_payload(compiled, run_id=run_id, extra=extra)
    if events is not None:
        events.append(Event(run_id=run_id, type=PLUGINS_MANIFEST_FROZEN, payload=payload))
    return payload


__all__ = ["freeze_manifest", "freeze_payload", "PLUGINS_MANIFEST_FROZEN"]

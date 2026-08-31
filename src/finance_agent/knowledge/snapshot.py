"""kb_snapshot_id：as_of(T) 物化投影的内容寻址哈希。

决策卡与评估报告都引用它——「这个决策是基于哪一版知识」必须可复现
（DESIGN.md §5.2/§5.3）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import datetime

from .store import BitemporalStore

EntityRef = tuple[str, str]  # (entity_kind, entity_id)


def kb_snapshot_id(
    store: BitemporalStore,
    entities: Iterable[EntityRef],
    t: datetime,
    *,
    namespace: str = "prod",
) -> str:
    projection = {
        "namespace": namespace,
        "as_of": t.isoformat(),
        "entities": {},
    }
    for kind, entity_id in sorted(set(entities)):
        facts = store.view(kind, entity_id, t, namespace=namespace)
        projection["entities"][f"{kind}:{entity_id}"] = {
            field: {
                "value": rec.value,
                "event_time": rec.event_time.isoformat() if rec.event_time else None,
                "knowledge_time": rec.knowledge_time.isoformat(),
                "version": rec.version,
                "fact_id": rec.fact_id,
                "evidence_ids": sorted(rec.evidence_ids),
            }
            for field, rec in sorted(facts.items())
        }
    canonical = json.dumps(projection, ensure_ascii=False, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

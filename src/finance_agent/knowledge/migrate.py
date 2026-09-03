"""档案维护迁移：合并重复实体（验收事故 2228.HK vs 02228.HK 的修复工具）。

做法：把 source 实体的全部事实按 (knowledge_time, version) 顺序重放进 target
——复用 BitemporalStore.assert_fact 的版本链/冲突语义（同 event_time 不同值
自动标冲突，交给既有裁决流程），而非直接 SQL 改行（那会绕过不变量）。
重放完成后删除 source 的全部行与遗留 HTML 存档目录（投影，可从合并后事实重建）。

证据表不受影响（evidence_id 全局共享）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .models import Fact
from .store import BitemporalStore


def merge_entity(
    store: BitemporalStore,
    entity_kind: str,
    from_id: str,
    to_id: str,
    *,
    namespace: str = "prod",
    knowledge_dir: Path | None = None,
) -> dict:
    """把 from_id 档案并入 to_id；返回合并报告（重放条数、冲突字段、删除条数）。

    安全约束（2026-09-03 事故整改）：from_id 必须与 to_id 不同——曾经 from_id
    被调用方归一化后与 to_id 相同，实体合并进自身后整档被 DELETE（38 条事实
    靠事件溯源回放才找回）。
    """
    if from_id == to_id:
        raise ValueError(
            f"from_id 与 to_id 相同（{from_id!r}）：归一化只能在调用方对 to_id 做，"
            "from_id 必须保持原始（可能不规范）形态"
        )
    rows = store._conn.execute(  # noqa: SLF001 - 迁移工具需要逐行重放，属维护通道
        "SELECT field, value_json, event_time, knowledge_time, version, evidence_ids, run_id"
        " FROM facts WHERE namespace = ? AND entity_kind = ? AND entity_id = ?"
        " ORDER BY knowledge_time, version",
        (namespace, entity_kind, from_id),
    ).fetchall()
    if not rows:
        return {"merged": 0, "conflict_fields": [], "deleted": 0, "note": f"{from_id} 无事实"}

    import json as _json
    from datetime import datetime as _dt

    before_conflicts = {r.field for r in store.open_conflicts(entity_kind, to_id, namespace=namespace)}
    for field, value_json, event_time, knowledge_time, _ver, evidence_ids, run_id in rows:
        store.assert_fact(
            Fact(
                entity_kind=entity_kind,
                entity_id=to_id,
                field=field,
                value=_json.loads(value_json),
                event_time=_dt.fromisoformat(event_time) if event_time else None,
                knowledge_time=_dt.fromisoformat(knowledge_time),
                evidence_ids=_json.loads(evidence_ids),
                run_id=run_id,
            ),
            namespace=namespace,
        )
    after_conflicts = {r.field for r in store.open_conflicts(entity_kind, to_id, namespace=namespace)}

    with store._lock:  # noqa: SLF001
        cur = store._conn.execute(  # noqa: SLF001
            "DELETE FROM facts WHERE namespace = ? AND entity_kind = ? AND entity_id = ?",
            (namespace, entity_kind, from_id),
        )
        store._conn.commit()  # noqa: SLF001
        deleted = cur.rowcount

    archive_removed = False
    if knowledge_dir is not None:
        orphan = knowledge_dir / f"{entity_kind}s" / from_id
        if orphan.exists():
            shutil.rmtree(orphan)
            archive_removed = True

    return {
        "merged": len(rows),
        "conflict_fields": sorted(after_conflicts - before_conflicts),
        "deleted": deleted,
        "archive_removed": archive_removed,
    }

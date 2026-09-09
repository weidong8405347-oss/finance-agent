"""删除协调层：会话清理与知识实体清除（用户发起，可审计，默认不硬删）。

为什么需要这一层：历史上积累了大量低质量档案与会话，用户必须能清掉它们；
但事件日志是全系统唯一真相源（append-only），知识行删了还能从事件重放复活——
所以删除分两种模式，且每一次删除都留审计记录：

- **tombstone（默认）**：写实体墓碑 + `knowledge/purged` 事件，所有读路径过滤。
  事实/观测行仍在（可审计、可 `restore`），页面与列表立即干净。
- **hard**：墓碑 + 真删行（kb.facts / metrics 九表 / decisions）+ 清孤儿证据
  （两个库都不再引用的 evidence）+ 删磁盘产物（reports/<child_run>、
  knowledge/<kind>/<id> 存档）。事件日志保留（含 `knowledge/purged` 审计），
  因此 hard 删除**不可恢复**，必须由调用方显式选择。

会话删除（`purge_session`）走硬删：会话是过程产物不是知识真相源，但同样级联子 run、
先写审计事件、并拒绝删正在跑的会话（除非 force）。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..decision.store import DecisionStore
from ..eventstore.events import Event
from ..eventstore.store import PURGE_AUDIT_RUN, EventStore
from .metric_store import MetricStore
from .store import BitemporalStore

KNOWLEDGE_PURGED = "knowledge/purged"
KNOWLEDGE_RESTORED = "knowledge/restored"


class PurgeError(RuntimeError):
    """删除前置条件不满足（如会话仍在运行且未 force）。"""


@dataclass
class PurgeReport:
    """一次删除的完整回执（每一项都可核对，不笼统报「已清理」）。"""

    entity_kind: str
    entity_id: str
    mode: str  # tombstone / hard
    namespace: str
    reason: str
    purged_at: str
    #: 逐表删除行数（hard 模式）
    counts: dict[str, int] = field(default_factory=dict)
    orphan_evidence_deleted: int = 0
    evidence_remaining: int | None = None
    files_removed: list[str] = field(default_factory=list)
    restorable: bool = True
    warnings: list[str] = field(default_factory=list)

    def as_payload(self) -> dict[str, Any]:
        return {
            "entity_kind": self.entity_kind, "entity_id": self.entity_id,
            "mode": self.mode, "namespace": self.namespace, "reason": self.reason,
            "purged_at": self.purged_at, "counts": dict(self.counts),
            "orphan_evidence_deleted": self.orphan_evidence_deleted,
            "evidence_remaining": self.evidence_remaining,
            "files_removed": list(self.files_removed),
            "restorable": self.restorable, "warnings": list(self.warnings),
            "total_rows_deleted": sum(self.counts.values()),
        }


def preview_entity(
    *, kb: BitemporalStore, metrics: MetricStore | None = None,
    decisions: DecisionStore | None = None,
    entity_kind: str, entity_id: str, namespace: str = "prod",
) -> dict[str, Any]:
    """删除前盘点（干跑）：会删掉什么、各多少行，不落任何改动。"""
    counts: dict[str, int] = {}
    counts["facts"] = int(kb._conn.execute(  # noqa: SLF001 - 只读盘点
        "SELECT COUNT(*) FROM facts WHERE namespace = ? AND entity_kind = ? AND entity_id = ?",
        (namespace, entity_kind, entity_id),
    ).fetchone()[0])
    if metrics is not None:
        for table in MetricStore._ENTITY_TABLES:  # noqa: SLF001
            counts[table] = int(metrics._conn.execute(  # noqa: SLF001
                f"SELECT COUNT(*) FROM {table} WHERE namespace = ? AND entity_kind = ?"
                " AND entity_id = ?", (namespace, entity_kind, entity_id),
            ).fetchone()[0])
    if decisions is not None:
        counts["decisions"] = int(decisions._conn.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM decisions WHERE namespace = ? AND entity_kind = ?"
            " AND entity_id = ?", (namespace, entity_kind, entity_id),
        ).fetchone()[0])
    return {
        "entity": f"{entity_kind}:{entity_id}",
        "namespace": namespace,
        "counts": {k: v for k, v in counts.items() if v},
        "total_rows": sum(counts.values()),
        "already_purged": kb.is_purged(entity_kind, entity_id, namespace=namespace),
    }


def purge_entity(
    *,
    kb: BitemporalStore,
    entity_kind: str,
    entity_id: str,
    metrics: MetricStore | None = None,
    decisions: DecisionStore | None = None,
    events: EventStore | None = None,
    mode: str = "tombstone",
    namespace: str = "prod",
    reason: str = "",
    reports_dir: str | Path | None = None,
    knowledge_dir: str | Path | None = None,
    now: datetime | None = None,
) -> PurgeReport:
    """删除一个知识实体（默认墓碑；mode=hard 才真删行与文件）。"""
    if mode not in ("tombstone", "hard"):
        raise PurgeError(f"未知删除模式 {mode!r}（可用：tombstone / hard）")
    at = now or datetime.now(UTC)
    report = PurgeReport(
        entity_kind=entity_kind, entity_id=entity_id, mode=mode, namespace=namespace,
        reason=reason, purged_at=at.isoformat(), restorable=(mode == "tombstone"),
    )
    if not reason.strip():
        report.warnings.append("未给删除原因（审计记录里 reason 为空）")

    if mode == "hard":
        report.counts["facts"] = kb.delete_entity(entity_kind, entity_id, namespace=namespace)
        if metrics is not None:
            for table, n in metrics.delete_entity(
                entity_kind, entity_id, namespace=namespace
            ).items():
                report.counts[table] = n
        if decisions is not None:
            n = decisions.delete_entity(entity_kind, entity_id, namespace=namespace)
            if n:
                report.counts["decisions"] = n
        # 孤儿证据：两个库都不再引用才删（证据是全局表，不能按实体粗暴清）
        referenced = kb.referenced_evidence_ids()
        if metrics is not None:
            referenced |= metrics.referenced_evidence_ids()
        before = kb.evidence_count()
        orphans = _orphan_evidence(kb, referenced)
        report.orphan_evidence_deleted = kb.delete_evidence(orphans) if orphans else 0
        report.evidence_remaining = kb.evidence_count()
        if report.orphan_evidence_deleted:
            report.warnings.append(
                f"清理孤儿证据 {report.orphan_evidence_deleted} 条"
                f"（删除前 {before}，删除后 {report.evidence_remaining}）"
            )
        report.files_removed = _remove_entity_files(
            entity_kind, entity_id, reports_dir=reports_dir, knowledge_dir=knowledge_dir
        )

    kb.mark_purged(entity_kind, entity_id, namespace=namespace, mode=mode,
                   reason=reason, purged_at=at)
    if events is not None:
        events.append(Event(
            run_id=PURGE_AUDIT_RUN, type=KNOWLEDGE_PURGED,
            payload={**report.as_payload(), "entity": f"{entity_kind}:{entity_id}"},
        ))
    return report


def restore_entity(
    *, kb: BitemporalStore, entity_kind: str, entity_id: str,
    events: EventStore | None = None, namespace: str = "prod", reason: str = "",
) -> bool:
    """撤销墓碑（仅 tombstone 模式可恢复；hard 已删行，无法恢复）。"""
    ok = kb.restore_entity(entity_kind, entity_id, namespace=namespace)
    if ok and events is not None:
        events.append(Event(
            run_id=PURGE_AUDIT_RUN, type=KNOWLEDGE_RESTORED,
            payload={"entity": f"{entity_kind}:{entity_id}", "namespace": namespace,
                     "reason": reason},
        ))
    return ok


def _orphan_evidence(kb: BitemporalStore, referenced: set[str]) -> list[str]:
    rows = kb._conn.execute("SELECT evidence_id FROM evidence").fetchall()  # noqa: SLF001
    return sorted(r[0] for r in rows if r[0] not in referenced)


def _remove_entity_files(
    entity_kind: str, entity_id: str, *,
    reports_dir: str | Path | None, knowledge_dir: str | Path | None,
) -> list[str]:
    """删磁盘产物：档案 HTML 存档目录（knowledge/<kind>s/<id>）。

    reports/ 按 child_run_id 分目录、不带实体标识，无法安全归属到单个实体——
    不猜、不误删（要清报告请删对应会话，走 purge_session）。
    """
    removed: list[str] = []
    if knowledge_dir:
        bucket = "industries" if entity_kind == "industry" else "stocks"
        target = Path(knowledge_dir) / bucket / entity_id
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            removed.append(str(target))
    if reports_dir:
        # 只有目录名里显式带实体 id 的才删（保守：宁可不删也不误删别的实体）
        needle = entity_id.lower()
        root = Path(reports_dir)
        if root.is_dir():
            for child in root.iterdir():
                if child.is_dir() and needle in child.name.lower():
                    shutil.rmtree(child, ignore_errors=True)
                    removed.append(str(child))
    return removed


def purge_session(
    *,
    events: EventStore,
    run_id: str,
    force: bool = False,
    reason: str = "",
    reports_dir: str | Path | None = None,
) -> dict[str, Any]:
    """删除一个会话（含全部子 run）与其报告目录。

    正在跑的会话默认拒删（409 语义）：先 stop 再删，或显式 force——
    避免删掉一个仍在写库的运行，留下半截数据。
    """
    if not events.is_session_run(run_id):
        # 系统/投影 run 不作为会话删除（要清请走实体删除或直接在库里处理）
        raise PurgeError(
            f"{run_id} 不是会话（系统/投影 run 或只有副作用事件），不按会话删除"
        )
    if events.is_active(run_id) and not force:
        raise PurgeError(
            f"会话 {run_id} 仍在运行（有未闭合的 command/turn）："
            "先 POST /api/sessions/{run_id}/stop，或带 force=true 强删"
        )
    result = events.delete_run(run_id, cascade=True, reason=reason)
    removed: list[str] = []
    if reports_dir:
        root = Path(reports_dir)
        if root.is_dir():
            for target in (root / rid for rid in result["deleted_runs"]):
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                    removed.append(str(target))
    result["files_removed"] = removed
    result["reason"] = reason
    result["forced"] = force
    return result


__all__ = [
    "PurgeReport", "PurgeError", "preview_entity", "purge_entity", "restore_entity",
    "purge_session", "KNOWLEDGE_PURGED", "KNOWLEDGE_RESTORED",
]

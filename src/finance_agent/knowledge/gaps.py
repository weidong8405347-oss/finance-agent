"""GapAnalyzer：对照 schema 计算档案缺口（完整度 + 新鲜度 + 冲突）。

每轮研究的起点（DESIGN.md §5.1）：「这轮研究什么」由缺口决定，
而不是让模型自由发挥——迭代完善的机制化保证。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from .schema import SCHEMAS, ProfileSchema
from .store import BitemporalStore

STALE_WEIGHT = 0.5  # 陈旧字段按半分计入完整度


class GapReport(BaseModel):
    entity_kind: str
    entity_id: str
    as_of: datetime
    missing: list[str]
    stale: list[str]
    conflicts: list[str]
    completeness: float  # 0..1
    optional_missing: list[str] = []  # 可选维度缺口（引导，不计入完整度）


class GapAnalyzer:
    def __init__(self, store: BitemporalStore, schemas: dict[str, ProfileSchema] | None = None):
        self._store = store
        self._schemas = schemas or SCHEMAS

    def analyze(
        self,
        entity_kind: str,
        entity_id: str,
        as_of: datetime,
        *,
        namespace: str = "prod",
    ) -> GapReport:
        schema = self._schemas.get(entity_kind)
        if schema is None or not schema.required:
            return GapReport(
                entity_kind=entity_kind, entity_id=entity_id, as_of=as_of,
                missing=[], stale=[], conflicts=[], completeness=0.0,
            )

        profile = self._store.view(entity_kind, entity_id, as_of, namespace=namespace)
        missing: list[str] = []
        stale: list[str] = []
        fresh_count = 0
        for field, policy in schema.required.items():
            rec = profile.get(field)
            if rec is None:
                missing.append(field)
            elif policy.max_age_days is not None and (as_of - rec.knowledge_time).days > policy.max_age_days:
                stale.append(field)
            else:
                fresh_count += 1

        completeness = (fresh_count + STALE_WEIGHT * len(stale)) / len(schema.required)
        conflicts = [f.field for f in self._store.open_conflicts(entity_kind, entity_id, namespace=namespace)]
        optional_missing = [f for f in schema.optional if f not in profile]
        return GapReport(
            entity_kind=entity_kind,
            entity_id=entity_id,
            as_of=as_of,
            missing=missing,
            stale=stale,
            conflicts=conflicts,
            completeness=completeness,
            optional_missing=optional_missing,
        )

"""Code review 修复的回归验收（31 条意见中后端可测项）。

每条测试对应一个 review 复现路径：量级绑定、冻结快照输入、历史冲突过滤、
计算引用上下文、论断反方引用、无来源表值、原子问题更新、计划时态、
评估命名空间、计划进快照哈希、计算幂等身份、币种/期间口径、序列语义拆分。
"""

import json
import threading
from datetime import UTC, date, datetime

import pytest

from finance_agent.dossier.projector import DossierProjector, series_set
from finance_agent.dossier.service import DossierService
from finance_agent.eventstore.events import Event
from finance_agent.eventstore.store import EventStore
from finance_agent.harness.manifest import RunManifest, RunMode
from finance_agent.knowledge.metric_store import MetricStore
from finance_agent.knowledge.metric_writer import TypedMetricWriter
from finance_agent.knowledge.metrics import MetricPeriod, RawValue, ReportedObservation
from finance_agent.knowledge.models import Evidence, Fact, PitGrade
from finance_agent.knowledge.normalization import NormalizationError, normalize_raw
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.research.artifacts import (
    ArtifactValidator,
    ComparisonBlock,
    MetricCell,
    MetricTableBlock,
    ReportDocument,
    ResearchArtifact,
    ResearchClaim,
)
from finance_agent.research.calculations import CalculationError, CalculationService, InputRef

T0 = datetime(2024, 6, 1, tzinfo=UTC)
T1 = datetime(2025, 1, 31, tzinfo=UTC)
T2 = datetime(2025, 6, 1, tzinfo=UTC)
FY2024 = MetricPeriod(start=date(2024, 1, 1), end=date(2024, 12, 31),
                      frequency="FY", fiscal_label="FY2024")
RUN = RunManifest(run_id="r-fix", mode=RunMode.LIVE)


@pytest.fixture()
def env(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    metrics = MetricStore(tmp_path / "m.db")
    events = EventStore(tmp_path / "e.db")
    mw = TypedMetricWriter(store=metrics, kb=kb, events=events)
    calcs = CalculationService(metrics, events=events)
    projector = DossierProjector(kb=kb, metrics=metrics)
    service = DossierService(kb=kb, metrics=metrics, projector=projector, events=events)
    return kb, metrics, events, mw, calcs, projector, service


def seed_ev(kb, ev_id, quote, available_at=T0):
    kb.add_evidence(Evidence(
        evidence_id=ev_id, source_id="edgar", url=f"https://sec.gov/{ev_id}",
        verbatim_quote=quote, retrieved_at=available_at, available_at=available_at,
        pit_grade=PitGrade.A,
    ))


def seed_obs(kb, metrics, mw, *, ev_id, quote, value_text, metric_key="revenue",
             knowledge_time=T0, entity_id="BE", namespace="prod", dims=None,
             period=FY2024, unit_text=None):
    seed_ev(kb, ev_id, quote, available_at=knowledge_time)
    value, steps = normalize_raw(value_text, unit_text or "USD")
    obs = ReportedObservation(
        entity_kind="stock", entity_id=entity_id, metric_key=metric_key, period=period,
        dimensions=dims or {}, value=value, unit="USD", currency="USD",
        raw=RawValue(value_text=value_text, unit_text=unit_text or "USD"),
        normalization=[s.model_dump(mode="json") for s in steps],
        evidence_refs=[ev_id], knowledge_time=knowledge_time,
        source_available_at=knowledge_time, retrieved_at=knowledge_time,
        created_at=knowledge_time, pit_grade=PitGrade.A,
    )
    return mw.write_observation(
        obs, run=RUN, namespace=namespace
    )[0]


# ---------------- #1 量级绑定 ----------------


class TestMagnitudeBinding:
    def test_scale_word_swap_rejected(self, env):
        """证据「1500 million」提交「1500 billion」→ 拒（错误放大 1000 倍）。"""
        kb, metrics, events, mw, *_ = env
        seed_ev(kb, "ev-m1", "Total revenue 1500 million for fiscal 2024")
        value, steps = normalize_raw("1500 billion", "USD")
        obs = ReportedObservation(
            entity_kind="stock", entity_id="BE", metric_key="revenue", period=FY2024,
            value=value, unit="USD", currency="USD",
            raw=RawValue(value_text="1500 billion", unit_text="USD"),
            normalization=[s.model_dump(mode="json") for s in steps],
            evidence_refs=["ev-m1"], knowledge_time=T0, source_available_at=T0,
            retrieved_at=T0, created_at=T0, pit_grade=PitGrade.A,
        )
        with pytest.raises(NormalizationError, match="量级未绑定"):
            mw.write_observation(obs, run=RUN)
        assert metrics.observations_as_of("stock", "BE", T2) == []

    def test_dropped_scale_word_rejected(self, env):
        """证据「1500 million」提交「1500」且无单位词 → 拒（静默缩小 100 万倍）。"""
        kb, metrics, events, mw, *_ = env
        seed_ev(kb, "ev-m2", "Total revenue 1500 million for fiscal 2024")
        obs = ReportedObservation(
            entity_kind="stock", entity_id="BE", metric_key="revenue", period=FY2024,
            value="1500", unit="USD", currency="USD",
            raw=RawValue(value_text="1500", unit_text="USD"),
            evidence_refs=["ev-m2"], knowledge_time=T0, source_available_at=T0,
            retrieved_at=T0, created_at=T0, pit_grade=PitGrade.A,
        )
        with pytest.raises(NormalizationError, match="量级未绑定"):
            mw.write_observation(obs, run=RUN)

    def test_table_header_unit_pattern_accepted(self, env):
        """财报表头模式：unit_text 声明 'USD millions'，摘录含表头与数字（共现即可）。"""
        kb, metrics, events, mw, *_ = env
        oid = seed_obs(kb, metrics, mw, ev_id="ev-m3",
                       quote="In millions, except per share data. Total revenue 1500",
                       value_text="1500", unit_text="USD millions")
        assert metrics.get_observation(oid).value == "1500000000"

    def test_paren_negative_preserved(self):
        """#30：会计括号负数不得记成盈利。"""
        assert normalize_raw("(1,234)", "USD")[0] == "-1234"
        assert normalize_raw("net loss ( 1500 ) million", "USD")[0] == "-1500000000"


# ---------------- #2/#3 冻结快照输入与历史冲突 ----------------


class TestFrozenSnapshot:
    def test_module_payload_frozen_against_backdated_insert(self, env):
        """补录历史观测（knowledge_time ≤ as_of）不得改变已冻结快照的模块内容。"""
        kb, metrics, events, mw, calcs, projector, service = env
        seed_obs(kb, metrics, mw, ev_id="ev-f1", quote="revenue 1500 million",
                 value_text="1500 million")
        snap, _ = service.open("stock", "BE")
        sid = snap["context"]["snapshot_id"]
        before = service.module(sid, "financial_quality")
        rev_before = [s for s in before.payload["fy"]["series"] if s["metric_key"] == "revenue"]
        assert rev_before[0]["points"][0]["value"] == "1500000000"
        # 事后补录一条 knowledge_time 早于 as_of 的重述观测（16 亿）
        seed_obs(kb, metrics, mw, ev_id="ev-f2", quote="restated revenue 1600 million",
                 value_text="1600 million", knowledge_time=T0)
        after = service.module(sid, "financial_quality")
        rev_after = [s for s in after.payload["fy"]["series"] if s["metric_key"] == "revenue"]
        # 冻结快照不变：仍是发布时的 15 亿（重述进入新快照，不是旧快照）
        assert rev_after[0]["points"][-1]["value"] == "1500000000" or \
               all(p["value"] != "1600000000" for p in rev_after[0]["points"])
        # 漂移可见：提示刷新而非静默替换
        assert any("刷新" in r for r in after.reasons)
        # 重新打开 → 新快照包含重述
        snap2, created = service.open("stock", "BE")
        assert created and snap2["data_hash"] != snap["data_hash"]

    def test_frozen_series_endpoint(self, env):
        kb, metrics, events, mw, calcs, projector, service = env
        seed_obs(kb, metrics, mw, ev_id="ev-s1", quote="revenue 1500 million",
                 value_text="1500 million")
        snap, _ = service.open("stock", "BE")
        out = service.frozen_series(snap["context"]["snapshot_id"], metric="revenue")
        assert out["frozen"] is True
        assert out["series"][0]["points"][0]["value"] == "1500000000"
        # 补录后冻结序列不变
        seed_obs(kb, metrics, mw, ev_id="ev-s2", quote="revenue 9999 million",
                 value_text="9999 million", knowledge_time=T0)
        out2 = service.frozen_series(snap["context"]["snapshot_id"], metric="revenue")
        assert all(p["value"] == "1500000000" for p in out2["series"][0]["points"])

    def test_historical_conflict_not_leaked(self, env):
        """#3：T1 才可知的竞争值不进 T0.5 的历史快照（冲突状态/版本链均过滤）。"""
        kb, metrics, events, mw, calcs, projector, service = env
        seed_obs(kb, metrics, mw, ev_id="ev-c1", quote="revenue 1500 million",
                 value_text="1500 million", knowledge_time=T0)
        t_mid = datetime(2024, 9, 1, tzinfo=UTC)
        seed_obs(kb, metrics, mw, ev_id="ev-c2", quote="revised revenue 1600 million",
                 value_text="1600 million", knowledge_time=T1)
        # 当前视图：冲突可见
        assert metrics.conflicted_semantic_hashes("stock", "BE", as_of=T2,
                                                  exclude_resolved=True) != []
        # 历史视图（T1 前）：竞争值尚不可知 → 无冲突
        assert metrics.conflicted_semantic_hashes("stock", "BE", as_of=t_mid,
                                                  exclude_resolved=True) == []
        snap_hist, _ = service.open("stock", "BE", as_of=t_mid, mode="historical")
        km = {m["metric_key"]: m for m in snap_hist["summary"]["key_metrics"]}
        assert km["revenue"]["status"] != "conflicted"
        assert km["revenue"]["value"] == "1500000000"
        rs = service.module(snap_hist["context"]["snapshot_id"], "research_sources")
        assert rs.payload["observation_conflicts"] == []
        # 版本链按 as_of 截断：历史页面看不到未来重述
        sem = metrics.conflicted_semantic_hashes("stock", "BE")[0]
        hist = metrics.observation_history(sem, as_of=t_mid)
        assert [o.value for o in hist] == ["1500000000"]


# ---------------- #4/#13/#14 计算引用上下文 ----------------


class TestCalculationContext:
    def test_cross_namespace_ref_rejected(self, env):
        kb, metrics, events, mw, calcs, *_ = env
        oid = seed_obs(kb, metrics, mw, ev_id="ev-n1", quote="revenue 1500 million",
                       value_text="1500 million", namespace="eval:run-9")
        with pytest.raises(CalculationError, match="命名空间"):
            calcs.calculate(
                entity_kind="stock", entity_id="BE", formula_id="yoy_growth",
                inputs=[InputRef(kind="observation", label="current", ref_id=oid),
                        InputRef(kind="assumption", label="prior", value="1000000000")],
                namespace="prod",
            )

    def test_cross_entity_ref_rejected(self, env):
        """BE 的计算引用 OTHER 实体的观测 → 拒（review #4 复现路径）。"""
        kb, metrics, events, mw, calcs, *_ = env
        oid = seed_obs(kb, metrics, mw, ev_id="ev-x1", quote="revenue 1500 million",
                       value_text="1500 million", entity_id="OTHER")
        with pytest.raises(CalculationError, match="跨实体"):
            calcs.calculate(
                entity_kind="stock", entity_id="BE", formula_id="yoy_growth",
                inputs=[InputRef(kind="observation", label="current", ref_id=oid),
                        InputRef(kind="assumption", label="prior", value="1000000000")],
            )

    def test_as_of_binding_rejects_future_obs(self, env):
        kb, metrics, events, mw, calcs, *_ = env
        oid = seed_obs(kb, metrics, mw, ev_id="ev-a1", quote="revenue 1500 million",
                       value_text="1500 million", knowledge_time=T1)
        with pytest.raises(CalculationError, match="尚不可知"):
            calcs.calculate(
                entity_kind="stock", entity_id="BE", formula_id="yoy_growth",
                inputs=[InputRef(kind="observation", label="current", ref_id=oid),
                        InputRef(kind="assumption", label="prior", value="1000000000")],
                as_of=T0,
            )

    def test_input_hash_includes_entity_identity(self, env):
        """#13：同数值不同实体 → 不同 calculation（归属与血缘不错位）。"""
        kb, metrics, events, mw, calcs, *_ = env
        inputs = [InputRef(kind="assumption", label="current", value="150"),
                  InputRef(kind="assumption", label="prior", value="100")]
        r1 = calcs.calculate(entity_kind="stock", entity_id="BE",
                             formula_id="yoy_growth", inputs=inputs)
        r2 = calcs.calculate(entity_kind="stock", entity_id="OTHER",
                             formula_id="yoy_growth", inputs=inputs)
        assert r1.calculation_id != r2.calculation_id
        assert metrics.get_calculation(r2.calculation_id).entity_id == "OTHER"

    def test_mixed_currency_rejected(self, env):
        """#14：100 USD − 100 HKD 不得返回 0。"""
        kb, metrics, events, mw, calcs, *_ = env
        with pytest.raises(CalculationError, match="币种不一致"):
            calcs.calculate(
                entity_kind="stock", entity_id="BE", formula_id="net_debt",
                inputs=[
                    InputRef(kind="assumption", label="total_debt", value="100", currency="USD"),
                    InputRef(kind="assumption", label="cash", value="100", currency="HKD"),
                ],
            )

    def test_ttm_generic_entry_requires_quarter_obs(self, env):
        """#14：同一年度值重复四次冒充季度 → 通用入口拒绝。"""
        kb, metrics, events, mw, calcs, *_ = env
        oid = seed_obs(kb, metrics, mw, ev_id="ev-t1", quote="revenue 1500 million",
                       value_text="1500 million")
        with pytest.raises(CalculationError):
            calcs.calculate(
                entity_kind="stock", entity_id="BE", formula_id="ttm_sum",
                inputs=[InputRef(kind="observation", label=f"q{i}", ref_id=oid)
                        for i in range(1, 5)],
            )
        # 裸数字拼季度同样拒绝
        with pytest.raises(CalculationError):
            calcs.calculate(
                entity_kind="stock", entity_id="BE", formula_id="ttm_sum",
                inputs=[InputRef(kind="assumption", label=f"q{i}", value="100")
                        for i in range(1, 5)],
            )


# ---------------- #5/#6 论断与报告验证 ----------------


class TestClaimAndReportValidation:
    def test_counter_ref_unresolvable_blocks_validated(self, env):
        """#5：反方引用不存在 → 不得 validated。"""
        kb, metrics, *_ = env
        seed_ev(kb, "ev-r1", "some disclosure text")
        from finance_agent.research.artifacts import ref_resolvable
        assert ref_resolvable(kb, metrics, "ev-r1")
        assert not ref_resolvable(kb, metrics, "ev-nonexistent")
        # 跨命名空间观测引用不可支撑 prod 论断
        oid = seed_obs(kb, metrics, env[3], ev_id="ev-r2", quote="revenue 1500 million",
                       value_text="1500 million", namespace="eval:x")
        assert not ref_resolvable(kb, metrics, oid, namespace="prod")
        assert ref_resolvable(kb, metrics, oid, namespace="eval:x")
        # 跨实体引用不可支撑
        oid2 = seed_obs(kb, metrics, env[3], ev_id="ev-r3", quote="revenue 200 million",
                        value_text="200 million", entity_id="OTHER")
        assert not ref_resolvable(kb, metrics, oid2, namespace="prod",
                                  entity_kind="stock", entity_id="BE")

    def test_validator_rechecks_claim_refs(self, env):
        """#5：报告验证不再只查 claim 存在——claim 的坏引用同样硬失败。"""
        kb, metrics, *_ = env
        seed_ev(kb, "ev-v1", "disclosure")
        bad_claim = ResearchClaim(
            entity_kind="stock", entity_id="BE", statement="订单转化率高于同业水平", kind="inference",
            support_refs=["ev-v1"], counter_refs=["ev-ghost"], status="draft",
            created_at=T0,
        ).with_id()
        # 直接落库模拟历史脏数据（写入工具会拦，验证层兜底）
        metrics.save_claim(claim_id=bad_claim.claim_id, namespace="prod",
                           payload=bad_claim.model_dump(mode="json"))
        doc = ReportDocument(
            title="t", entity_kind="stock", entity_id="BE",
            blocks=[{"type": "claim", "claim_id": bad_claim.claim_id}],
        ).with_id()
        art = ResearchArtifact(entity_kind="stock", entity_id="BE", title="t",
                               report_document=doc, claim_ids=[bad_claim.claim_id],
                               created_at=T0).with_id()
        issues = ArtifactValidator(kb=kb, metric_store=metrics).validate(art)
        assert any(i.code == "unresolved_claim_ref" and i.hard and i.ref == "ev-ghost"
                   for i in issues)

    def test_metric_table_unsourced_value_hard_fails(self, env):
        """#6：表内无来源大数不得零问题通过。"""
        kb, metrics, *_ = env
        doc = ReportDocument(
            title="t", entity_kind="stock", entity_id="BE",
            blocks=[MetricTableBlock(
                title="财务", columns=["指标", "值"],
                rows=[[MetricCell(label="收入"), MetricCell(label="收入", value="9876543210")]],
            )],
        ).with_id()
        art = ResearchArtifact(entity_kind="stock", entity_id="BE", title="t",
                               report_document=doc, created_at=T0).with_id()
        issues = ArtifactValidator(kb=kb, metric_store=metrics).validate(art)
        assert any(i.code == "unsourced_metric_cell" and i.hard for i in issues)

    def test_metric_table_with_observation_ref_passes(self, env):
        kb, metrics, events, mw, *_ = env
        oid = seed_obs(kb, metrics, mw, ev_id="ev-t9", quote="revenue 1500 million",
                       value_text="1500 million")
        doc = ReportDocument(
            title="t", entity_kind="stock", entity_id="BE",
            blocks=[MetricTableBlock(
                title="财务", columns=["指标", "值"],
                rows=[[MetricCell(label="收入"), MetricCell(label="收入", observation_id=oid)]],
            )],
        ).with_id()
        art = ResearchArtifact(entity_kind="stock", entity_id="BE", title="t",
                               report_document=doc, created_at=T0).with_id()
        issues = ArtifactValidator(kb=kb, metric_store=metrics).validate(art)
        assert not [i for i in issues if i.hard]

    def test_metric_table_fake_observation_ref_hard_fails(self, env):
        kb, metrics, *_ = env
        doc = ReportDocument(
            title="t", entity_kind="stock", entity_id="BE",
            blocks=[MetricTableBlock(
                title="财务", columns=["指标", "值"],
                rows=[[MetricCell(label="收入", value="1", observation_id="obs-ghost")]],
            )],
        ).with_id()
        art = ResearchArtifact(entity_kind="stock", entity_id="BE", title="t",
                               report_document=doc, created_at=T0).with_id()
        issues = ArtifactValidator(kb=kb, metric_store=metrics).validate(art)
        assert any(i.code == "unresolved_observation" and i.ref == "obs-ghost" for i in issues)

    def test_comparison_block_validated(self, env):
        """#6：comparison 此前无验证分支——裸值与坏引用现在都被拦。"""
        kb, metrics, *_ = env
        doc = ReportDocument(
            title="t", entity_kind="stock", entity_id="BE",
            blocks=[ComparisonBlock(title="对比", items=[
                MetricCell(label="BE 收入", value="123456789"),
                MetricCell(label="幽灵", value="1", observation_id="obs-ghost"),
            ])],
        ).with_id()
        art = ResearchArtifact(entity_kind="stock", entity_id="BE", title="t",
                               report_document=doc, created_at=T0).with_id()
        issues = ArtifactValidator(kb=kb, metric_store=metrics).validate(art)
        codes = {(i.code, i.ref) for i in issues}
        assert ("unsourced_metric_cell", "对比:BE 收入") in codes or \
               any(i.code == "unsourced_metric_cell" for i in issues)
        assert any(i.code == "unresolved_observation" and i.ref == "obs-ghost" for i in issues)


# ---------------- #8 原子问题更新 ----------------


class TestAtomicQuestionUpdate:
    def test_parallel_updates_do_not_clobber(self, env):
        """#8 复现路径：两个 worker 并发回答不同问题，两个状态都必须保留。"""
        _, metrics, *_ = env
        plan = {
            "plan_id": "plan-c1", "entity_kind": "stock", "entity_id": "BE",
            "objective": "并发测试", "mode": "deep", "recipe_id": "general",
            "recipe_version": "1", "created_at": T0.isoformat(), "status": "active",
            "questions": [
                {"question_id": "q-1", "text": "问题1", "priority": "high", "status": "unanswered"},
                {"question_id": "q-2", "text": "问题2", "priority": "high", "status": "unanswered"},
            ],
            "acceptance": "", "budgets": {}, "scope": {},
        }
        metrics.save_plan(plan_id="plan-c1", namespace="prod", payload=json.loads(json.dumps(plan)))
        barrier = threading.Barrier(2)

        def answer(qid):
            barrier.wait()
            for _ in range(20):  # 放大竞态窗口
                metrics.update_plan_question("plan-c1", qid, {
                    "status": "answered", "conclusion": f"{qid} 结论",
                    "support_refs": ["ev-1"],
                })

        threads = [threading.Thread(target=answer, args=(q,)) for q in ("q-1", "q-2")]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        final = metrics.get_plan("plan-c1")
        statuses = {q["question_id"]: q["status"] for q in final["questions"]}
        assert statuses == {"q-1": "answered", "q-2": "answered"}  # 无丢失更新


# ---------------- #10/#11/#12 计划时态 / 评估命名空间 / 快照哈希 ----------------


class TestTemporalProjections:
    def test_future_plan_not_in_historical_snapshot(self, env):
        """#10：2025 年创建的计划不得出现在 2024 年快照里。"""
        kb, metrics, events, mw, calcs, projector, service = env
        seed_obs(kb, metrics, mw, ev_id="ev-p1", quote="revenue 1500 million",
                 value_text="1500 million", knowledge_time=T0)
        metrics.save_plan(plan_id="plan-future", namespace="prod", payload={
            "plan_id": "plan-future", "entity_kind": "stock", "entity_id": "BE",
            "objective": "未来研究", "mode": "deep", "recipe_id": "biotech",
            "recipe_version": "1", "created_at": T1.isoformat(), "status": "active",
            "questions": [{"question_id": "q-x", "text": "?", "priority": "high",
                           "status": "unanswered"}],
            "acceptance": "", "budgets": {}, "scope": {},
        })
        snap_hist, _ = service.open("stock", "BE", as_of=datetime(2024, 9, 1, tzinfo=UTC),
                                    mode="historical")
        assert snap_hist["recipe"]["id"] != "biotech"  # 配方不采用未来计划的选择
        rs = service.module(snap_hist["context"]["snapshot_id"], "research_sources")
        assert all(p["plan_id"] != "plan-future" for p in rs.payload["plans"])
        # 当前视图能看到该计划
        snap_now, _ = service.open("stock", "BE")
        assert snap_now["recipe"]["id"] == "biotech"

    def test_plan_updated_after_as_of_masked_in_history(self, env):
        """#10：既有计划在快照后被更新 → 历史投影状态标 historical_unknown。"""
        kb, metrics, events, mw, calcs, projector, service = env
        metrics.save_plan(plan_id="plan-u1", namespace="prod", payload={
            "plan_id": "plan-u1", "entity_kind": "stock", "entity_id": "BE",
            "objective": "研究", "mode": "standard", "recipe_id": "general",
            "recipe_version": "1", "created_at": T0.isoformat(), "status": "active",
            "questions": [{"question_id": "q-1", "text": "?", "priority": "high",
                           "status": "unanswered"}],
            "acceptance": "", "budgets": {}, "scope": {},
        })
        snap, _ = service.open("stock", "BE")
        # 快照后计划被推进（问题 answered）
        metrics.update_plan_question("plan-u1", "q-1", {
            "status": "answered", "conclusion": "今天的结论", "support_refs": ["ev-x"],
        })
        # 旧冻结快照的 inputs.plan 仍是发布时状态（frozen）
        rs = service.module(snap["context"]["snapshot_id"], "research_sources")
        q = rs.payload["plans"][0]["questions"][0]
        assert q["status"] == "unanswered"  # 不被今日更新覆盖
        # 历史重开（as_of 早于 updated_at）→ 状态不可分辨
        snap_hist, _ = service.open("stock", "BE", as_of=T0, mode="historical")
        rs_h = service.module(snap_hist["context"]["snapshot_id"], "research_sources")
        q_h = rs_h.payload["plans"][0]["questions"][0]
        assert q_h["status"] == "historical_unknown"
        assert rs_h.payload["plan_notes"]

    def test_assessment_namespace_and_time_filter(self, env):
        """#11：eval 评估不进生产档案；存在未来评估时返回当时最新一条。"""
        kb, metrics, events, mw, calcs, projector, service = env
        seed_obs(kb, metrics, mw, ev_id="ev-a9", quote="revenue 1500 million",
                 value_text="1500 million", knowledge_time=T0)
        events.append(Event(run_id="r-old", type="research/assessment", payload={
            "entity": "stock:BE", "namespace": "prod", "verdict": "partial",
            "created_at": T0.isoformat(), "assessment_id": "a-old",
        }))
        events.append(Event(run_id="r-eval", type="research/assessment", payload={
            "entity": "stock:BE", "namespace": "eval:run-1", "verdict": "sufficient",
            "created_at": T1.isoformat(), "assessment_id": "a-eval",
        }))
        events.append(Event(run_id="r-future", type="research/assessment", payload={
            "entity": "stock:BE", "namespace": "prod", "verdict": "sufficient",
            "created_at": T2.isoformat(), "assessment_id": "a-future",
        }))
        snap, _ = service.open("stock", "BE")  # live as_of ≈ now ≥ T2 → 看到 a-future
        mod = service.module(snap["context"]["snapshot_id"], "investment_snapshot")
        assert mod.payload["assessment"]["assessment_id"] == "a-future"
        # 历史视图（T1 前）：只看到当时的 prod 评估，不借用未来/eval
        snap_h, _ = service.open("stock", "BE", as_of=datetime(2024, 9, 1, tzinfo=UTC),
                                 mode="historical")
        mod_h = service.module(snap_h["context"]["snapshot_id"], "investment_snapshot")
        assert mod_h.payload["assessment"]["assessment_id"] == "a-old"

    def test_plan_and_questions_enter_snapshot_hash(self, env):
        """#12：新计划/问题推进 → 新快照身份（首屏覆盖不再滞后）。"""
        kb, metrics, events, mw, calcs, projector, service = env
        snap1, _ = service.open("stock", "BE")
        metrics.save_plan(plan_id="plan-h1", namespace="prod", payload={
            "plan_id": "plan-h1", "entity_kind": "stock", "entity_id": "BE",
            "objective": "研究", "mode": "standard", "recipe_id": "general",
            "recipe_version": "1", "created_at": datetime.now(UTC).isoformat(),
            "status": "active",
            "questions": [{"question_id": "q-1", "text": "?", "priority": "high",
                           "status": "unanswered"}],
            "acceptance": "", "budgets": {}, "scope": {},
        })
        snap2, created2 = service.open("stock", "BE")
        assert created2 and snap2["data_hash"] != snap1["data_hash"]
        assert snap2["research"]["required"] == 1
        metrics.update_plan_question("plan-h1", "q-1", {
            "status": "answered", "conclusion": "结论", "support_refs": ["ev-1"],
        })
        snap3, created3 = service.open("stock", "BE")
        assert created3 and snap3["data_hash"] != snap2["data_hash"]
        assert snap3["research"]["answered"] == 1


# ---------------- #15/#22/#24 序列拆分 / claim 证据 / 行业内容 ----------------


class TestProjectionSemantics:
    def test_series_split_by_full_semantics(self, env):
        """#15：分部/合并/指引各自成序列，不混合投影。"""
        kb, metrics, events, mw, *_ = env
        seed_obs(kb, metrics, mw, ev_id="ev-g1", quote="total revenue 1500 million",
                 value_text="1500 million")
        seed_obs(kb, metrics, mw, ev_id="ev-g2", quote="segment revenue 900 million",
                 value_text="900 million", dims={"segment": "Energy"})
        from finance_agent.knowledge.metrics import GuidanceObservation
        seed_ev(kb, "ev-g3", "guidance: revenue 2000 million next year", available_at=T0)
        value, steps = normalize_raw("2000 million", "USD")
        gw = GuidanceObservation(
            entity_kind="stock", entity_id="BE", metric_key="revenue",
            period=MetricPeriod(start=date(2025, 1, 1), end=date(2025, 12, 31),
                                frequency="FY", fiscal_label="FY2025"),
            value=value, unit="USD", currency="USD",
            raw=RawValue(value_text="2000 million", unit_text="USD"),
            normalization=[s.model_dump(mode="json") for s in steps],
            evidence_refs=["ev-g3"], issuer="公司管理层", guidance_published_at=T0,
            target_period=MetricPeriod(start=date(2025, 1, 1), end=date(2025, 12, 31),
                                       frequency="FY"),
            knowledge_time=T0, source_available_at=T0, retrieved_at=T0, created_at=T0,
            pit_grade=PitGrade.A,
        )
        mw.write_observation(gw, run=RUN)
        obs = metrics.observations_as_of("stock", "BE", T2)
        s = series_set(obs, ["revenue"])
        assert len(s.series) == 3  # 合并 reported / 分部 reported / FY2025 guidance
        natures = {(tuple(sorted(x.dimensions.items())), x.nature) for x in s.series}
        assert (((("segment", "Energy"),), "reported") in natures)
        assert (((), "guidance") in natures)
        # 主图过滤：只留合并披露值
        consolidated = [o for o in obs if not o.dimensions and o.nature == "reported"]
        s2 = series_set(consolidated, ["revenue"])
        assert len(s2.series) == 1
        assert s2.series[0].points[0].value == "1500000000"

    def test_claim_only_evidence_in_snapshot_refs(self, env):
        """#22：仅由论断引用的证据进快照来源目录（抽屉不再 404）。"""
        kb, metrics, events, mw, calcs, projector, service = env
        seed_ev(kb, "ev-only-claim", "disclosure used only by a claim")
        claim = ResearchClaim(
            entity_kind="stock", entity_id="BE", statement="仅证据支撑的论断",
            kind="inference", support_refs=["ev-only-claim"], status="validated",
            created_at=T0,
        ).with_id()
        metrics.save_claim(claim_id=claim.claim_id, namespace="prod",
                           payload=claim.model_dump(mode="json"))
        snap, _ = service.open("stock", "BE")
        assert "ev-only-claim" in snap["evidence_refs"]

    def test_industry_value_chain_projected(self, env):
        """#24 + audit §3.6：行业档案的产业链内容不再空投影，且不再借用股票模块改标题。"""
        kb, metrics, events, mw, calcs, projector, service = env
        seed_ev(kb, "ev-i1", "AI for Science value chain description")
        kb.assert_fact(Fact(
            entity_kind="industry", entity_id="ai-for-science", field="value_chain",
            value="上游算力 → 中游平台 → 下游科研应用", knowledge_time=T0,
            evidence_ids=["ev-i1"],
        ))
        snap, _ = service.open("industry", "ai-for-science")
        # 行业信息架构由注册表定义：产业链有自己的模块，股票专属模块标 not_applicable
        assert snap["modules"]["industry_chain"]["title"] == "产业链与技术路线"
        assert snap["modules"]["business_engine"]["status"] == "not_applicable"
        registry = snap["module_registry"]
        assert registry["registry_version"] and registry["entity_kind"] == "industry"
        nav = [m["module_id"] for m in registry["modules"] if m["default_nav"]]
        assert nav[:3] == ["investment_snapshot", "industry_chain", "candidate_pool"]
        mod = service.module(snap["context"]["snapshot_id"], "industry_chain")
        assert "上游算力" in mod.payload["graph"]["narrative"]
        assert mod.payload["graph"]["narrative_refs"]
        # 尚无结构化图时诚实告知，不拿叙述冒充关系图
        assert mod.payload["nodes"] == []
        assert any("尚无结构化产业链图" in n for n in mod.payload["notes"])

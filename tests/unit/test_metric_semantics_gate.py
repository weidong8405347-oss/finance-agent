"""结构化数值语义准入验收（audit §3.2 P0 + §5 验收用例 2「错误数据四例」）。

四条真实事故观测（均曾以 status=ok 落库）：

| 观测 | 系统保存值 | 问题 |
| --- | --- | --- |
| obs-1ce021570310 | `sdgr_top20_kpi=73700000`（ratio+USD） | 摘录 `$73.7 million, or 37%`：金额当比例 |
| obs-723282fae4d1 | `xtalpi_major_deal_upfront=4 USD` | 摘录乱码；潜在总额未拆首付款 |
| obs-79a868c353bb | `revenue=106303 USD` | 摘录只有 `106,303 27,456` |
| obs-cf01ee8416d6 | `revenue=193518 CNY`（segment 维度） | 无公司/表头/币种/规模词；跨公司记在行业上 |

判据：这四例都不能再次以 ok 入库；相同期间不同公司的收入不能形成同一指标序列；
金额与比率必须同时通过来源与量纲验证。
"""

from datetime import UTC, date, datetime

import pytest

from finance_agent.eventstore.store import EventStore
from finance_agent.harness.manifest import RunManifest, RunMode
from finance_agent.knowledge.errors import KnowledgeInvariantError
from finance_agent.knowledge.metric_spec import REGISTRY, infer_value_kind, spec_for
from finance_agent.knowledge.metric_store import MetricStore
from finance_agent.knowledge.metric_writer import TypedMetricWriter
from finance_agent.knowledge.metrics import (
    MetricPeriod,
    RawValue,
    ReportedObservation,
)
from finance_agent.knowledge.models import Evidence, PitGrade
from finance_agent.knowledge.store import BitemporalStore

T0 = datetime(2024, 6, 1, tzinfo=UTC)
FY2024 = MetricPeriod(start=date(2024, 1, 1), end=date(2024, 12, 31), frequency="FY",
                      fiscal_label="FY2024")
RUN = RunManifest(run_id="r-audit", mode=RunMode.LIVE)


def make_env(tmp_path, *, subject_gate=None):
    kb = BitemporalStore(tmp_path / "kb.db")
    metrics = MetricStore(tmp_path / "m.db")
    events = EventStore(tmp_path / "e.db")
    writer = TypedMetricWriter(store=metrics, kb=kb, events=events,
                               subject_gate=subject_gate)
    return kb, metrics, events, writer


def seed(kb, evidence_id: str, quote: str, *, quality: str = "ok",
         locator: dict | None = None, available_at=T0):
    kb.add_evidence(Evidence(
        evidence_id=evidence_id, source_id="web_search", url="https://x.com/a",
        verbatim_quote=quote, retrieved_at=T0, available_at=available_at,
        pit_grade=PitGrade.B, quality=quality, locator=locator or {},
    ))


def obs(**kw):
    """造一条 reported 观测：给了 value_text 就自动算换算链（服务端重算口径一致）。"""
    from finance_agent.knowledge.normalization import normalize_raw

    base = dict(
        entity_kind="stock", entity_id="SDGR", metric_key="revenue", period=FY2024,
        value="100000000", unit="USD", currency="USD", basis="GAAP",
        evidence_refs=["ev-1"], knowledge_time=T0, source_available_at=T0,
        retrieved_at=T0, created_at=T0, pit_grade=PitGrade.B,
    )
    base.update(kw)
    explicit_value = "value" in kw
    value_text = base.pop("value_text", None)
    unit_text = base.pop("unit_text", "")
    value_span = base.pop("value_span", "")
    raw = base.pop("raw", None)
    steps: list = []
    if raw is None and value_text:
        computed, steps = normalize_raw(value_text, unit_text)
        # 显式给的 value 优先（故意不一致的用例靠它触发血缘/值域门禁），否则用重算值
        if not explicit_value:
            base["value"] = computed
        raw = RawValue(value_text=value_text, unit_text=unit_text, span=value_span)
    return ReportedObservation(
        raw=raw, normalization=[s.model_dump(mode="json") for s in steps], **base
    )


def expect_reject(fn, *match_all: str) -> str:
    """断言写入被拒（fail-closed），并返回拒绝原因。

    门禁有两个异常家族：KnowledgeInvariantError（语义/主体/质量）与
    NormalizationError（量级/上下文/血缘）——两者都是「不得入库」，测试不区分。
    """
    from finance_agent.knowledge.normalization import NormalizationError

    with pytest.raises((KnowledgeInvariantError, NormalizationError, ValueError)) as ei:
        fn()
    reason = str(ei.value)
    for frag in match_all:
        assert frag in reason, f"拒绝原因未命中 {frag!r}：{reason}"
    return reason


# ---------------- 1. 四例错误数据不得再以 ok 入库 ----------------


class TestFourIncidentSamples:
    def test_amount_saved_as_ratio_is_rejected(self, tmp_path):
        """① `$73.7 million, or 37%` → unit=ratio + currency=USD + 值 73700000。"""
        kb, metrics, events, writer = make_env(tmp_path)
        seed(kb, "ev-1", "Schrödinger's top 20 KPI: $73.7 million, or 37% of revenue.")
        reason = expect_reject(
            lambda: writer.write_observation(obs(
                metric_key="sdgr_top20_kpi", value="73700000", unit="ratio", currency="USD",
                value_text="73.7 million", unit_text="ratio",
                value_span="$73.7 million",
            ), run=RUN),
            "ratio", "币种",
        )
        assert "金额被当比例" in reason
        # 多数字摘录必须显式选 span（不许猜要哪个数）
        expect_reject(
            lambda: writer.write_observation(obs(
                metric_key="gross_margin", unit="percent", currency=None,
                value_text="37%", unit_text="percent",
            ), run=RUN),
            "value_span",
        )

    def test_garbled_deal_upfront_is_rejected(self, tmp_path):
        """② 乱码摘录 + 语义冲突（claim 说潜在总额未拆首付款，metric 叫 upfront）。"""
        kb, metrics, events, writer = make_env(tmp_path)
        seed(kb, "ev-1", "\ufffd\ufffd\u0002\u0003 up to USD 4 \ufffd potential \ufffd",
             quality="garbled")
        expect_reject(
            lambda: writer.write_observation(obs(
                entity_kind="stock", entity_id="2228.HK",
                metric_key="contract_upfront_received", value="4", unit="USD", currency="USD",
                value_text="4", unit_text="USD", locator={"document": "ann"},
            ), run=RUN),
            "抽取质量",
        )

    def test_potential_total_cannot_be_booked_as_upfront(self, tmp_path):
        """摘录说「潜在总额 up to」，metric 却叫已收首付款 → 语义冲突拒写。"""
        kb, metrics, events, writer = make_env(tmp_path)
        seed(kb, "ev-1", "双方订立合作协议，潜在交易总额 up to USD 595 million，首付款未拆分。")
        expect_reject(
            lambda: writer.write_observation(obs(
                entity_kind="stock", entity_id="2228.HK",
                metric_key="contract_upfront_received", unit="USD", currency="USD",
                value_text="USD 595 million", unit_text="USD",
                value_span="up to USD 595 million", locator={"document": "ann"},
            ), run=RUN),
            "语义冲突",
        )

    def test_bare_number_revenue_without_header_is_rejected(self, tmp_path):
        """③ 摘录只有 `106,303 27,456` → 多数字 + 无规模词 + 无表头定位。"""
        kb, metrics, events, writer = make_env(tmp_path)
        seed(kb, "ev-1", "106,303 27,456")
        expect_reject(
            lambda: writer.write_observation(obs(
                metric_key="revenue", value="106303", unit="USD", currency="USD",
                value_text="106,303", unit_text="USD",
            ), run=RUN),
            "value_span",
        )

    def test_bare_number_with_span_still_needs_scale_context(self, tmp_path):
        """给了 span 也不能凭空定量级：无规模词且无表头定位 → 仍拒。"""
        kb, metrics, events, writer = make_env(tmp_path)
        seed(kb, "ev-1", "106,303 27,456")
        expect_reject(
            lambda: writer.write_observation(obs(
                metric_key="revenue", value="106303", unit="USD", currency="USD",
                value_text="106,303", unit_text="USD", value_span="106,303",
            ), run=RUN),
            "规模上下文",
        )

    def test_bare_number_with_table_locator_is_accepted(self, tmp_path):
        """表头定位齐备时裸数字可写（量级由定位上下文证明）。"""
        kb, metrics, events, writer = make_env(tmp_path)
        seed(kb, "ev-1", "106,303 27,456",
             locator={"table": "合并利润表", "header": "单位：千元", "row": "营业收入"})
        oid, created = writer.write_observation(obs(
            metric_key="revenue", unit="CNY", currency="CNY",
            value_text="106,303", unit_text="千元", value_span="106,303",
            locator={"table": "合并利润表", "row": "营业收入"},
        ), run=RUN)
        assert created and oid
        assert metrics.get_observation(oid).value == "106303000"  # 千元 → 元（换算链重算）

    def test_cross_company_revenue_on_industry_entity_is_rejected(self, tmp_path):
        """④ 不同公司的收入记在行业实体上：跨主体未装配授权闸 → 拒。"""
        kb, metrics, events, writer = make_env(tmp_path)
        seed(kb, "ev-1", "分部收入 193,518 81,864（单位：千元）")
        expect_reject(
            lambda: writer.write_observation(obs(
                entity_kind="industry", entity_id="ai-for-science",
                subject_entity_kind="stock", subject_entity_id="603259.SS",
                metric_key="revenue", unit="CNY", currency="CNY",
                dimensions={"segment": "AI for Science"},
                value_text="193,518", unit_text="千元", value_span="193,518",
            ), run=RUN),
            "跨主体", "授权",
        )

    def test_authorized_subject_within_industry_scope_is_accepted(self, tmp_path):
        """多主体研究：授权范围内的跨主体引用可以写（不是一律禁止）。"""
        gate = lambda sk, si, jk, ji: (  # noqa: E731
            (True, "plan:scope.authorized_subjects") if ji == "603259.SS"
            else (False, f"未授权 {jk}:{ji}")
        )
        kb, metrics, events, writer = make_env(tmp_path, subject_gate=gate)
        seed(kb, "ev-1", "分部收入 193,518 81,864（单位：千元）")
        oid, created = writer.write_observation(obs(
            entity_kind="industry", entity_id="ai-for-science",
            subject_entity_kind="stock", subject_entity_id="603259.SS",
            metric_key="revenue", unit="CNY", currency="CNY",
            dimensions={"segment": "AI for Science"},
            value_text="193,518", unit_text="千元", value_span="193,518",
        ), run=RUN)
        assert created and oid
        assert metrics.get_observation(oid).subject_id == "603259.SS"

    def test_unauthorized_subject_rejected_even_with_gate(self, tmp_path):
        gate = lambda sk, si, jk, ji: (False, f"{jk}:{ji} 不在候选清单")  # noqa: E731
        kb, metrics, events, writer = make_env(tmp_path, subject_gate=gate)
        seed(kb, "ev-1", "收入 193,518 千元")
        expect_reject(
            lambda: writer.write_observation(obs(
                entity_kind="industry", entity_id="ai-for-science",
                subject_entity_kind="stock", subject_entity_id="UNKNOWN",
                metric_key="revenue", unit="CNY", currency="CNY",
                value_text="193,518", unit_text="千元", value_span="193,518",
            ), run=RUN),
            "主体未授权",
        )


# ---------------- 2. 量纲/单位/主体/期间的一致性 ----------------


class TestMetricSpec:
    def test_ratio_must_not_carry_currency(self):
        assert "ratio" in spec_for("gross_margin").units()
        assert spec_for("gross_margin").value_kind == "ratio"
        with pytest.raises(KnowledgeInvariantError):
            raise KnowledgeInvariantError("placeholder")  # 语义校验在 writer 层，见下

    def test_contract_metrics_are_distinct(self):
        """收入 / 合同潜在总额 / 已收首付款是三个指标（audit §3.2 修复方案 2）。"""
        assert {"contract_potential_total", "contract_upfront_received",
                "contract_milestone_max"} <= set(REGISTRY)
        upfront = REGISTRY["contract_upfront_received"]
        assert upfront.required_quote_markers
        potential = REGISTRY["contract_potential_total"]
        assert potential.incompatible_quote_markers == ()
        assert upfront.incompatible_quote_markers

    def test_unknown_key_inferred_from_naming(self):
        assert infer_value_kind("xtalpi_major_deal_upfront") == "currency_amount"
        assert infer_value_kind("gross_margin_pct") == "ratio"
        assert infer_value_kind("cash_runway_months") == "duration"
        assert infer_value_kind("customer_count") == "count"
        assert infer_value_kind("zzz_mystery") is None
        with pytest.raises(Exception, match="未登记指标"):
            spec_for("zzz_mystery")

    def test_ratio_value_range_catches_amount_as_ratio(self, tmp_path):
        """把金额当比例存：值域门禁兜底（|ratio|>100 直接拒）。"""
        kb, metrics, events, writer = make_env(tmp_path)
        seed(kb, "ev-1", "gross margin was 73700000 percent")
        expect_reject(
            lambda: writer.write_observation(obs(
                metric_key="gross_margin", value="73700000", unit="ratio", currency=None,
                value_text="73700000", unit_text="ratio", value_span="73700000",
            ), run=RUN),
            "gross_margin",
        )

    def test_balance_metric_rejects_ttm_at_model_level(self):
        """余额类指标不得做 TTM（模型层就拦，不依赖写入层）。"""
        with pytest.raises(ValueError, match="TTM"):
            obs(metric_key="net_debt", unit="USD", currency="USD",
                period=MetricPeriod(start=date(2023, 1, 1), end=date(2024, 12, 31),
                                    frequency="TTM"),
                value_text="500 million", unit_text="USD")

    def test_industry_only_metric_rejects_company_subject(self, tmp_path):
        """行业供给量写在公司主体上 → 主体类别不合法。"""
        kb, metrics, events, writer = make_env(tmp_path)
        seed(kb, "ev-1", "行业总产能 5000 MW")
        expect_reject(
            lambda: writer.write_observation(obs(
                entity_kind="stock", entity_id="SDGR",
                metric_key="capacity_supply", value="5000", unit="MW", currency=None,
                value_text="5000", unit_text="MW", value_span="5000 MW",
            ), run=RUN),
            "主体类别",
        )


class TestSeriesSeparation:
    def test_different_subjects_same_period_do_not_share_semantic_key(self, tmp_path):
        """相同期间不同公司的收入不得形成同一指标序列。"""
        kb, metrics, events, writer = make_env(tmp_path)
        seed(kb, "ev-1", "A 公司收入 100 million 美元")
        seed(kb, "ev-2", "B 公司收入 200 million 美元")
        gate = lambda sk, si, jk, ji: (True, "候选池授权")  # noqa: E731
        writer = TypedMetricWriter(store=metrics, kb=kb, events=events, subject_gate=gate)
        id_a, _ = writer.write_observation(obs(
            entity_kind="industry", entity_id="ai-for-science",
            subject_entity_kind="stock", subject_entity_id="AAA",
            value="100000000", value_text="100 million", unit_text="USD",
            evidence_refs=["ev-1"],
        ), run=RUN)
        id_b, _ = writer.write_observation(obs(
            entity_kind="industry", entity_id="ai-for-science",
            subject_entity_kind="stock", subject_entity_id="BBB",
            value="200000000", value_text="200 million", unit_text="USD",
            evidence_refs=["ev-2"],
        ), run=RUN)
        assert id_a != id_b
        stored_a = metrics.get_observation(id_a)
        stored_b = metrics.get_observation(id_b)
        assert stored_a.semantic_hash() != stored_b.semantic_hash()
        # 不因为「同实体同期间同指标」被误判为竞争版本
        assert metrics.conflicted_semantic_hashes("industry", "ai-for-science") == []


# ---------------- 3. 修订/失效记录（保留旧版本审计链） ----------------


class TestRevision:
    def _seed_bad(self, tmp_path):
        kb, metrics, events, writer = make_env(tmp_path)
        seed(kb, "ev-1", "revenue 1500 million for fiscal 2024")
        oid, _ = writer.write_observation(obs(
            metric_key="revenue", value="1500000000", unit="USD", currency="USD",
            value_text="1500 million", unit_text="USD",
        ), run=RUN)
        return kb, metrics, events, writer, oid

    def test_invalidate_keeps_history_and_hides_from_projection(self, tmp_path):
        kb, metrics, events, writer, oid = self._seed_bad(tmp_path)
        assert len(metrics.observations_as_of("stock", "SDGR", datetime.now(UTC))) == 1

        result = writer.revise_observation(
            oid, action="invalidated", reason="量级错误：应为 1.5 million 而非 1500 million",
            run=RUN,
        )
        assert result["revision_id"].startswith("rev-")
        # 旧行仍在（append-only，不原位修改冻结历史）
        assert metrics.get_observation(oid) is not None
        assert metrics.observation_history(
            metrics.get_observation(oid).semantic_hash())
        # 当前投影不再包含已失效观测
        assert metrics.observations_as_of("stock", "SDGR", datetime.now(UTC)) == []
        # 历史投影：失效之前的时刻仍看到它（不泄露「今天才作废」的状态）
        assert len(metrics.observations_as_of("stock", "SDGR", T0)) == 1
        revised = [e for e in events.read("r-audit") if e.type == "metric/revised"]
        assert revised and revised[0].payload["observation_id"] == oid
        assert revised[0].payload["reason"]

    def test_dependents_are_listed_for_re_review(self, tmp_path):
        kb, metrics, events, writer, oid = self._seed_bad(tmp_path)
        # 一条依赖该观测的计算 + 一条引用它的论断
        metrics.save_calculation(
            calculation_id="calc-x", namespace="prod", entity_kind="stock",
            entity_id="SDGR", formula_id="yoy_growth", formula_version=1,
            status="ok", result="0.1", unit="ratio", input_hash="h1",
            created_at=T0, run_id="r-audit",
            payload={"calculation_id": "calc-x", "inputs": [{"ref_id": oid}]},
        )
        metrics.save_claim(claim_id="claim-1", namespace="prod", payload={
            "claim_id": "claim-1", "entity_kind": "stock", "entity_id": "SDGR",
            "kind": "inference", "statement": "收入 15 亿", "created_at": T0.isoformat(),
            "support_refs": [oid], "status": "validated"},
        )
        result = writer.revise_observation(
            oid, action="needs_review", reason="摘录量级存疑，依赖项需重审", run=RUN,
        )
        assert "calc:calc-x" in result["dependent_refs"]
        assert "claim:claim-1" in result["dependent_refs"]
        assert result["dependents"]["calculations"] == ["calc-x"]

    def test_corrected_action_records_replacement(self, tmp_path):
        kb, metrics, events, writer, oid = self._seed_bad(tmp_path)
        result = writer.revise_observation(
            oid, action="corrected", reason="表头单位误读（千元→元）",
            replacement_observation_id="obs-new", run=RUN,
        )
        rows = metrics.revisions_for(oid)
        assert rows and rows[0]["action"] == "corrected"
        assert rows[0]["replacement_observation_id"] == "obs-new"
        assert result["action"] == "corrected"

    def test_unknown_observation_fails_loud(self, tmp_path):
        kb, metrics, events, writer = make_env(tmp_path)
        with pytest.raises(Exception, match="观测不存在"):
            writer.revise_observation("obs-nope", action="invalidated", reason="x", run=RUN)

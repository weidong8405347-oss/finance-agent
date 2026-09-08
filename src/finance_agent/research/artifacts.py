"""研究产物：ResearchClaim / ReportDocument / ResearchArtifact（设计 §6.1/§7.8）。

判断与事实分离：
- ResearchClaim = 分析层论断（fact_summary/inference/hypothesis/analysis），
  带支持/反方引用与限制；旧 thesis 兼容为 legacy analysis，不自动标成披露事实；
- ReportDocument = 固定 block 类型的结构化报告（LLM 产候选，服务端验证引用后渲染）；
- ResearchArtifact = 冻结研报（report_document + claim/calculation 引用 + 验证状态）。

数值纪律：关键数值经 metric_ref 插值（``{{metric:<observation_id>}}``），
自由文本里的事实数字在验证阶段对照允许值清单检查；旧 [ev-xxx] 锚点转可解析引用，
未知 id 明确报错（不静默丢弃）。
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ClaimKind = Literal["fact_summary", "inference", "hypothesis", "analysis"]
ClaimStatus = Literal["draft", "validated", "superseded"]
ArtifactSufficiency = Literal["sufficient", "partial", "blocked"]

#: 段落内引用锚点：[ev-xxx]（证据）/{{metric:obs-id}}（数值插值）
_EV_REF_RE = re.compile(r"\[(ev-[A-Za-z0-9_-]+)\]")
_METRIC_REF_RE = re.compile(r"\{\{metric:([A-Za-z0-9_-]+)\}\}")
#: 事实性数字扫描（验证用）：排除年份/日期/百分比序号的粗过滤
_BARE_NUMBER_RE = re.compile(r"(?<![\w.:/-])\d[\d,]*\.?\d*(?![\w])")


class ResearchClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str = ""
    entity_kind: Literal["stock", "industry"]
    entity_id: str
    statement: str = Field(min_length=4)
    kind: ClaimKind
    question_id: str | None = None
    support_refs: list[str] = Field(default_factory=list)  # evidence/observation/calculation id
    counter_refs: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    status: ClaimStatus = "draft"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    evidence_cutoff: datetime | None = None
    run_id: str | None = None
    namespace: str = "prod"
    superseded_by: str | None = None
    legacy_field: str | None = None  # 旧 thesis 等兼容迁移来源（legacy analysis 标记）

    @model_validator(mode="after")
    def _check(self) -> ResearchClaim:
        if self.kind == "fact_summary" and not self.support_refs:
            raise ValueError("fact_summary 论断必须绑定支持引用（事实摘要不能无来源）")
        if self.status == "validated" and not self.support_refs:
            raise ValueError("validated 论断必须有支持引用")
        if self.status == "superseded" and not self.superseded_by:
            raise ValueError("superseded 论断必须指向替代者")
        return self

    def with_id(self) -> ResearchClaim:
        if not self.claim_id:
            self.claim_id = f"claim-{uuid.uuid4().hex[:12]}"
        return self


# ---------------- ReportDocument blocks（§7.8 固定类型） ----------------


class HeadingBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["heading"] = "heading"
    level: int = Field(ge=1, le=4)
    text: str


class ParagraphBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["paragraph"] = "paragraph"
    text: str  # 可含 [ev-xxx] 与 {{metric:obs-id}} 锚点


class ClaimBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["claim"] = "claim"
    claim_id: str
    note: str = ""


class MetricCell(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = ""
    value: str | None = None  # 十进制字符串（展示层格式化）
    observation_id: str | None = None  # 有引用时服务端以引用值为准
    unit: str = ""
    note: str = ""


class MetricTableBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["metric_table"] = "metric_table"
    title: str
    columns: list[str]
    rows: list[list[MetricCell]]


class ChartRefBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["chart_ref"] = "chart_ref"
    chart_id: str  # 前端 registry 内的图表组件名（模型不能注入执行逻辑）
    title: str
    metric_refs: list[str] = Field(default_factory=list)  # observation_id 序列
    caption: str = ""


class ComparisonBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["comparison"] = "comparison"
    title: str
    items: list[MetricCell]


class AssumptionTableBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["assumption_table"] = "assumption_table"
    title: str
    assumptions: dict[str, str]
    calculation_ref: str | None = None


class SourceRefBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["source_ref"] = "source_ref"
    refs: list[str] = Field(min_length=1)  # evidence_id / document_id
    note: str = ""


class GapNoticeBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["gap_notice"] = "gap_notice"
    module: str
    message: str


ReportBlock = Annotated[
    (
        HeadingBlock
        | ParagraphBlock
        | ClaimBlock
        | MetricTableBlock
        | ChartRefBlock
        | ComparisonBlock
        | AssumptionTableBlock
        | SourceRefBlock
        | GapNoticeBlock
    ),
    Field(discriminator="type"),
]


class ReportDocument(BaseModel):
    """结构化研究报告：固定 block 类型，服务端验证后渲染 Markdown 与页面。"""

    model_config = ConfigDict(extra="forbid")

    doc_id: str = ""
    title: str
    entity_kind: Literal["stock", "industry"]
    entity_id: str
    blocks: list[ReportBlock] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    plan_id: str | None = None
    limitations: list[str] = Field(default_factory=list)

    def with_id(self) -> ReportDocument:
        if not self.doc_id:
            self.doc_id = f"doc-{uuid.uuid4().hex[:12]}"
        return self


class ValidationIssue(BaseModel):
    code: Literal[
        "unresolved_evidence", "unresolved_claim", "unresolved_observation",
        "unresolved_calculation", "uninterpolated_metric", "bare_number_unverified",
        "empty_document", "unsourced_metric_cell", "unresolved_claim_ref",
        "claim_context_mismatch",
    ]
    block_index: int | None = None
    ref: str = ""
    message: str = ""
    hard: bool = True  # hard=True → 不能 validated（integrity 门禁）


def ref_resolvable(
    kb, store, ref: str, *, namespace: str = "prod",
    entity_kind: str | None = None, entity_id: str | None = None,
) -> bool:
    """引用可解析性（带上下文，review #4/#5）：

    - ev- → 证据已登记（证据库全局，不绑实体）；
    - fact-/obs-/calc-/claim-/artifact-/plan- → 存在且同命名空间；
      给出实体时还要求同实体（跨实体/跨命名空间引用 = 不可信）。
    """
    try:
        if ref.startswith("ev-"):
            kb.get_evidence(ref)
            return True
        if ref.startswith("fact-"):
            row = kb._conn.execute(  # noqa: SLF001 - 只读存在性+上下文检查
                "SELECT namespace, entity_kind, entity_id FROM facts WHERE fact_id = ?", (ref,)
            ).fetchone()
            if row is None or row[0] != namespace:
                return False
            return not (entity_kind and (row[1], row[2]) != (entity_kind, entity_id))
        meta = store.get_ref_meta(ref)
        if meta is None or meta["namespace"] != namespace:
            return False
        return not (
            entity_kind and (meta["entity_kind"], meta["entity_id"]) != (entity_kind, entity_id)
        )
    except Exception:
        return False


class ResearchArtifact(BaseModel):
    """冻结研报：判断与事实分离的发布单元（§6.1）。"""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str = ""
    entity_kind: Literal["stock", "industry"]
    entity_id: str
    title: str
    report_document: ReportDocument
    claim_ids: list[str] = Field(default_factory=list)
    calculation_ids: list[str] = Field(default_factory=list)
    plan_id: str | None = None
    snapshot_refs: list[str] = Field(default_factory=list)
    #: 结构化产物（audit §3.7）：industry_map / candidate_assessment /
    #: comparison_matrix / validation_timeline / executive_summary——
    #: 合成器产候选、服务端验证、projector 确定性投影（不在读页面时调 LLM）
    structures: dict[str, Any] = Field(default_factory=dict)
    status: ClaimStatus = "draft"  # draft/validated/superseded（产物同用三态）
    sufficiency: ArtifactSufficiency = "partial"
    #: 发布用途区分（review #26）：用户情景不是默认发布版，不进档案结论/覆盖投影
    purpose: Literal["report", "scenario"] = "report"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    evidence_cutoff: datetime | None = None
    run_id: str | None = None
    namespace: str = "prod"
    validation_issues: list[ValidationIssue] = Field(default_factory=list)
    markdown: str = ""  # 渲染缓存（发布时物化，与页面同源）

    @model_validator(mode="after")
    def _check(self) -> ResearchArtifact:
        if self.status == "validated" and any(i.hard for i in self.validation_issues):
            raise ValueError("存在未通过的硬校验，产物不能标 validated")
        return self

    def with_id(self) -> ResearchArtifact:
        if not self.artifact_id:
            self.artifact_id = f"artifact-{uuid.uuid4().hex[:12]}"
        return self


# ---------------- 验证与渲染 ----------------


class ArtifactValidator:
    """引用/数值/完整性校验（integrity 硬门禁的执行点，设计 §7.6）。"""

    def __init__(self, *, kb, metric_store) -> None:
        self._kb = kb
        self._store = metric_store

    def validate(self, artifact: ResearchArtifact) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        doc = artifact.report_document
        if not doc.blocks:
            issues.append(ValidationIssue(code="empty_document", message="报告无内容 block"))
        allowed_numbers: set[str] = set()  # 可自由出现在文本里的数值（观测值/期间年份）
        for idx, block in enumerate(doc.blocks):
            if block.type == "paragraph":
                issues.extend(self._check_paragraph(block.text, idx, allowed_numbers))
            elif block.type == "claim":
                claim = self._store.get_claim(block.claim_id)
                if claim is None:
                    issues.append(ValidationIssue(
                        code="unresolved_claim", block_index=idx, ref=block.claim_id,
                        message=f"claim 未登记: {block.claim_id}",
                    ))
                else:
                    issues.extend(self._check_claim_refs(claim, idx))
            elif block.type == "metric_table":
                for row in block.rows:
                    for cell in row:
                        if cell.observation_id:
                            obs = self._store.get_observation(cell.observation_id)
                            if obs is None:
                                issues.append(ValidationIssue(
                                    code="unresolved_observation", block_index=idx,
                                    ref=cell.observation_id,
                                    message=f"observation 未登记: {cell.observation_id}",
                                ))
                            elif obs.value is not None:
                                allowed_numbers.add(_plain(obs.value))
                        elif cell.value is not None and not _is_exempt_number(_plain(cell.value)):
                            # review #6：表内数值必须带观测引用（关键数值经 metric_ref，
                            # 裸值不得进入「通过基础校验」的报告）
                            issues.append(ValidationIssue(
                                code="unsourced_metric_cell", block_index=idx,
                                ref=f"{block.title}:{cell.label or cell.value}",
                                message=(
                                    f"metric_table 单元格值 {cell.value!r} 无 observation_id 引用"
                                    "（表内关键数值必须绑观测；年份/小整数修辞豁免）"
                                ),
                            ))
            elif block.type == "comparison":
                # review #6：comparison 此前无验证分支——裸值/不可解析引用零问题通过
                for item in block.items:
                    if item.observation_id:
                        obs = self._store.get_observation(item.observation_id)
                        if obs is None:
                            issues.append(ValidationIssue(
                                code="unresolved_observation", block_index=idx,
                                ref=item.observation_id,
                                message=f"comparison 引用不可解析: {item.observation_id}",
                            ))
                        elif obs.value is not None:
                            allowed_numbers.add(_plain(obs.value))
                    elif item.value is not None and not _is_exempt_number(_plain(item.value)):
                        issues.append(ValidationIssue(
                            code="unsourced_metric_cell", block_index=idx,
                            ref=f"{block.title}:{item.label or item.value}",
                            message=f"comparison 项值 {item.value!r} 无 observation_id 引用",
                        ))
            elif block.type == "chart_ref":
                for ref in block.metric_refs:
                    if self._store.get_observation(ref) is None:
                        issues.append(ValidationIssue(
                            code="unresolved_observation", block_index=idx, ref=ref,
                            message=f"chart_ref 引用的 observation 未登记: {ref}",
                        ))
            elif block.type == "assumption_table" and block.calculation_ref:
                if self._store.get_calculation(block.calculation_ref) is None:
                    issues.append(ValidationIssue(
                        code="unresolved_calculation", block_index=idx,
                        ref=block.calculation_ref,
                        message=f"calculation 未登记: {block.calculation_ref}",
                    ))
            elif block.type == "source_ref":
                for ref in block.refs:
                    if ref.startswith("ev-"):
                        try:
                            self._kb.get_evidence(ref)
                        except Exception:
                            issues.append(ValidationIssue(
                                code="unresolved_evidence", block_index=idx, ref=ref,
                                message=f"证据未登记: {ref}",
                            ))
                    elif self._store.get_document(ref) is None:
                        issues.append(ValidationIssue(
                            code="unresolved_evidence", block_index=idx, ref=ref,
                            message=f"文档未登记: {ref}",
                        ))
        for claim_id in artifact.claim_ids:
            claim = self._store.get_claim(claim_id)
            if claim is None:
                issues.append(ValidationIssue(
                    code="unresolved_claim", ref=claim_id, message=f"产物引用的 claim 未登记: {claim_id}"
                ))
            else:
                issues.extend(self._check_claim_refs(claim, None))
        for calc_id in artifact.calculation_ids:
            if self._store.get_calculation(calc_id) is None:
                issues.append(ValidationIssue(
                    code="unresolved_calculation", ref=calc_id,
                    message=f"产物引用的 calculation 未登记: {calc_id}",
                ))
        return issues

    def _check_claim_refs(self, claim: dict[str, Any], block_index: int | None) -> list[ValidationIssue]:
        """论断的支持/反方引用逐条验证（review #5）：存在性 + 命名空间 + 实体上下文。

        只查 claim 存在无法阻止错误进入正式结论——validated 论断的两侧引用
        都必须可在同一上下文解析。"""
        issues: list[ValidationIssue] = []
        ns = claim.get("namespace", "prod")
        kind, eid = claim.get("entity_kind"), claim.get("entity_id")
        for ref in [*claim.get("support_refs", []), *claim.get("counter_refs", [])]:
            if not ref_resolvable(self._kb, self._store, ref, namespace=ns,
                                  entity_kind=kind, entity_id=eid):
                side = "支持" if ref in claim.get("support_refs", []) else "反方"
                issues.append(ValidationIssue(
                    code="unresolved_claim_ref", block_index=block_index, ref=ref,
                    message=(
                        f"claim {claim.get('claim_id')} 的{side}引用不可解析或跨上下文: {ref}"
                        f"（namespace={ns}, entity={kind}:{eid}）"
                    ),
                ))
        return issues

    def _check_paragraph(
        self, text: str, idx: int, allowed_numbers: set[str]
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for m in _EV_REF_RE.finditer(text):
            try:
                self._kb.get_evidence(m.group(1))
            except Exception:
                issues.append(ValidationIssue(
                    code="unresolved_evidence", block_index=idx, ref=m.group(1),
                    message=f"段落引用了未登记的证据 {m.group(1)}（未知 id 必须报错，不静默）",
                ))
        for m in _METRIC_REF_RE.finditer(text):
            obs = self._store.get_observation(m.group(1))
            if obs is None or obs.value is None:
                issues.append(ValidationIssue(
                    code="unresolved_observation", block_index=idx, ref=m.group(1),
                    message=f"段落数值插值引用不可解析: {m.group(1)}",
                ))
            else:
                allowed_numbers.add(_plain(obs.value))
        # 自由文本里的事实数字：对照允许值清单（年份/常见小数字豁免）
        stripped = _METRIC_REF_RE.sub(" ", _EV_REF_RE.sub(" ", text))
        for m in _BARE_NUMBER_RE.finditer(stripped):
            raw = m.group(0).replace(",", "")
            if _is_exempt_number(raw):
                continue
            if _plain(raw) not in allowed_numbers:
                issues.append(ValidationIssue(
                    code="bare_number_unverified", block_index=idx, ref=m.group(0),
                    message=(
                        f"段落中的事实数字 {m.group(0)!r} 未绑定观测引用"
                        "（关键数值请用 {{metric:<observation_id>}} 插值）"
                    ),
                    hard=False,  # 软检查：提示改写，不阻断 validated（integrity 之外的质量项）
                ))
        return issues


def _plain(value: str) -> str:
    """数值字符串规范化（去逗号/尾零），用于允许清单比对。"""
    from decimal import Decimal, InvalidOperation

    try:
        d = Decimal(value.replace(",", ""))
    except InvalidOperation:
        return value
    if d == d.to_integral_value():
        return str(int(d))
    return str(d.normalize())


def _is_exempt_number(raw: str) -> bool:
    """年份（1900-2100 整数）与 0-100 的小整数（常见修辞/序号）豁免事实数字检查。"""
    from decimal import Decimal, InvalidOperation

    try:
        d = Decimal(raw)
    except InvalidOperation:
        return True
    if d == d.to_integral_value():
        n = int(d)
        if 1900 <= n <= 2100:
            return True  # 年份
        if 0 <= n <= 100:
            return True  # 小整数修辞（三个变化/八大风险…）
    return False


def document_from_markdown(
    markdown: str,
    *,
    title: str,
    entity_kind: str,
    entity_id: str,
    plan_id: str | None = None,
) -> ReportDocument:
    """兼容入口：自由 Markdown → ReportDocument（heading/paragraph block）。

    LLM 未走 submit_report_document 时的降级路径：[ev-xxx] 锚点保留在段落里
    由验证层检查；产物标 draft（未经结构化验证不得 validated）。
    """
    blocks: list[Any] = []
    buf: list[str] = []

    def flush() -> None:
        text = "\n".join(buf).strip()
        if text:
            blocks.append(ParagraphBlock(text=text))
        buf.clear()

    for line in (markdown or "").splitlines():
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            flush()
            blocks.append(HeadingBlock(level=min(len(m.group(1)), 4), text=m.group(2).strip()))
        else:
            buf.append(line)
    flush()
    doc = ReportDocument(
        title=title, entity_kind=entity_kind, entity_id=entity_id,  # type: ignore[arg-type]
        blocks=blocks, plan_id=plan_id,
        limitations=["由自由 Markdown 降级解析（未经结构化提交）"],
    )
    return doc.with_id()


def interpolate_metrics(text: str, store) -> str:
    """{{metric:obs-id}} → 观测值（带单位）；不可解析保留原样（验证层已报错）。"""

    def repl(m: re.Match[str]) -> str:
        obs = store.get_observation(m.group(1))
        if obs is None or obs.value is None:
            return m.group(0)
        unit = f" {obs.unit}" if obs.unit else ""
        return f"{obs.value}{unit}"

    return _METRIC_REF_RE.sub(repl, text)


def render_markdown(artifact: ResearchArtifact, *, store, kb) -> str:
    """ReportDocument → Markdown（report.md、档案概览、Sessions 摘要同源，§7.8）。"""
    doc = artifact.report_document
    lines: list[str] = [f"# {doc.title}", ""]
    meta = [
        f"- 产物: {artifact.artifact_id}",
        f"- 状态: {artifact.status}（充分度 {artifact.sufficiency}）",
        f"- 生成: {artifact.created_at.isoformat(timespec='seconds')}",
    ]
    if artifact.evidence_cutoff:
        meta.append(f"- 证据截止: {artifact.evidence_cutoff.isoformat(timespec='seconds')}")
    if artifact.plan_id:
        meta.append(f"- 研究计划: {artifact.plan_id}")
    lines.extend(meta)
    lines.append("")
    for block in doc.blocks:
        if block.type == "heading":
            lines.append(f"{'#' * (block.level + 1)} {block.text}")
            lines.append("")
        elif block.type == "paragraph":
            lines.append(interpolate_metrics(block.text, store))
            lines.append("")
        elif block.type == "claim":
            claim = store.get_claim(block.claim_id)
            if claim is None:
                lines.append(f"> ⚠ 不可解析的 claim 引用: {block.claim_id}")
            else:
                kind_note = {"fact_summary": "事实摘要", "inference": "推论",
                             "hypothesis": "待验证假设", "analysis": "分析"}[claim["kind"]]
                lines.append(f"> **[{kind_note}]** {claim['statement']}")
                if claim.get("limitations"):
                    lines.append(f"> 限制: {'；'.join(claim['limitations'])}")
                if block.note:
                    lines.append(f"> {block.note}")
            lines.append("")
        elif block.type == "metric_table":
            lines.append(f"**{block.title}**")
            lines.append("")
            lines.append("| " + " | ".join(block.columns) + " |")
            lines.append("|" + "---|" * len(block.columns))
            for row in block.rows:
                cells = []
                for cell in row:
                    value = cell.value
                    if cell.observation_id:
                        obs = store.get_observation(cell.observation_id)
                        if obs is not None and obs.value is not None:
                            value = obs.value
                    unit = f" {cell.unit}" if cell.unit else ""
                    cells.append(f"{value or '—'}{unit}" + (f"（{cell.note}）" if cell.note else ""))
                lines.append("| " + " | ".join(cells) + " |")
            lines.append("")
        elif block.type == "chart_ref":
            refs = ", ".join(block.metric_refs) or "—"
            lines.append(f"📈 **{block.title}**（图表 {block.chart_id}；数据引用 {refs}）")
            if block.caption:
                lines.append(f"_{block.caption}_")
            lines.append("")
        elif block.type == "comparison":
            lines.append(f"**{block.title}**")
            for item in block.items:
                unit = f" {item.unit}" if item.unit else ""
                lines.append(f"- {item.label}: {item.value or '—'}{unit}")
            lines.append("")
        elif block.type == "assumption_table":
            lines.append(f"**{block.title}**")
            for k, v in block.assumptions.items():
                lines.append(f"- {k}: {v}")
            if block.calculation_ref:
                lines.append(f"- 计算引用: {block.calculation_ref}")
            lines.append("")
        elif block.type == "source_ref":
            for ref in block.refs:
                if ref.startswith("ev-"):
                    try:
                        ev = kb.get_evidence(ref)
                        lines.append(f"- [{ref}] {ev.verbatim_quote[:80]}…（{ev.url or ev.source_id}）")
                    except Exception:
                        lines.append(f"- [{ref}] ⚠ 不可解析")
                else:
                    docm = store.get_document(ref)
                    lines.append(f"- [{ref}] {docm.title if docm else '⚠ 不可解析'}")
            if block.note:
                lines.append(f"_{block.note}_")
            lines.append("")
        elif block.type == "gap_notice":
            lines.append(f"> ⚠ **缺口（{block.module}）**: {block.message}")
            lines.append("")
    if doc.limitations:
        lines.append("## 限制")
        lines.extend(f"- {x}" for x in doc.limitations)
        lines.append("")
    if artifact.validation_issues:
        lines.append("## 校验备注")
        lines.extend(
            f"- [{'HARD' if i.hard else 'soft'}] {i.code}: {i.message}"
            for i in artifact.validation_issues
        )
        lines.append("")
    return "\n".join(lines)

// Thesis 对象卡（优化方案 §13-§15）：每个论点必须同时回答
// 「什么支持它 / 什么反驳它 / 什么未决 / 什么证伪」，并按
// Fact → Observation → Inference → Thesis 分层标注——事实、分析、预测在视觉上不得一致。
//
// 数据源两级契约：
// - 服务端 investment_objects.theses（§13 ThesisObject：确定性推导的计数与
//   状态映射置信度、monitor 监测链接、反证义务状态）存在时优先消费；
// - 旧快照无 theses → 回退 ClaimItem 渲染（行为与改版前一致）。

import { CitationChips } from "./citations";
import { splitTextRefs } from "./overview";
import type { ClaimItem, ThesisObject } from "./types";

/** 事实分层（§15）：Fact=事实摘要 / Observation=观测 / Inference=推论 / Hypothesis=待验证。 */
const KIND_META: Record<string, { label: string; cls: string }> = {
  fact_summary: { label: "事实", cls: "border-line bg-paper text-ink-soft" },
  inference: { label: "推论", cls: "border-accent/40 bg-accent-soft text-accent" },
  hypothesis: { label: "待验证假设", cls: "border-warn/40 bg-warn-soft text-warn" },
  analysis: { label: "分析", cls: "border-line bg-paper text-ink-soft" },
};

/** 支撑/反证比例条（确定性计数，不造置信度分数）。 */
export function EvidenceBalance({ support, counter }: { support: number; counter: number }) {
  const total = support + counter;
  if (!total) return null;
  const pct = Math.round((support / total) * 100);
  return (
    <span className="inline-flex items-center gap-2" title={`支持 ${support} · 反证 ${counter}`}>
      <span className="flex h-1.5 w-20 overflow-hidden rounded-full bg-risk/25">
        <span className="block h-full bg-pos" style={{ width: `${pct}%` }} />
      </span>
      <span className="dos-num text-meta text-ink-mute">
        支持 {support} · 反证 {counter}
      </span>
    </span>
  );
}

/** 置信度圆点（§14）：值是服务端的 claim 状态映射（非统计置信度）——
 *  圆点只做视觉分级，tooltip 必须展示推导依据（confidence_basis）。 */
export function ConfidenceDots({ confidence, basis }: {
  confidence: number | null; basis?: string;
}) {
  if (confidence === null || confidence === undefined) return null;
  const filled = Math.max(0, Math.min(5, Math.round(confidence * 5)));
  return (
    <span className="inline-flex items-center gap-1.5"
          title={`置信度（claim 状态映射，非统计口径）\n${basis || ""}`}>
      <span className="text-meta text-ink-mute">置信度</span>
      <span aria-label={`置信度 ${filled}/5`} className="tracking-[0.08em]">
        {[0, 1, 2, 3, 4].map((i) => (
          <span key={i} className={i < filled ? "text-accent" : "text-line"}>●</span>
        ))}
      </span>
    </span>
  );
}

// ---------------- ThesisObject 渲染（§13/§14，服务端推导对象优先） ----------------

export function ThesisObjectCard({ thesis, citeIndex, onCite }: {
  thesis: ThesisObject;
  citeIndex?: Map<string, number>;
  onCite: (id: string) => void;
}) {
  const kind = KIND_META[thesis.kind] ?? KIND_META.analysis;
  return (
    <article className="rounded-card border border-line bg-white p-4">
      <div className="mb-1.5 flex flex-wrap items-center gap-2">
        <span className={`rounded-full border px-2 py-0.5 text-[11px] font-medium ${kind.cls}`}>
          {kind.label}
        </span>
        <ConfidenceDots confidence={thesis.confidence} basis={thesis.confidence_basis} />
        <EvidenceBalance support={thesis.support_count} counter={thesis.counter_count} />
        {thesis.unresolved_count > 0 && (
          <span className="text-meta text-ink-faint">未决 {thesis.unresolved_count}</span>
        )}
        {thesis.status === "draft" && <span className="text-meta text-warn">草稿</span>}
        {/* §14 反证义务：无反证引用 = 义务未履行（显性标注，不假装无反证） */}
        {thesis.bear_case_status === "unmet" && (
          <span className="rounded bg-warn-soft px-1.5 py-0.5 text-meta text-warn"
                title="该论点尚无绑定的反方证据——反方检索义务未履行，结论应按未对冲看待">
            反证义务未履行
          </span>
        )}
      </div>
      {thesis.title && thesis.title !== thesis.summary && (
        <div className="mb-1 text-[13px] font-semibold text-ink">{thesis.title}</div>
      )}
      <p className="max-w-[76ch] text-sm leading-[1.8] text-ink">
        {/* 内嵌 raw ID 剥离子（§16）；剥离出的 ev-x 并入引用 chips */}
        {splitTextRefs(thesis.summary).clean}
        <CitationChips refs={[...splitTextRefs(thesis.summary).refs, ...thesis.supports]}
                       index={citeIndex} onCite={onCite} />
      </p>
      {(thesis.contradicts.length > 0 || thesis.monitor.length > 0
        || thesis.related_companies.length > 0) && (
        <div className="mt-2 space-y-1 border-t border-line/70 pt-2">
          {thesis.contradicts.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5 text-meta text-ink-mute">
              <span className="text-risk">反证：</span>
              <CitationChips refs={thesis.contradicts} index={citeIndex} onCite={onCite} />
            </div>
          )}
          {thesis.monitor.length > 0 && (
            <div className="text-meta text-ink-mute">
              监测：{thesis.monitor.join("；")}
            </div>
          )}
          {thesis.related_companies.length > 0 && (
            <div className="text-meta text-ink-faint">
              相关公司：{thesis.related_companies.join("、")}
            </div>
          )}
        </div>
      )}
    </article>
  );
}

// ---------------- 旧 ClaimItem 渲染（回退路径，与改版前一致） ----------------

export function ThesisCard({ claim, citeIndex, onCite }: {
  claim: ClaimItem;
  citeIndex?: Map<string, number>;
  onCite: (id: string) => void;
}) {
  const kind = KIND_META[claim.kind] ?? KIND_META.analysis;
  const support = claim.support_refs.filter((r) => r.startsWith("ev-")).length;
  const counter = claim.counter_refs.length;
  const unresolved = claim.limitations.length;
  const superseded = claim.status === "superseded";
  return (
    <article className={`rounded-card border bg-white p-4 ${
      superseded ? "border-line opacity-55" : "border-line"
    }`}>
      <div className="mb-1.5 flex flex-wrap items-center gap-2">
        <span className={`rounded-full border px-2 py-0.5 text-[11px] font-medium ${kind.cls}`}>
          {kind.label}
        </span>
        <EvidenceBalance support={support} counter={counter} />
        {unresolved > 0 && (
          <span className="text-meta text-ink-faint">未决 {unresolved}</span>
        )}
        {claim.status === "validated" && (
          <span className="text-meta text-pos">引用已校验</span>
        )}
        {claim.status === "draft" && (
          <span className="text-meta text-warn">草稿</span>
        )}
        {superseded && <span className="text-meta text-ink-faint">已被替代</span>}
      </div>
      <p className="max-w-[76ch] text-sm leading-[1.8] text-ink">
        {splitTextRefs(claim.statement).clean}
        <CitationChips refs={[...splitTextRefs(claim.statement).refs, ...claim.support_refs]}
                       index={citeIndex} onCite={onCite} />
      </p>
      {(counter > 0 || unresolved > 0) && (
        <div className="mt-2 space-y-1 border-t border-line/70 pt-2">
          {claim.counter_refs.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5 text-meta text-ink-mute">
              <span className="text-risk">反证：</span>
              <CitationChips refs={claim.counter_refs} index={citeIndex} onCite={onCite} />
            </div>
          )}
          {claim.limitations.slice(0, 2).map((l, i) => (
            <div key={i} className="text-meta text-ink-faint">未决：{l}</div>
          ))}
        </div>
      )}
    </article>
  );
}

/** 论点列表（投资快照下半区）：服务端 ThesisObject 优先（§13），
 *  旧快照回退 ClaimItem 渲染（superseded 折叠保留审计轨迹）。 */
export function ThesisList({ claims, theses, citeIndex, onCite }: {
  claims: ClaimItem[];
  theses?: ThesisObject[];
  citeIndex?: Map<string, number>;
  onCite: (id: string) => void;
}) {
  if (theses?.length) {
    return (
      <div className="space-y-3">
        {theses.map((t) => (
          <ThesisObjectCard key={t.id} thesis={t} citeIndex={citeIndex} onCite={onCite} />
        ))}
      </div>
    );
  }
  const active = claims.filter((c) => c.status !== "superseded");
  const dead = claims.filter((c) => c.status === "superseded");
  if (!claims.length) {
    return <div className="text-sm text-ink-faint">（尚无论点——补研后出现）</div>;
  }
  return (
    <div className="space-y-3">
      {active.map((c) => (
        <ThesisCard key={c.claim_id} claim={c} citeIndex={citeIndex} onCite={onCite} />
      ))}
      {dead.length > 0 && (
        <details className="rounded-card border border-line bg-paper/60 p-3">
          <summary className="cursor-pointer text-meta text-ink-mute">
            已被替代的论点（{dead.length}）——保留审计轨迹
          </summary>
          <div className="mt-2 space-y-2">
            {dead.map((c) => (
              <ThesisCard key={c.claim_id} claim={c} citeIndex={citeIndex} onCite={onCite} />
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

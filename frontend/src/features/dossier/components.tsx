// Dossier 共享组件（dossier 优化方案重构版）：
// - 投资者视图组件迁至 overview.tsx（撕页）/ thesis.tsx（论点卡）/ citations.tsx（引用编号）；
// - 本文件保留：模块状态徽标（审计）、长文折叠、缺口补研、审计汇总面板、
//   来源抽屉（§16/§17：raw ID 只在抽屉底部）、补研面板、导出菜单、数值格式化。
//
// 视觉纪律（§38-§43）：90% 中性色；颜色只表达语义；1px border，无重阴影；
// 中文正文 ≥14px；10px 不用于核心信息；金融数字 tabular-nums。

import { useEffect, useRef, useState } from "react";

import { dossierApi, isOfflineExport } from "./api";
import { formatRatioDisplay } from "./charts";
import type { DossierSnapshot, EvidenceDetail, ModuleStatus } from "./types";

// ---------------- 模块状态徽标（审计视图；投资者视图导航用状态点代替） ----------------

const STATUS_META: Record<ModuleStatus, { label: string; cls: string; title: string }> = {
  ready: { label: "就绪", cls: "border-pos/40 bg-pos-soft text-pos", title: "typed 数据齐备，通过基础校验" },
  partial: { label: "部分", cls: "border-warn/40 bg-warn-soft text-warn", title: "部分数据可用，缺口已标注" },
  missing: { label: "缺失", cls: "border-line bg-paper text-ink-mute", title: "as_of 时点无该模块数据" },
  stale: { label: "陈旧", cls: "border-warn/40 bg-warn-soft text-warn", title: "超过配方新鲜度目标" },
  conflicted: { label: "冲突", cls: "border-risk/40 bg-risk-soft text-risk", title: "同语义键竞争值未裁决" },
  not_applicable: { label: "不适用", cls: "border-line bg-paper text-ink-mute", title: "行业配方判定不适用（不硬套模型）" },
  unavailable_at_as_of: { label: "当时不可知", cls: "border-accent/40 bg-accent-soft text-accent", title: "历史视图：该数据在当时尚不可知" },
};

export function ModuleStateBadge({ status, reasons }: { status: ModuleStatus; reasons?: string[] }) {
  const meta = STATUS_META[status] ?? STATUS_META.missing;
  return (
    <span
      className={`inline-block rounded-full border px-2 py-0.5 text-meta ${meta.cls}`}
      title={[meta.title, ...(reasons ?? [])].join("\n")}
    >
      {meta.label}
    </span>
  );
}

/** 长文本默认折叠：不截断数据——只控制默认展示高度，全文一键展开。 */
export function LongText({ text, maxPx = 88, className = "" }: {
  text: string; maxPx?: number; className?: string;
}) {
  const [open, setOpen] = useState(false);
  const long = text.length > 160 || text.split("\n").length > 3;
  if (!long) return <span className={className}>{text}</span>;
  return (
    <span className={`block ${className}`} data-longtext={open ? "open" : "clamped"}>
      <span
        className="block whitespace-pre-wrap"
        style={open ? undefined : { maxHeight: `${maxPx}px`, overflow: "hidden" }}
      >
        {text}
      </span>
      <button onClick={() => setOpen((v) => !v)}
              className="mt-0.5 whitespace-nowrap text-meta text-accent hover:underline">
        {open ? "收起" : `展开（${text.length} 字）`}
      </button>
    </span>
  );
}

export function ModuleReasons({ reasons, gapRefs }: {
  reasons: string[];
  /** 未完成的问题 id：点击缺口直接补研相应 question_id */
  gapRefs?: string[];
}) {
  if (!reasons.length && !gapRefs?.length) return null;
  return (
    <div className="mt-1">
      {reasons.length > 0 && (
        <ul className="space-y-0.5 text-meta text-ink-mute">
          {reasons.map((r, i) => <li key={i}>· {r}</li>)}
        </ul>
      )}
      {(gapRefs?.length ?? 0) > 0 && (
        <div className="mt-1.5 flex flex-wrap items-center gap-1">
          <span className="text-meta text-ink-faint">未完成问题：</span>
          {gapRefs!.map((qid) => (
            <button
              key={qid}
              onClick={() => requestResearch(qid)}
              title={`以 targeted 深度补研该问题（focus=${qid}）`}
              className="rounded-full border border-line bg-white px-1.5 py-0.5 font-mono text-[11px] text-ink-soft hover:border-accent hover:bg-accent-soft hover:text-accent"
            >
              {qid} ↻补研
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** 缺口 → 补研：页面级事件，ResearchPanel 监听并预填 focus。 */
export function requestResearch(questionId: string, objective?: string) {
  window.dispatchEvent(new CustomEvent("dossier:research", {
    detail: { focus: questionId, objective: objective ?? "" },
  }));
}

// ---------------- 审计汇总面板（§10/§18：仅 Audit Mode 渲染） ----------------
// 研究目标 / 问题进展 / 可信度分层 / 限制清单——这些信息是「系统可信吗」的答案，
// 不是「该买什么」的答案，因此默认不进投资者首屏。

const CREDIBILITY_LABELS: Record<string, string> = {
  refs_resolvable: "引用可解析",
  facts_checked: "事实已核对",
  analysis_reviewed: "分析已复核",
  sufficiency: "研究充分度",
  level: "充分度等级",
  note: "说明",
};

export function AuditSummaryPanel({ snap }: { snap: DossierSnapshot }) {
  const s = snap.summary;
  const credibility = Object.entries(s.credibility ?? {});
  const limitations = s.limitations ?? [];
  const [showAll, setShowAll] = useState(false);
  const progress = s.question_progress
    || (snap.research.required
      ? `关键问题 ${snap.research.answered}/${snap.research.required} 已回答`
      : "");
  return (
    <section className="dos-card space-y-3 border-line">
      <div className="border-b border-line pb-1.5">
        <span className="dos-kicker">Audit · 研究过程与可信度（审计视图）</span>
      </div>
      {s.objective && (
        <div className="text-sm text-ink-soft">
          <span className="font-semibold text-ink">研究目标：</span>
          <LongText text={s.objective} maxPx={64} />
        </div>
      )}
      {(progress || snap.research.verdict) && (
        <div className="flex flex-wrap gap-2 text-meta">
          {progress && (
            <span className="rounded-full border border-line px-2 py-0.5 text-ink-soft">{progress}</span>
          )}
          {snap.research.verdict && (
            <span className="rounded-full border border-line px-2 py-0.5 text-ink-mute">
              研究充分度 {snap.research.verdict}
            </span>
          )}
        </div>
      )}
      {credibility.length > 0 && (
        <div>
          <div className="mb-1 text-meta font-semibold text-ink-mute">可信度分层（不笼统称「已验证」）</div>
          <ul className="grid gap-1 text-meta text-ink-soft md:grid-cols-2">
            {credibility.map(([k, v]) => (
              <li key={k}><span className="text-ink-faint">{CREDIBILITY_LABELS[k] ?? k}：</span>{v}</li>
            ))}
          </ul>
        </div>
      )}
      {limitations.length > 0 && (
        <div>
          <div className="mb-1 flex items-center gap-2 text-meta font-semibold text-ink-mute">
            限制与未核验部分
            {limitations.length > 4 && (
              <button onClick={() => setShowAll((v) => !v)} className="font-normal text-accent hover:underline">
                {showAll ? "收起" : `展开全部 ${limitations.length} 条`}
              </button>
            )}
          </div>
          <ul className="space-y-0.5 text-meta text-ink-soft">
            {(showAll ? limitations : limitations.slice(0, 4)).map((l, i) => <li key={i}>· {l}</li>)}
          </ul>
        </div>
      )}
    </section>
  );
}

// ---------------- 数值格式化（KPI/图表共用） ----------------

export function MetricValue({ value, unit, currency, rawText }: {
  value: string | null; unit: string; currency?: string | null; rawText?: string;
}) {
  if (value === null || value === undefined) return <span className="text-line">—</span>;
  const n = Number(value);
  let text = value;
  if (Number.isFinite(n)) {
    const abs = Math.abs(n);
    if (unit === "ratio") {
      // 与图表 tooltip 同一实现（formatRatioDisplay）：原文锚点优先，
      // 无锚点 |v|≤1 按分数、否则按百分点
      text = formatRatioDisplay(n, rawText);
    } else if (abs >= 1e9) text = `${(n / 1e9).toFixed(2)}B`;
    else if (abs >= 1e6) text = `${(n / 1e6).toFixed(1)}M`;
    else text = n.toLocaleString("en-US", { maximumFractionDigits: 2 });
  }
  return <>{text}{currency && unit !== "ratio" ? <span className="ml-0.5 text-meta font-normal text-ink-faint">{currency}</span> : null}</>;
}

// ---------------- 来源抽屉（§16/§17：Evidence Everywhere, Visible on Demand） ----------------
// 正文只有 [n] 编号；抽屉给原文/出处/时间/PIT 等级；raw evidence ID 只在底部元数据。

export function SourceDrawer({ snapshotId, evidenceId, onClose }: {
  snapshotId: string;
  evidenceId: string | null;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<EvidenceDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!evidenceId) { setDetail(null); setError(null); return; }
    let cancelled = false;
    setDetail(null); setError(null);
    dossierApi.evidence(snapshotId, evidenceId)
      .then((d) => { if (!cancelled) setDetail(d); })
      .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
  }, [snapshotId, evidenceId]);

  useEffect(() => {
    if (!evidenceId) return;
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [evidenceId, onClose]);

  if (!evidenceId) return null;
  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-ink/25" onClick={onClose} role="dialog"
         aria-modal="true" aria-label="来源详情">
      <div
        className="h-full w-full max-w-md overflow-y-auto border-l border-line bg-white p-5 shadow-xl sm:w-[30rem]"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between">
          <h3 className="text-[15px] font-semibold text-ink">来源与原文</h3>
          <button ref={closeRef} onClick={onClose}
                  className="rounded border border-line px-2 py-0.5 text-meta hover:border-ink-faint"
                  aria-label="关闭来源抽屉（Esc）">
            关闭 ✕
          </button>
        </div>
        {error && (
          <div className="rounded border border-risk/40 bg-risk-soft p-2 text-meta text-risk">
            来源不可读：{error}（该快照未引用此证据或证据缺失——不生成虚假精度）
          </div>
        )}
        {detail && (
          <div className="space-y-4 text-sm">
            <div>
              <div className="mb-1 text-meta font-semibold text-ink-mute">原文摘录（逐字）</div>
              <blockquote className="rounded-card border-l-2 border-ink/50 bg-paper p-3 text-[13px] leading-[1.8] text-ink">
                「{detail.verbatim_quote}」
              </blockquote>
            </div>
            <dl className="grid grid-cols-[5.5rem_1fr] gap-y-2 text-[13px]">
              <dt className="text-ink-mute">出处</dt>
              <dd>
                {detail.url
                  ? <a href={detail.url} target="_blank" rel="noreferrer" className="break-all text-accent hover:underline">{detail.url}</a>
                  : "（无 URL）"}
              </dd>
              <dt className="text-ink-mute">采集方</dt>
              <dd className="font-mono text-meta">{detail.provider_id}</dd>
              <dt className="text-ink-mute">公开可知</dt>
              <dd className="dos-num text-meta">{detail.available_at ?? "未知（C 级无 PIT 保证）"}</dd>
              <dt className="text-ink-mute">系统获取</dt>
              <dd className="dos-num text-meta">{detail.retrieved_at}</dd>
              <dt className="text-ink-mute">PIT 等级</dt>
              <dd>
                <span className={`rounded-full border px-1.5 py-0.5 font-mono text-[11px] ${
                  detail.pit_grade === "A" ? "border-pos/40 bg-pos-soft text-pos"
                  : detail.pit_grade === "B" ? "border-warn/40 bg-warn-soft text-warn"
                  : "border-risk/40 bg-risk-soft text-risk"
                }`}>{detail.pit_grade}</span>
                <span className="ml-2 text-meta text-ink-faint">
                  A=精确时间戳 B=可编辑发布时间 C=无 PIT 保证
                </span>
              </dd>
              {detail.document && (
                <>
                  <dt className="text-ink-mute">定位</dt>
                  <dd className="font-mono text-meta">
                    {Object.entries((detail.document as any).locator ?? {}).map(([k, v]) => `${k}:${v}`).join(" · ") || "（页码/章节未知）"}
                  </dd>
                </>
              )}
            </dl>
            {/* raw ID 仅抽屉底部（§17：审计信息不进入正文流） */}
            <div className="border-t border-line pt-3 font-mono text-[11px] text-ink-faint">
              Evidence ID：{detail.evidence_id}
            </div>
          </div>
        )}
        {!detail && !error && <div className="text-meta text-ink-faint">读取中…</div>}
      </div>
    </div>
  );
}

// ---------------- 补研面板（问题驱动补研入口） ----------------

export function ResearchPanel({ snap, onStarted }: {
  snap: DossierSnapshot;
  onStarted: (sessionId: string, commandId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [depth, setDepth] = useState("standard");
  const [objective, setObjective] = useState("");
  const [focus, setFocus] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [idem, setIdem] = useState(() => `ui-${snap.entity.id}-${Date.now()}`);

  // 缺口点击补研：任意模块发出 dossier:research → 打开面板并预填 focus
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent<{ focus?: string; objective?: string }>).detail ?? {};
      if (detail.focus) setFocus(detail.focus);
      if (detail.objective) setObjective(detail.objective);
      setDepth("targeted");
      setIdem(`ui-${snap.entity.id}-${detail.focus ?? ""}-${Date.now()}`);
      setError(null);
      setOpen(true);
    };
    window.addEventListener("dossier:research", handler);
    return () => window.removeEventListener("dossier:research", handler);
  }, [snap.entity.id]);

  const submit = async () => {
    setBusy(true); setError(null);
    try {
      const r = await dossierApi.requestResearch({
        entity_kind: snap.entity.kind,
        entity_id: snap.entity.id,
        objective: objective || undefined,
        depth,
        focus: focus || undefined,
        base_snapshot: snap.context.snapshot_id,
        idempotency_key: idem,  // 重复点击返回同一 command
      });
      onStarted(r.session_run_id, r.command_id);
      setOpen(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <button onClick={() => setOpen(true)}
              className="rounded-md bg-ink px-3 py-1.5 text-meta font-semibold text-white hover:bg-ink-soft">
        补研
      </button>
    );
  }
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/30 p-4" onClick={() => !busy && setOpen(false)}>
      <div className="w-full max-w-md rounded-card border border-line bg-white p-5 shadow-xl" onClick={(e) => e.stopPropagation()}>
        <h3 className="mb-3 text-[15px] font-semibold text-ink">
          发起研究 · {snap.entity.kind}:{snap.entity.id}
          {snap.context.mode === "historical" && (
            <span className="ml-2 rounded bg-accent-soft px-1.5 py-0.5 text-meta font-normal text-accent">
              以此为基线，研究当前情况（不回写过去）
            </span>
          )}
        </h3>
        <div className="space-y-2.5 text-sm">
          <label className="block">
            <span className="mb-0.5 block text-meta text-ink-mute">深度</span>
            <select value={depth} onChange={(e) => setDepth(e.target.value)}
                    className="w-full rounded border border-line px-2 py-1.5 text-sm">
              <option value="standard">standard（6-10 个关键问题）</option>
              <option value="deep">deep（12-18 个问题 + 反方审查）</option>
              <option value="refresh">refresh（新披露后的增量更新）</option>
              <option value="targeted">targeted（指定问题/模块缺口）</option>
            </select>
          </label>
          <label className="block">
            <span className="mb-0.5 block text-meta text-ink-mute">研究目标（自然语言）</span>
            <input value={objective} onChange={(e) => setObjective(e.target.value)}
                   placeholder="如：评估数据中心订单转化为收入的确定性"
                   className="w-full rounded border border-line px-2 py-1.5 text-sm" />
          </label>
          {(depth === "targeted" || depth === "refresh") && (
            <label className="block">
              <span className="mb-0.5 block text-meta text-ink-mute">聚焦问题/模块</span>
              <input value={focus} onChange={(e) => setFocus(e.target.value)}
                     placeholder="如：最近两季 backlog 缺失"
                     className="w-full rounded border border-line px-2 py-1.5 text-sm" />
            </label>
          )}
          {error && <div className="rounded border border-risk/40 bg-risk-soft p-2 text-meta text-risk">{error}</div>}
          <div className="flex justify-end gap-2 pt-1">
            <button onClick={() => setOpen(false)} disabled={busy}
                    className="rounded border border-line px-3 py-1.5 text-sm hover:border-ink-faint">取消</button>
            <button onClick={submit} disabled={busy}
                    className="rounded bg-ink px-3 py-1.5 text-sm font-semibold text-white hover:bg-ink-soft disabled:opacity-50">
              {busy ? "发起中…" : "进入 Sessions 执行"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------- 导出（JSON/Markdown/HTML 冻结导出，与页面同源） ----------------

export function ExportMenu({ snap }: { snap: DossierSnapshot }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  const doExport = async (format: "json" | "markdown" | "html") => {
    setBusy(format); setError(null);
    try {
      const job = await dossierApi.exportSnapshot(snap.context.snapshot_id, format);
      const a = document.createElement("a");
      a.href = dossierApi.jobArtifactUrl(job.job_id);
      a.download = job.artifact_name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setOpen(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="relative">
      <button onClick={() => setOpen((v) => !v)} disabled={busy !== null}
              className="rounded-md border border-line bg-white px-3 py-1.5 text-meta text-ink-soft hover:border-ink-faint disabled:opacity-50"
              title="导出冻结快照：自包含交互 HTML / Markdown / JSON（与页面同源）">
        {busy ? "导出中…" : "导出 ▾"}
      </button>
      {open && (
        <div className="absolute right-0 top-full z-10 mt-1 w-56 rounded-card border border-line bg-white p-1.5 shadow-lg">
          <button onClick={() => doExport("html")}
                  className="block w-full rounded px-2.5 py-1.5 text-left text-meta text-ink-soft hover:bg-paper">
            <b className="text-ink">HTML</b> · 自包含交互文件，双击离线打开
          </button>
          <button onClick={() => doExport("markdown")}
                  className="block w-full rounded px-2.5 py-1.5 text-left text-meta text-ink-soft hover:bg-paper">
            <b className="text-ink">Markdown</b> · 绑定 data_hash 与截止时间
          </button>
          <button onClick={() => doExport("json")}
                  className="block w-full rounded px-2.5 py-1.5 text-left text-meta text-ink-soft hover:bg-paper">
            <b className="text-ink">JSON</b> · 冻结 Dossier 读模型契约
          </button>
        </div>
      )}
      {error && <div className="absolute right-0 top-full z-10 mt-1 w-64 rounded border border-risk/40 bg-risk-soft p-2 text-meta text-risk">{error}</div>}
    </div>
  );
}

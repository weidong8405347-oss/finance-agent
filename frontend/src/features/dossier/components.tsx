// Dossier 共享组件（设计 §4.3/§4.5/§4.6）：
// Header（时间锁/补研/导出）、ThesisPanel（结论-变化-驱动-反证固定位置）、
// ModuleStateBadge（三轴状态不合并成一个「完成」）、SourceDrawer（证据一次点击，
// 键盘可达）、ClaimCard（判断/事实分离标注）。

import { useEffect, useRef, useState } from "react";

import { dossierApi } from "./api";
import type {
  ClaimItem, DossierSnapshot, EvidenceDetail, KeyMetric, ModuleStatus,
} from "./types";

// ---------------- 模块状态徽标（§4.6：运行/模块/产物三轴分离） ----------------

const STATUS_META: Record<ModuleStatus, { label: string; cls: string; title: string }> = {
  ready: { label: "就绪", cls: "border-green-300 bg-green-50 text-green-700", title: "typed 数据齐备，通过基础校验" },
  partial: { label: "部分", cls: "border-amber-300 bg-amber-50 text-amber-700", title: "部分数据可用，缺口已标注" },
  missing: { label: "缺失", cls: "border-neutral-300 bg-neutral-50 text-neutral-500", title: "as_of 时点无该模块数据" },
  stale: { label: "陈旧", cls: "border-orange-300 bg-orange-50 text-orange-700", title: "超过配方新鲜度目标" },
  conflicted: { label: "冲突", cls: "border-red-300 bg-red-50 text-red-700", title: "同语义键竞争值未裁决" },
  not_applicable: { label: "不适用", cls: "border-neutral-300 bg-neutral-100 text-neutral-500", title: "行业配方判定不适用（不硬套模型）" },
  unavailable_at_as_of: { label: "当时不可知", cls: "border-indigo-300 bg-indigo-50 text-indigo-700", title: "历史视图：该数据在当时尚不可知" },
};

export function ModuleStateBadge({ status, reasons }: { status: ModuleStatus; reasons?: string[] }) {
  const meta = STATUS_META[status] ?? STATUS_META.missing;
  return (
    <span
      className={`inline-block rounded-full border px-2 py-0.5 text-[11px] ${meta.cls}`}
      title={[meta.title, ...(reasons ?? [])].join("\n")}
    >
      {meta.label}
    </span>
  );
}

export function ModuleReasons({ reasons }: { reasons: string[] }) {
  if (!reasons.length) return null;
  return (
    <ul className="mt-1 space-y-0.5 text-[11px] text-neutral-500">
      {reasons.map((r, i) => <li key={i}>· {r}</li>)}
    </ul>
  );
}

// ---------------- 结论面板（首屏 10 秒层，§3.1/§4.3） ----------------

const THESIS_KIND_META = {
  claim: { label: "已验证论断", cls: "bg-green-50 text-green-700 border-green-200" },
  draft: { label: "研究草稿（未验证）", cls: "bg-amber-50 text-amber-700 border-amber-200" },
  legacy_analysis: { label: "旧论点（legacy 分析，非披露事实）", cls: "bg-neutral-100 text-neutral-600 border-neutral-200" },
  none: { label: "无研究结论", cls: "bg-neutral-50 text-neutral-400 border-neutral-200" },
} as const;

export function ThesisPanel({ snap }: { snap: DossierSnapshot }) {
  const s = snap.summary;
  const kindMeta = THESIS_KIND_META[s.thesis_kind] ?? THESIS_KIND_META.none;
  return (
    <div className="rounded-lg border border-neutral-200 bg-white p-4">
      <div className="mb-1 flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold text-neutral-500">研究结论</span>
        <span className={`rounded-full border px-2 py-0.5 text-[10px] ${kindMeta.cls}`}>{kindMeta.label}</span>
        {snap.research.verdict && (
          <span className="rounded-full border border-neutral-200 px-2 py-0.5 text-[10px] text-neutral-500">
            研究充分度 {snap.research.verdict}（{snap.research.answered}/{snap.research.required} 问题）
          </span>
        )}
      </div>
      <p className="text-[15px] leading-relaxed text-neutral-800">
        {s.thesis ?? "尚无研究结论——点击右上「补研」发起问题驱动研究。"}
      </p>
      <div className="mt-3 grid gap-3 md:grid-cols-3">
        <div>
          <div className="mb-1 text-[11px] font-semibold text-neutral-500">最近变化</div>
          {s.key_changes.length ? (
            <ol className="space-y-1 text-xs text-neutral-700">
              {s.key_changes.map((c, i) => (
                <li key={i}><span className="mr-1 text-neutral-400">{["①", "②", "③", "④"][i] ?? `${i + 1}.`}</span>{c}</li>
              ))}
            </ol>
          ) : <div className="text-xs text-neutral-400">（本轮无记录变化）</div>}
        </div>
        <div>
          <div className="mb-1 text-[11px] font-semibold text-neutral-500">关键驱动</div>
          {s.drivers.length ? (
            <ul className="space-y-1 text-xs text-neutral-700">
              {s.drivers.map((d, i) => <li key={i}>→ {d}</li>)}
            </ul>
          ) : <div className="text-xs text-neutral-400">（待研究建立驱动链）</div>}
        </div>
        <div>
          <div className="mb-1 text-[11px] font-semibold text-neutral-500">最大反证</div>
          {s.counter_evidence
            ? <div className="rounded border border-red-100 bg-red-50/50 p-2 text-xs text-red-900">{s.counter_evidence}</div>
            : <div className="text-xs text-neutral-400">（反证义务：研究中必须主动寻找）</div>}
        </div>
      </div>
    </div>
  );
}

// ---------------- 关键指标栏（首屏 5-6 个，行业模板替换；缺口不补零） ----------------

export function KeyMetricBar({ snap, onMetricClick }: {
  snap: DossierSnapshot;
  onMetricClick?: (metric: KeyMetric) => void;  // review #21：回传指标对象，由页面用它自己的证据开抽屉
}) {
  const metrics = snap.summary.key_metrics.filter((m) => m.status !== "missing").slice(0, 6);
  const missing = snap.summary.key_metrics.filter((m) => m.status === "missing");
  return (
    <div className="flex flex-wrap gap-2">
      {metrics.map((m) => (
        <button
          key={m.metric_key}
          onClick={() => onMetricClick?.(m)}
          disabled={!(m.evidence_refs ?? []).length}
          className={`rounded-lg border bg-white px-3 py-2 text-left ${
            m.status === "conflicted" ? "border-red-200" : m.status === "stale" ? "border-orange-200" : "border-neutral-200"
          } ${m.observation_id ? "hover:border-neutral-400" : ""}`}
          title={`${m.period_label} · ${m.nature || "—"}${m.as_of_note ? ` · ${m.as_of_note}` : ""}（点击查看来源）`}
        >
          <div className="text-[10px] text-neutral-500">
            {m.label}
            {m.status === "stale" && <span className="ml-1 text-orange-600">陈旧</span>}
            {m.status === "conflicted" && <span className="ml-1 text-red-600">⚠冲突</span>}
          </div>
          <div className="font-mono text-base font-semibold tabular-nums text-neutral-900">
            <MetricValue value={m.value} unit={m.unit} currency={m.currency} />
          </div>
          <div className="font-mono text-[10px] text-neutral-400">{m.period_label}</div>
        </button>
      ))}
      {metrics.length === 0 && (
        <div className="rounded-lg border border-dashed border-neutral-300 px-3 py-2 text-xs text-neutral-400">
          无可靠 typed 指标——缺口如实显示（{missing.map((m) => m.label).join("、") || "无 KPI 定义"}），不以零或旧估计补位
        </div>
      )}
    </div>
  );
}

export function MetricValue({ value, unit, currency }: { value: string | null; unit: string; currency?: string | null }) {
  if (value === null || value === undefined) return <span className="text-neutral-300">—</span>;
  const n = Number(value);
  let text = value;
  if (Number.isFinite(n)) {
    const abs = Math.abs(n);
    if (unit === "ratio") text = `${(n * 100).toFixed(1)}%`;
    else if (abs >= 1e9) text = `${(n / 1e9).toFixed(2)}B`;
    else if (abs >= 1e6) text = `${(n / 1e6).toFixed(1)}M`;
    else text = n.toLocaleString("en-US", { maximumFractionDigits: 2 });
  }
  return <>{text}{currency && unit !== "ratio" ? <span className="ml-0.5 text-[10px] font-normal text-neutral-400">{currency}</span> : null}</>;
}

// ---------------- 来源抽屉（§4.6：一次点击到原文；键盘可达；小屏全屏） ----------------

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
    <div className="fixed inset-0 z-40 flex justify-end bg-black/20" onClick={onClose} role="dialog"
         aria-modal="true" aria-label="来源详情">
      <div
        className="h-full w-full max-w-md overflow-y-auto border-l border-neutral-200 bg-white p-4 shadow-xl sm:w-[28rem]"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-3 flex items-center justify-between">
          <h3 className="font-mono text-sm font-semibold">来源 · {evidenceId}</h3>
          <button ref={closeRef} onClick={onClose}
                  className="rounded border border-neutral-200 px-2 py-0.5 text-xs hover:border-neutral-400"
                  aria-label="关闭来源抽屉（Esc）">
            关闭 ✕
          </button>
        </div>
        {error && (
          <div className="rounded border border-red-200 bg-red-50 p-2 text-xs text-red-700">
            来源不可读：{error}（该快照未引用此证据或证据缺失——不生成虚假精度）
          </div>
        )}
        {detail && (
          <div className="space-y-3 text-xs">
            <div>
              <div className="mb-1 text-[11px] font-semibold text-neutral-500">原文摘录（逐字）</div>
              <blockquote className="rounded border-l-2 border-neutral-400 bg-neutral-50 p-2 leading-relaxed text-neutral-800">
                「{detail.verbatim_quote}」
              </blockquote>
            </div>
            <dl className="grid grid-cols-[7rem_1fr] gap-y-1.5">
              <dt className="text-neutral-500">采集供应商</dt>
              <dd className="font-mono">{detail.provider_id}</dd>
              <dt className="text-neutral-500">文档</dt>
              <dd>
                {detail.url
                  ? <a href={detail.url} target="_blank" rel="noreferrer" className="text-blue-700 hover:underline break-all">{detail.url}</a>
                  : "（无 URL）"}
              </dd>
              <dt className="text-neutral-500">公开可知</dt>
              <dd className="font-mono">{detail.available_at ?? "未知（C 级无 PIT 保证）"}</dd>
              <dt className="text-neutral-500">系统获取</dt>
              <dd className="font-mono">{detail.retrieved_at}</dd>
              <dt className="text-neutral-500">PIT 等级</dt>
              <dd>
                <span className={`rounded-full border px-1.5 py-0.5 font-mono text-[10px] ${
                  detail.pit_grade === "A" ? "border-green-300 bg-green-50 text-green-700"
                  : detail.pit_grade === "B" ? "border-amber-300 bg-amber-50 text-amber-700"
                  : "border-red-300 bg-red-50 text-red-700"
                }`}>{detail.pit_grade}</span>
                <span className="ml-2 text-[10px] text-neutral-400">
                  A=精确时间戳 B=可编辑发布时间 C=无 PIT 保证（评估模式永不可用）
                </span>
              </dd>
              {detail.document && (
                <>
                  <dt className="text-neutral-500">定位</dt>
                  <dd className="font-mono">
                    {Object.entries((detail.document as any).locator ?? {}).map(([k, v]) => `${k}:${v}`).join(" · ") || "（页码/章节未知，不生成虚假定位）"}
                  </dd>
                </>
              )}
            </dl>
          </div>
        )}
        {!detail && !error && <div className="text-xs text-neutral-400">读取中…</div>}
      </div>
    </div>
  );
}

// ---------------- 论断卡（判断/事实分离） ----------------

const CLAIM_KIND_LABEL: Record<string, string> = {
  fact_summary: "事实摘要", inference: "推论", hypothesis: "待验证假设", analysis: "分析",
};

export function ClaimCard({ claim, onRefClick }: { claim: ClaimItem; onRefClick?: (ref: string) => void }) {
  return (
    <div className={`rounded-lg border p-3 ${claim.status === "validated" ? "border-green-200 bg-green-50/30" : claim.status === "superseded" ? "border-neutral-200 bg-neutral-50 opacity-60" : "border-amber-200 bg-amber-50/30"}`}>
      <div className="mb-1 flex flex-wrap items-center gap-1.5">
        <span className="rounded bg-neutral-100 px-1.5 py-0.5 text-[10px] font-semibold text-neutral-600">
          {CLAIM_KIND_LABEL[claim.kind] ?? claim.kind}
        </span>
        <span className={`rounded-full border px-1.5 py-0.5 text-[10px] ${
          claim.status === "validated" ? "border-green-300 text-green-700"
          : claim.status === "draft" ? "border-amber-300 text-amber-700" : "border-neutral-300 text-neutral-400"
        }`}>
          {claim.status === "validated" ? "通过基础校验" : claim.status === "draft" ? "草稿" : "已被替代"}
        </span>
        {claim.legacy && <span className="rounded bg-neutral-100 px-1.5 py-0.5 text-[10px] text-neutral-500">legacy</span>}
        {claim.question_id && <span className="font-mono text-[10px] text-neutral-400">{claim.question_id}</span>}
      </div>
      <p className="text-sm leading-relaxed text-neutral-800">{claim.statement}</p>
      {(claim.support_refs.length > 0 || claim.counter_refs.length > 0) && (
        <div className="mt-1.5 flex flex-wrap gap-1 font-mono text-[10px]">
          {claim.support_refs.map((r) => (
            <RefChip key={r} refId={r} tone="support" onClick={onRefClick} />
          ))}
          {claim.counter_refs.map((r) => (
            <RefChip key={r} refId={r} tone="counter" onClick={onRefClick} />
          ))}
        </div>
      )}
      {claim.limitations.length > 0 && (
        <div className="mt-1.5 text-[11px] text-neutral-500">限制：{claim.limitations.join("；")}</div>
      )}
    </div>
  );
}

export function RefChip({ refId, tone, onClick }: {
  refId: string; tone: "support" | "counter"; onClick?: (ref: string) => void;
}) {
  const clickable = refId.startsWith("ev-") && onClick;
  return (
    <button
      disabled={!clickable}
      onClick={() => clickable && onClick?.(refId)}
      className={`rounded border px-1 py-0.5 ${
        tone === "counter" ? "border-red-200 bg-red-50 text-red-700" : "border-neutral-200 bg-white text-neutral-600"
      } ${clickable ? "cursor-pointer hover:border-neutral-400" : "cursor-default"}`}
      title={clickable ? "点击查看原文" : refId}
    >
      {tone === "counter" ? "反 " : "支 "}{refId}
    </button>
  );
}

// ---------------- 补研面板（§3.2 流程 2/3：问题驱动补研入口） ----------------

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
  const [idem] = useState(() => `ui-${snap.entity.id}-${Date.now()}`);

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
        idempotency_key: idem,  // 重复点击返回同一 command（不启动多份全量研究）
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
              className="rounded-md bg-neutral-900 px-3 py-1.5 text-xs font-semibold text-white hover:bg-neutral-700">
        补研
      </button>
    );
  }
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4" onClick={() => !busy && setOpen(false)}>
      <div className="w-full max-w-md rounded-lg border border-neutral-200 bg-white p-4 shadow-xl" onClick={(e) => e.stopPropagation()}>
        <h3 className="mb-2 text-sm font-semibold">
          发起研究 · {snap.entity.kind}:{snap.entity.id}
          {snap.context.mode === "historical" && (
            <span className="ml-2 rounded bg-indigo-50 px-1.5 py-0.5 text-[10px] font-normal text-indigo-700">
              以此为基线，研究当前情况（不回写过去）
            </span>
          )}
        </h3>
        <div className="space-y-2 text-xs">
          <label className="block">
            <span className="mb-0.5 block text-neutral-500">深度</span>
            <select value={depth} onChange={(e) => setDepth(e.target.value)}
                    className="w-full rounded border border-neutral-200 px-2 py-1.5">
              <option value="standard">standard（6-10 个关键问题）</option>
              <option value="deep">deep（12-18 个问题 + 反方审查）</option>
              <option value="refresh">refresh（新披露后的增量更新）</option>
              <option value="targeted">targeted（指定问题/模块缺口）</option>
            </select>
          </label>
          <label className="block">
            <span className="mb-0.5 block text-neutral-500">研究目标（自然语言）</span>
            <input value={objective} onChange={(e) => setObjective(e.target.value)}
                   placeholder="如：评估数据中心订单转化为收入的确定性"
                   className="w-full rounded border border-neutral-200 px-2 py-1.5" />
          </label>
          {(depth === "targeted" || depth === "refresh") && (
            <label className="block">
              <span className="mb-0.5 block text-neutral-500">聚焦问题/模块</span>
              <input value={focus} onChange={(e) => setFocus(e.target.value)}
                     placeholder="如：最近两季 backlog 缺失"
                     className="w-full rounded border border-neutral-200 px-2 py-1.5" />
            </label>
          )}
          {error && <div className="rounded border border-red-200 bg-red-50 p-2 text-red-700">{error}</div>}
          <div className="flex justify-end gap-2 pt-1">
            <button onClick={() => setOpen(false)} disabled={busy}
                    className="rounded border border-neutral-200 px-3 py-1.5 hover:border-neutral-400">取消</button>
            <button onClick={submit} disabled={busy}
                    className="rounded bg-neutral-900 px-3 py-1.5 font-semibold text-white hover:bg-neutral-700 disabled:opacity-50">
              {busy ? "发起中…" : "进入 Sessions 执行"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------- 导出（§11.4：JSON/Markdown 冻结导出，与页面同源） ----------------

export function ExportMenu({ snap }: { snap: DossierSnapshot }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const doExport = async (format: "json" | "markdown") => {
    setBusy(format); setError(null);
    try {
      const job = await dossierApi.exportSnapshot(snap.context.snapshot_id, format);
      // 触发浏览器下载（工件 URL 绑定 job）
      const a = document.createElement("a");
      a.href = dossierApi.jobArtifactUrl(job.job_id);
      a.download = job.artifact_name;
      document.body.appendChild(a);
      a.click();
      a.remove();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="relative">
      <div className="flex gap-1">
        <button onClick={() => doExport("markdown")} disabled={busy !== null}
                className="rounded border border-neutral-200 px-2.5 py-1.5 text-xs hover:border-neutral-400 disabled:opacity-50"
                title="导出与页面同源的 Markdown（绑定 data_hash 与截止时间）">
          {busy === "markdown" ? "导出中…" : "导出 MD"}
        </button>
        <button onClick={() => doExport("json")} disabled={busy !== null}
                className="rounded border border-neutral-200 px-2.5 py-1.5 text-xs hover:border-neutral-400 disabled:opacity-50"
                title="导出冻结 Dossier JSON（读模型契约）">
          {busy === "json" ? "导出中…" : "导出 JSON"}
        </button>
      </div>
      {error && <div className="absolute right-0 top-full z-10 mt-1 w-64 rounded border border-red-200 bg-red-50 p-2 text-[11px] text-red-700">{error}</div>}
    </div>
  );
}

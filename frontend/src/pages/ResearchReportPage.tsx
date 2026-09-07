// 冻结研究报告页（#/research/<artifact_id>，设计 §4.1/§7.8）：
// ReportDocument 渲染的 markdown 与页面同源；产物状态（draft/validated/superseded）
// 与充分度（sufficient/partial/blocked）分开展示——运行完成 ≠ 可用研报。

import { useEffect, useState } from "react";

import { navigate } from "../app/route";
import { dossierApi } from "../features/dossier/api";
import { Markdown } from "../lib/markdown";
import type { ResearchArtifactJson } from "../features/dossier/types";

const STATUS_META: Record<string, { label: string; cls: string }> = {
  validated: { label: "通过基础校验", cls: "border-green-300 bg-green-50 text-green-700" },
  draft: { label: "草稿（未通过验证）", cls: "border-amber-300 bg-amber-50 text-amber-700" },
  superseded: { label: "已被替代", cls: "border-neutral-300 bg-neutral-100 text-neutral-500" },
};

const SUFFICIENCY_META: Record<string, { label: string; cls: string }> = {
  sufficient: { label: "研究充分", cls: "border-green-300 text-green-700" },
  partial: { label: "部分完成（缺口已标注）", cls: "border-amber-300 text-amber-700" },
  blocked: { label: "受阻（硬门禁未过/无产出）", cls: "border-red-300 text-red-700" },
};

export default function ResearchReportPage({ artifactId }: { artifactId: string }) {
  const [artifact, setArtifact] = useState<ResearchArtifactJson | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setArtifact(null); setError(null);
    dossierApi.artifact(artifactId)
      .then((a) => { if (!cancelled) setArtifact(a); })
      .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
  }, [artifactId]);

  if (error) {
    return (
      <div className="mx-auto max-w-md rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
        <div className="mb-1 font-semibold">研究报告不可读</div>
        <div className="mb-3 text-xs">{error}</div>
        <button onClick={() => navigate({ page: "knowledge", params: {} })}
                className="rounded border border-neutral-300 bg-white px-3 py-1 text-xs">
          ← 返回档案库
        </button>
      </div>
    );
  }
  if (!artifact) {
    return <div className="py-16 text-center text-sm text-neutral-400">报告加载中…</div>;
  }
  const status = STATUS_META[artifact.status] ?? STATUS_META.draft;
  const suff = SUFFICIENCY_META[artifact.sufficiency] ?? SUFFICIENCY_META.partial;
  return (
    <div className="mx-auto max-w-[840px]">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <button
          onClick={() => navigate({
            page: "knowledge", kind: artifact.entity_kind as "stock" | "industry",
            id: artifact.entity_id, params: { section: "research_sources" },
          })}
          className="rounded border border-neutral-200 px-2 py-1 text-xs text-neutral-500 hover:border-neutral-400"
        >
          ← 返回 {artifact.entity_kind}:{artifact.entity_id} 档案
        </button>
        <span className={`rounded-full border px-2 py-0.5 text-[11px] ${status.cls}`}>{status.label}</span>
        <span className={`rounded-full border px-2 py-0.5 text-[11px] ${suff.cls}`}>{suff.label}</span>
      </div>
      <header className="mb-4 border-b border-neutral-200 pb-3">
        <h2 className="text-xl font-bold text-neutral-900">{artifact.title}</h2>
        <div className="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 font-mono text-[11px] text-neutral-400">
          <span>{artifact.artifact_id}</span>
          <span>生成 {artifact.created_at.replace("T", " ").slice(0, 19)}</span>
          {artifact.evidence_cutoff && <span>证据截止 {artifact.evidence_cutoff.slice(0, 10)}</span>}
          {artifact.plan_id && <span>计划 {artifact.plan_id}</span>}
        </div>
        <div className="mt-1 text-[11px] text-neutral-400">
          「通过基础校验」= 本系统引用与数值检查通过，不表示人工审计或投资结论一定正确。
        </div>
      </header>
      {artifact.validation_issues.length > 0 && (
        <div className="mb-4 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
          <div className="mb-1 font-semibold">校验备注（{artifact.validation_issues.length}）</div>
          <ul className="space-y-0.5">
            {artifact.validation_issues.slice(0, 10).map((i, idx) => (
              <li key={idx}>
                <span className={`mr-1 rounded px-1 font-mono text-[10px] ${i.hard ? "bg-red-100 text-red-700" : "bg-amber-100 text-amber-700"}`}>
                  {i.hard ? "HARD" : "soft"}
                </span>
                {i.code}{i.ref ? ` (${i.ref})` : ""}: {i.message}
              </li>
            ))}
          </ul>
        </div>
      )}
      <article className="text-sm leading-relaxed text-neutral-800">
        <Markdown text={artifact.markdown} />
      </article>
    </div>
  );
}

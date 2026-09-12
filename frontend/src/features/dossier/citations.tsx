// 引用编号（优化方案 §16/§48.3）：正文不再出现 raw evidence ID（ev-xxx 是数据库
// 主键，不是阅读界面元素）——正文用 [1][2][3] 编号引用，hover 看来源摘要，
// 点击开 Evidence Drawer；raw ID 只在抽屉底部与审计模式出现。
//
// 编号纪律：同一组件树内同一证据编号稳定（按首次出现顺序编号，纯函数可测）。

import { useState } from "react";

/** 按首次出现顺序为证据引用编号（确定性；同一 ref 多处引用共享同一编号）。 */
export function buildCitationIndex(lists: (readonly string[] | undefined)[]): Map<string, number> {
  const index = new Map<string, number>();
  for (const list of lists) {
    for (const ref of list ?? []) {
      if (typeof ref === "string" && ref.startsWith("ev-") && !index.has(ref)) {
        index.set(ref, index.size + 1);
      }
    }
  }
  return index;
}

/** 引用编号 chips：[n]。非证据引用（claim-/obs- 等）在投资者界面不展示。
 *  密度纪律：默认最多显示 6 个，其余收进「+N」——一排 16 个编号是噪音不是可读性。 */
export function CitationChips({ refs, index, onCite, className = "", maxVisible = 6 }: {
  refs: readonly string[] | undefined;
  /** 页面级编号表（buildCitationIndex）；缺省时按本组内顺序临时编号 */
  index?: Map<string, number>;
  onCite: (evidenceId: string) => void;
  className?: string;
  maxVisible?: number;
}) {
  const evRefs = (refs ?? []).filter((r) => typeof r === "string" && r.startsWith("ev-"));
  const [expanded, setExpanded] = useState(false);
  if (!evRefs.length) return null;
  const local = index ?? buildCitationIndex([evRefs]);
  const visible = expanded ? evRefs : evRefs.slice(0, maxVisible);
  const hidden = evRefs.length - visible.length;
  return (
    <span className={`ml-1 inline-flex flex-wrap items-center gap-1 align-middle ${className}`}>
      {visible.map((ref) => {
        const n = local.get(ref) ?? 0;
        return (
          <button
            key={ref}
            onClick={(e) => { e.stopPropagation(); onCite(ref); }}
            title={`来源 [${n}]：点击查看原文摘录与出处`}
            className="inline-flex h-[18px] min-w-[20px] items-center justify-center rounded border border-line bg-white px-1 font-mono text-[11px] leading-none text-accent hover:border-accent hover:bg-accent-soft"
          >
            {n}
          </button>
        );
      })}
      {hidden > 0 && (
        <button
          onClick={(e) => { e.stopPropagation(); setExpanded(true); }}
          title={`展开其余 ${hidden} 条来源引用`}
          className="inline-flex h-[18px] items-center justify-center rounded border border-dashed border-line px-1 font-mono text-[11px] leading-none text-ink-faint hover:border-accent hover:text-accent"
        >
          +{hidden}
        </button>
      )}
    </span>
  );
}

/** 审计模式专用：raw ref 列表（ev/obs/claim 原样展示，进「研究与来源」区）。 */
export function RawRefChips({ refs, onCite }: {
  refs: readonly string[] | undefined;
  onCite: (evidenceId: string) => void;
}) {
  const list = (refs ?? []).filter(Boolean);
  if (!list.length) return null;
  return (
    <span className="flex flex-wrap gap-1 font-mono text-[11px]">
      {list.map((r) => (
        r.startsWith("ev-")
          ? <button key={r} onClick={() => onCite(r)}
                    className="rounded border border-line px-1 py-0.5 text-ink-soft hover:border-accent hover:text-accent">{r}</button>
          : <span key={r} className="rounded border border-line/60 px-1 py-0.5 text-ink-faint">{r}</span>
      ))}
    </span>
  );
}

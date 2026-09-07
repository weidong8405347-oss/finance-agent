// Hash 路由（设计 §4.1）：可解析的路由对象——实体、章节、时间、快照、报告、
// 证据级深链；刷新恢复、前进后退可用；兼容旧 `#knowledge` 等入口。
//
//   #/knowledge                                   档案库
//   #/knowledge/stock/BE?section=overview          股票档案
//   #/knowledge/stock/BE?section=financials&as_of=<ISO>&namespace=prod
//   #/knowledge/stock/BE?section=sources&evidence=<evidence_id>&snapshot=<id>
//   #/knowledge/industry/ai-for-science            行业档案
//   #/research/<artifact_id>                       冻结的完整研究报告
//   #/sessions|decisions|evaluations|capabilities|providers（旧入口原样兼容）

export type TopPage =
  | "sessions" | "decisions" | "evaluations" | "capabilities" | "providers";

export type Route =
  | { page: TopPage }
  | {
      page: "knowledge";
      kind?: "stock" | "industry";
      id?: string;
      params: Record<string, string>; // section/as_of/namespace/snapshot/evidence...
    }
  | { page: "research"; artifactId: string; params: Record<string, string> }
  | { page: "compare"; params: Record<string, string> }
  | { page: "not_found"; raw: string };

const TOP_PAGES: readonly string[] = [
  "sessions", "decisions", "evaluations", "capabilities", "providers",
];

export function parseHash(hash: string): Route {
  const raw = hash.replace(/^#\/?/, "");
  const [pathPart, queryPart = ""] = raw.split("?");
  const params: Record<string, string> = {};
  new URLSearchParams(queryPart).forEach((v, k) => { params[k] = v; });
  const segs = pathPart.split("/").filter(Boolean).map((s) => {
    try { return decodeURIComponent(s); } catch { return s; }
  });
  if (segs.length === 0) return { page: "sessions" };
  const head = segs[0].toLowerCase();
  if (head === "knowledge") {
    const kind = segs[1]?.toLowerCase();
    if ((kind === "stock" || kind === "industry") && segs[2]) {
      return { page: "knowledge", kind, id: segs[2], params };
    }
    return { page: "knowledge", params };
  }
  if (head === "research" && segs[1]) {
    return { page: "research", artifactId: segs[1], params };
  }
  if (head === "compare") {
    return { page: "compare", params };
  }
  if (TOP_PAGES.includes(head) && segs.length === 1) {
    return { page: head as TopPage };
  }
  return { page: "not_found", raw };
}

export function buildHash(route: Route): string {
  const q = (params: Record<string, string>) => {
    const sp = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== "") sp.set(k, v);
    }
    const s = sp.toString();
    return s ? `?${s}` : "";
  };
  switch (route.page) {
    case "knowledge": {
      if (route.kind && route.id) {
        return `#/knowledge/${route.kind}/${encodeURIComponent(route.id)}${q(route.params)}`;
      }
      return `#/knowledge${q(route.params)}`;
    }
    case "research":
      return `#/research/${encodeURIComponent(route.artifactId)}${q(route.params)}`;
    case "compare":
      return `#/compare${q(route.params)}`;
    case "not_found":
      return `#/${route.raw}`;
    default:
      return `#/${route.page}`;
  }
}

export function navigate(route: Route): void {
  const next = buildHash(route);
  if (window.location.hash !== next) window.location.hash = next;
}

/** 路由订阅：hashchange → 解析后的 Route（App 层唯一状态源）。 */
export function subscribeRoute(cb: (route: Route) => void): () => void {
  const handler = () => cb(parseHash(window.location.hash));
  window.addEventListener("hashchange", handler);
  return () => window.removeEventListener("hashchange", handler);
}

export function currentRoute(): Route {
  return parseHash(window.location.hash);
}

/** 深链参数更新（保留当前实体，改 section/as_of/evidence 等）。 */
export function withParams(route: Route, patch: Record<string, string | null>): Route {
  if (route.page !== "knowledge" && route.page !== "research" && route.page !== "compare") {
    return route;
  }
  const params = { ...route.params };
  for (const [k, v] of Object.entries(patch)) {
    if (v === null || v === "") delete params[k];
    else params[k] = v;
  }
  return { ...route, params } as Route;
}

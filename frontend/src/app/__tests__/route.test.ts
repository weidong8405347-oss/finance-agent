// hash 路由契约（设计 §4.1）：深链解析/构建、旧入口兼容、参数编码。
import { describe, expect, it } from "vitest";

import { buildHash, parseHash, withParams } from "../route";

describe("parseHash", () => {
  it("空 hash → sessions", () => {
    expect(parseHash("")).toEqual({ page: "sessions" });
    expect(parseHash("#")).toEqual({ page: "sessions" });
  });

  it("兼容旧入口 #knowledge / #sessions（无斜杠）", () => {
    expect(parseHash("#knowledge")).toEqual({ page: "knowledge", params: {} });
    expect(parseHash("#decisions")).toEqual({ page: "decisions" });
  });

  it("档案库与实体深链", () => {
    expect(parseHash("#/knowledge")).toEqual({ page: "knowledge", params: {} });
    const r = parseHash("#/knowledge/stock/BE?section=overview");
    expect(r).toEqual({ page: "knowledge", kind: "stock", id: "BE", params: { section: "overview" } });
  });

  it("时间/快照/证据级深链", () => {
    const r = parseHash(
      "#/knowledge/stock/BE?section=financials&as_of=2026-09-07T06:00:00Z&namespace=prod",
    );
    expect(r.page === "knowledge" && r.params.as_of === "2026-09-07T06:00:00Z").toBe(true);
    const s = parseHash("#/knowledge/stock/BE?section=sources&evidence=ev-123&snapshot=dossier-be-abc");
    expect(s.page === "knowledge" && s.params.evidence === "ev-123" && s.params.snapshot === "dossier-be-abc").toBe(true);
  });

  it("行业与研报与比较路由", () => {
    expect(parseHash("#/knowledge/industry/ai-for-science")).toEqual({
      page: "knowledge", kind: "industry", id: "ai-for-science", params: {},
    });
    expect(parseHash("#/research/artifact-abc123")).toEqual({
      page: "research", artifactId: "artifact-abc123", params: {},
    });
    const c = parseHash("#/compare?entities=stock:BE,stock:PLUG&metric=revenue");
    expect(c.page === "compare" && c.params.metric === "revenue").toBe(true);
  });

  it("URL 编码的港股 id 可往返", () => {
    const r = parseHash(buildHash({ page: "knowledge", kind: "stock", id: "2228.HK", params: { section: "key_kpi" } }));
    expect(r.page === "knowledge" && r.id === "2228.HK" && r.params.section === "key_kpi").toBe(true);
  });

  it("未知路由 → not_found（不静默落到别的页）", () => {
    expect(parseHash("#/whatever/x")).toEqual({ page: "not_found", raw: "whatever/x" });
  });
});

describe("buildHash / withParams", () => {
  it("空参数不进 query", () => {
    expect(buildHash({ page: "knowledge", kind: "stock", id: "BE", params: { section: "", as_of: "x" } }))
      .toBe("#/knowledge/stock/BE?as_of=x");
  });

  it("withParams 保留实体改章节/时间", () => {
    const base = parseHash("#/knowledge/stock/BE?section=overview&snapshot=s1");
    const next = withParams(base, { section: "financials", snapshot: null, as_of: "2026-01-01T00:00:00Z" });
    expect(buildHash(next)).toBe("#/knowledge/stock/BE?section=financials&as_of=2026-01-01T00%3A00%3A00Z");
  });

  it("刷新恢复：build(parse(h)) 语义等价", () => {
    const h = "#/knowledge/stock/BE?section=sources&evidence=ev-1";
    expect(buildHash(parseHash(h))).toBe(h);
  });
});

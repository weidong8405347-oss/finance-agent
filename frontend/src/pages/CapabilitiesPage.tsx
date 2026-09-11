// 能力页：主 agent + 每个 command 的 step 分解（工具/插件/hook/预算/模型）。
// 数据源 GET /api/capabilities；新增 tools/skills/MCP 后在 STEP_MANIFEST 登记即在此可见。
import { useEffect, useState } from "react";
import { api, Capabilities } from "../api";

export default function CapabilitiesPage() {
  const [cap, setCap] = useState<Capabilities | null>(null);
  useEffect(() => {
    api.capabilities().then(setCap).catch(() => setCap(null));
  }, []);

  if (!cap) return <div className="text-sm text-neutral-400">加载中…</div>;

  return (
    <div className="space-y-6">
      <section className="rounded-lg border border-neutral-200 bg-white p-4">
        <h2 className="mb-2 text-sm font-semibold">主 agent（对话入口）</h2>
        <div className="mb-2 font-mono text-xs text-neutral-500">
          模型：<span className="text-neutral-800">{cap.main_agent.model}</span>
          {Object.entries(cap.models).map(([role, m]) => (
            <span key={role} className="ml-3">{role} → <span className="text-neutral-800">{m}</span></span>
          ))}
        </div>
        <div className="flex flex-wrap gap-1">
          {cap.main_agent.tools.map((t) => (
            <span key={t} className="rounded border border-neutral-200 bg-neutral-50 px-1.5 py-0.5 font-mono text-[11px]">{t}</span>
          ))}
        </div>
        <div className="mt-2 text-[11px] text-neutral-400">
          数据源（DataGateway，PIT 分级）：{cap.gateway_sources.join("、") || "无"}
        </div>
      </section>

      {cap.plugins && cap.plugins.plugins && (
        <section className="rounded-lg border border-neutral-200 bg-white p-4">
          <div className="mb-2 flex items-baseline gap-3">
            <h2 className="text-sm font-semibold">插件编译视图（P1-C）</h2>
            <span className="font-mono text-[10px] text-neutral-400">
              stage={cap.plugins.stage} · config_hash={cap.plugins.config_hash}
            </span>
          </div>
          <div className="space-y-1">
            {cap.plugins.plugins.map((p) => (
              <div key={p.id} className="flex items-center gap-2 rounded border border-neutral-100 bg-neutral-50/50 px-2 py-1 text-xs">
                <span className="font-mono font-semibold">{p.id}</span>
                <span className="font-mono text-[10px] text-neutral-400">v{p.version} · {p.kind}</span>
                <span className={
                  p.status === "enabled" ? "rounded bg-emerald-50 px-1.5 py-0.5 text-[10px] text-emerald-700"
                  : p.status === "degraded" ? "rounded bg-amber-50 px-1.5 py-0.5 text-[10px] text-amber-700"
                  : "rounded bg-neutral-100 px-1.5 py-0.5 text-[10px] text-neutral-500"
                }>{p.status}</span>
                {p.reason && <span className="text-[11px] text-neutral-500">{p.reason}</span>}
                <span className="ml-auto font-mono text-[10px] text-neutral-400">
                  {p.tools.join(" ")}
                </span>
              </div>
            ))}
          </div>
          <div className="mt-2 text-[11px] text-neutral-400">
            能力页从实际编译结果生成；缺凭证显示 missing_config 与原因，非关键源失败不阻断研究。
          </div>
        </section>
      )}

      {cap.commands.map((c) => (
        <section key={c.name} className="rounded-lg border border-neutral-200 bg-white p-4">
          <div className="mb-1 flex items-baseline gap-3">
            <span className="font-mono text-sm font-bold">/{c.name}</span>
            <span className="text-xs text-neutral-500">{c.summary}</span>
            {c.needs_approval && (
              <span className="rounded bg-amber-50 px-1.5 py-0.5 text-[10px] text-amber-700">默认需审批</span>
            )}
          </div>
          <div className="mb-2 font-mono text-[11px] text-neutral-400">{c.usage}</div>
          <div className="space-y-2">
            {c.steps.map((s, i) => (
              <div key={s.step} className="rounded-md border border-neutral-100 bg-neutral-50/50 p-3">
                <div className="mb-1 flex items-center gap-2 text-xs">
                  <span className="font-mono text-neutral-400">{i + 1}.</span>
                  <b>{s.title ?? s.step}</b>
                  {s.model_role && (
                    <span className="font-mono text-[10px] text-neutral-500">
                      模型角色：{s.model_role}（{cap.models[(s.model_role || "research").replace(/\(.*\)/, "")] ?? "—"}）
                    </span>
                  )}
                  {s.budget && Object.keys(s.budget).length > 0 && (
                    <span className="ml-auto font-mono text-[10px] text-neutral-400">
                      预算 {Object.entries(s.budget).map(([k, v]) => `${k}=${v}`).join(" ")}
                    </span>
                  )}
                </div>
                <div className="grid grid-cols-3 gap-2 text-[11px]">
                  <div>
                    <div className="mb-0.5 text-neutral-400">tools</div>
                    {(s.tools ?? []).map((t) => (
                      <div key={t} className="font-mono text-neutral-700">{t}</div>
                    ))}
                  </div>
                  <div>
                    <div className="mb-0.5 text-neutral-400">plugins（可插拔增强）</div>
                    {(s.plugins ?? []).map((t) => (
                      <div key={t} className="text-neutral-700">{t}</div>
                    ))}
                    {(s.plugins ?? []).length === 0 && <div className="text-neutral-300">—</div>}
                  </div>
                  <div>
                    <div className="mb-0.5 text-neutral-400">hooks（必达门禁）</div>
                    {(s.hooks ?? []).map((t) => (
                      <div key={t} className="text-neutral-700">{t}</div>
                    ))}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}

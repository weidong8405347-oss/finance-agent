// P5 provider 自配页：列表/新增/编辑/测活/恢复默认。
// 状态变换全在 lib/providers.ts（纯逻辑可测）；本组件只负责渲染与事件。
// 纪律：api_key 永不回显明文（掩码 "***" = 保存时沿用已存）；非法配置后端 422 落错误条。
import { useEffect, useMemo, useState } from "react";
import { api, ProviderProbeResult } from "../api";
import {
  EFFORT_LEVELS,
  FormState,
  KNOWN_ROLES,
  parseModels,
  roleTargetOptions,
  toFormState,
  toPayload,
  validateForm,
} from "../lib/providers";

export default function ProvidersPage() {
  const [form, setForm] = useState<FormState | null>(null);
  const [source, setSource] = useState<string>("");
  const [configPath, setConfigPath] = useState<string>("");
  const [effectiveRoles, setEffectiveRoles] = useState<Record<string, string>>({});
  const [errors, setErrors] = useState<string[]>([]);
  const [notice, setNotice] = useState<string>("");
  const [probes, setProbes] = useState<Record<string, ProviderProbeResult>>({});
  const [busy, setBusy] = useState(false);

  const load = () => {
    api.providers().then((v) => {
      setForm(toFormState(v.file));
      setSource(v.source);
      setConfigPath(v.config_path);
      setEffectiveRoles(v.effective.role_map ?? {});
      setErrors([]);
    }).catch((e) => setErrors([String(e)]));
  };
  useEffect(load, []);

  const targets = useMemo(() => (form ? roleTargetOptions(form) : []), [form]);

  if (!form) return <div className="text-sm text-neutral-400">加载中…</div>;

  const setRow = (i: number, patch: Partial<FormState["providers"][0]>) => {
    setForm({
      ...form,
      providers: form.providers.map((r, j) => (j === i ? { ...r, ...patch } : r)),
    });
  };

  const save = async () => {
    const errs = validateForm(form);
    setErrors(errs);
    if (errs.length > 0) return;
    setBusy(true);
    try {
      const v = await api.saveProviders(toPayload(form));
      setForm(toFormState(v.file));
      setSource(v.source);
      setNotice("已保存（热生效，无需重启）");
      setErrors([]);
    } catch (e) {
      setErrors([String(e)]);
      setNotice("");
    } finally {
      setBusy(false);
    }
  };

  const reset = async () => {
    setBusy(true);
    try {
      const v = await api.resetProviders();
      setForm(toFormState(v.file));
      setSource(v.source);
      setNotice("已删除自有配置，回落 pi/.env 兜底");
    } catch (e) {
      setErrors([String(e)]);
    } finally {
      setBusy(false);
    }
  };

  const probe = async (i: number) => {
    const row = form.providers[i];
    const model = parseModels(row.models)[0] ?? "";
    setProbes({ ...probes, [row.name]: { ok: false, latency_ms: 0, error: "测活中…" } });
    try {
      const r = await api.testProvider({
        name: row.name, base_url: row.base_url, api_key: row.api_key, model,
      });
      setProbes({ ...probes, [row.name]: r });
    } catch (e) {
      setProbes({ ...probes, [row.name]: { ok: false, latency_ms: 0, error: String(e) } });
    }
  };

  return (
    <div className="space-y-6">
      <section className="rounded-lg border border-neutral-200 bg-white p-4">
        <div className="mb-3 flex items-center gap-3">
          <h2 className="text-sm font-semibold">模型 Provider 配置</h2>
          <span className={`rounded px-1.5 py-0.5 text-[10px] ${
            source === "own" ? "bg-emerald-50 text-emerald-700" : "bg-neutral-100 text-neutral-500"
          }`}>
            {source === "own" ? "自有配置生效" : "pi/.env 兜底中"}
          </span>
          <span className="font-mono text-[11px] text-neutral-400">{configPath}</span>
        </div>
        {Object.keys(effectiveRoles).length > 0 && (
          <div className="mb-3 font-mono text-[11px] text-neutral-500">
            当前生效角色路由：
            {Object.entries(effectiveRoles).map(([role, target]) => (
              <span key={role} className="ml-3">{role} → <span className="text-neutral-800">{target}</span></span>
            ))}
          </div>
        )}
        {notice && <div className="mb-3 rounded bg-emerald-50 px-3 py-1.5 text-xs text-emerald-700">{notice}</div>}
        {errors.length > 0 && (
          <div className="mb-3 rounded bg-red-50 px-3 py-2 text-xs text-red-700">
            {errors.map((e, i) => <div key={i}>• {e}</div>)}
          </div>
        )}

        <div className="space-y-3">
          {form.providers.map((row, i) => (
            <div key={i} className="rounded-md border border-neutral-100 bg-neutral-50/50 p-3">
              <div className="grid grid-cols-[140px_1fr_1fr] gap-2">
                <input
                  className="rounded border border-neutral-200 px-2 py-1 font-mono text-xs"
                  placeholder="provider 名"
                  value={row.name}
                  onChange={(e) => setRow(i, { name: e.target.value })}
                />
                <input
                  className="rounded border border-neutral-200 px-2 py-1 font-mono text-xs"
                  placeholder="base_url（OpenAI 兼容，如 …/v1）"
                  value={row.base_url}
                  onChange={(e) => setRow(i, { base_url: e.target.value })}
                />
                <input
                  className="rounded border border-neutral-200 px-2 py-1 font-mono text-xs"
                  placeholder="api_key（*** 沿用已存 / env:VAR / 明文）"
                  value={row.api_key}
                  onChange={(e) => setRow(i, { api_key: e.target.value })}
                />
              </div>
              <div className="mt-2 flex items-center gap-2">
                <input
                  className="flex-1 rounded border border-neutral-200 px-2 py-1 font-mono text-xs"
                  placeholder="models（逗号分隔，首个为默认）"
                  value={row.models}
                  onChange={(e) => setRow(i, { models: e.target.value })}
                />
                <button
                  className="rounded border border-neutral-300 px-2 py-1 text-xs hover:bg-neutral-100"
                  onClick={() => probe(i)}
                >
                  测活
                </button>
                <button
                  className="rounded border border-red-200 px-2 py-1 text-xs text-red-600 hover:bg-red-50"
                  onClick={() =>
                    setForm({ ...form, providers: form.providers.filter((_, j) => j !== i) })
                  }
                >
                  删除
                </button>
              </div>
              {probes[row.name] && (
                <div className={`mt-1 font-mono text-[11px] ${
                  probes[row.name].ok ? "text-emerald-600" : "text-red-600"
                }`}>
                  {probes[row.name].ok
                    ? `✓ 通（${probes[row.name].latency_ms}ms）`
                    : `✗ ${probes[row.name].error}`}
                </div>
              )}
            </div>
          ))}
        </div>

        <button
          className="mt-3 rounded border border-neutral-300 px-3 py-1 text-xs hover:bg-neutral-100"
          onClick={() =>
            setForm({
              ...form,
              providers: [...form.providers, { name: "", base_url: "", api_key: "", models: "" }],
            })
          }
        >
          + 新增 provider
        </button>
      </section>

      <section className="rounded-lg border border-neutral-200 bg-white p-4">
        <h2 className="mb-3 text-sm font-semibold">角色路由（role → provider:model）与每角色 effort/timeout</h2>
        <div className="mb-3 flex items-center gap-2 text-xs">
          <span className="text-neutral-500">默认 provider：</span>
          <select
            className="rounded border border-neutral-200 px-2 py-1 font-mono text-xs"
            value={form.default_provider}
            onChange={(e) => setForm({ ...form, default_provider: e.target.value })}
          >
            <option value="">（首个 provider）</option>
            {form.providers.filter((r) => r.name.trim()).map((r) => (
              <option key={r.name} value={r.name.trim()}>{r.name.trim()}</option>
            ))}
          </select>
        </div>
        <div className="space-y-2">
          {KNOWN_ROLES.map((role) => {
            const opt = form.role_options[role] ?? { effort: "", timeout: "" };
            return (
              <div key={role} className="grid grid-cols-[170px_1fr_110px_100px] items-center gap-2">
                <span className="font-mono text-xs text-neutral-600">{role}</span>
                <select
                  className="rounded border border-neutral-200 px-2 py-1 font-mono text-xs"
                  value={form.role_map[role] ?? ""}
                  onChange={(e) =>
                    setForm({ ...form, role_map: { ...form.role_map, [role]: e.target.value } })
                  }
                >
                  <option value="">（默认/兜底）</option>
                  {targets.map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
                <select
                  className="rounded border border-neutral-200 px-2 py-1 font-mono text-xs"
                  value={opt.effort}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      role_options: { ...form.role_options, [role]: { ...opt, effort: e.target.value } },
                    })
                  }
                >
                  {EFFORT_LEVELS.map((lv) => (
                    <option key={lv} value={lv}>{lv || "（effort 默认）"}</option>
                  ))}
                </select>
                <input
                  className="rounded border border-neutral-200 px-2 py-1 font-mono text-xs"
                  placeholder="timeout s"
                  value={opt.timeout}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      role_options: { ...form.role_options, [role]: { ...opt, timeout: e.target.value } },
                    })
                  }
                />
              </div>
            );
          })}
        </div>
      </section>

      <div className="flex gap-3">
        <button
          className="rounded bg-neutral-900 px-4 py-1.5 text-sm text-white disabled:opacity-40"
          onClick={save}
          disabled={busy}
        >
          保存（热生效）
        </button>
        <button
          className="rounded border border-red-300 px-4 py-1.5 text-sm text-red-600 hover:bg-red-50 disabled:opacity-40"
          onClick={reset}
          disabled={busy}
        >
          恢复默认（删除自有配置）
        </button>
      </div>
    </div>
  );
}

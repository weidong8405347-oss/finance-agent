// P5 provider 自配页的纯逻辑层（页面只负责渲染/事件，状态变换全在这里——可测）。
//
// 契约对齐后端 /api/providers：
// - api_key 掩码 "***" = 保存时沿用已存 key（后端继承语义）；
// - providers 为空不允许保存（清空请用 reset）；
// - role_map 值必须指向已配置的 provider:model。

export const KEY_MASK = "***";

export const KNOWN_ROLES = [
  "research",
  "research-alt",
  "research-worker-1",
  "research-worker-2",
  "research-worker-3",
  "fast",
] as const;

export const EFFORT_LEVELS = ["", "minimal", "low", "medium", "high", "max"] as const;

export interface ProviderRow {
  name: string;
  base_url: string;
  api_key: string; // 编辑态：可能是 KEY_MASK / env:VAR / 明文
  models: string; // 逗号分隔编辑态
}

export interface FormState {
  providers: ProviderRow[];
  default_provider: string;
  role_map: Record<string, string>;
  role_options: Record<string, { effort: string; timeout: string }>;
}

export interface ProviderFileEntry {
  base_url: string;
  api_key: string;
  models: string[];
}

export interface ProvidersFile {
  providers: Record<string, ProviderFileEntry>;
  default_provider?: string;
  role_map?: Record<string, string>;
  role_options?: Record<string, { effort?: string; timeout?: number }>;
}

export interface ProvidersView {
  source: "own" | "fallback";
  config_path: string;
  file: ProvidersFile | null;
  effective: {
    providers?: { name: string; base_url: string; models: string[]; has_key: boolean }[];
    default_provider?: string;
    role_map?: Record<string, string>;
    role_options?: Record<string, { effort?: string; timeout?: number }>;
    error?: string;
  };
}

/** models 逗号分隔编辑态 → 模型 id 列表（唯一解析口径，save/validate/probe 共用）。 */
export function parseModels(csv: string): string[] {
  return csv.split(",").map((m) => m.trim()).filter(Boolean);
}

/** 表单中可选的 provider 别名集：includeBare 含裸 provider 名（校验面）；
 *  否则只含 provider:model（下拉面——下拉只给具体模型，裸名兼容留给手写配置）。 */
export function providerAliases(form: FormState, opts?: { includeBare?: boolean }): string[] {
  const out: string[] = [];
  for (const row of form.providers) {
    const name = row.name.trim();
    if (!name) continue;
    if (opts?.includeBare) out.push(name);
    for (const m of parseModels(row.models)) out.push(`${name}:${m}`);
  }
  return out;
}

/** 后端文件视图 → 表单态（无自有配置时给一行空白起步）。 */
export function toFormState(file: ProvidersFile | null): FormState {
  if (!file) {
    return {
      providers: [{ name: "", base_url: "", api_key: "", models: "" }],
      default_provider: "",
      role_map: {},
      role_options: {},
    };
  }
  const providers = Object.entries(file.providers ?? {}).map(([name, p]) => ({
    name,
    base_url: p.base_url ?? "",
    api_key: p.api_key ?? "",
    models: (p.models ?? []).join(", "),
  }));
  const role_options: FormState["role_options"] = {};
  for (const [role, opt] of Object.entries(file.role_options ?? {})) {
    role_options[role] = {
      effort: opt.effort ?? "",
      timeout: opt.timeout != null ? String(opt.timeout) : "",
    };
  }
  return {
    providers,
    default_provider: file.default_provider ?? "",
    role_map: { ...(file.role_map ?? {}) },
    role_options,
  };
}

/** 表单态 → 保存 payload（结构与 llm-providers.json 一致；掩码 key 原样透传给后端继承）。 */
export function toPayload(form: FormState): Record<string, unknown> {
  const providers: Record<string, ProviderFileEntry> = {};
  for (const row of form.providers) {
    const name = row.name.trim();
    if (!name) continue;
    providers[name] = {
      base_url: row.base_url.trim(),
      api_key: row.api_key.trim(),
      models: parseModels(row.models),
    };
  }
  const role_map: Record<string, string> = {};
  for (const [role, target] of Object.entries(form.role_map)) {
    if (target) role_map[role] = target;
  }
  const role_options: Record<string, { effort?: string; timeout?: number }> = {};
  for (const [role, opt] of Object.entries(form.role_options)) {
    const o: { effort?: string; timeout?: number } = {};
    if (opt.effort) o.effort = opt.effort;
    if (opt.timeout.trim()) o.timeout = Number(opt.timeout);
    if (Object.keys(o).length > 0) role_options[role] = o;
  }
  return {
    providers,
    default_provider: form.default_provider || undefined,
    role_map,
    role_options,
  };
}

/** 保存前校验（错误文案必须可操作；语义终审在后端 from_config 试装）。 */
export function validateForm(form: FormState): string[] {
  const errors: string[] = [];
  const rows = form.providers.filter((r) => r.name.trim());
  if (rows.length === 0) {
    errors.push("至少保留一个 provider——要回到 pi/.env 兜底配置请用「恢复默认」");
    return errors;
  }
  const names = new Set<string>();
  for (const row of rows) {
    const name = row.name.trim();
    if (names.has(name)) errors.push(`provider 名重复：${name}`);
    names.add(name);
    if (!row.base_url.trim()) errors.push(`provider ${name}：base_url 必填`);
    if (!row.api_key.trim()) errors.push(`provider ${name}：api_key 必填（沿用已存请保持 ***）`);
    if (parseModels(row.models).length === 0)
      errors.push(`provider ${name}：models 至少一个（逗号分隔）`);
  }
  if (form.default_provider && !names.has(form.default_provider)) {
    errors.push(`default_provider ${form.default_provider} 不在 providers 列表中`);
  }
  const aliases = new Set(providerAliases(form, { includeBare: true }));
  for (const [role, target] of Object.entries(form.role_map)) {
    if (target && !aliases.has(target)) {
      errors.push(`role_map.${role} 指向未配置的 ${target}（须为 provider 名或 provider:model）`);
    }
  }
  for (const [role, opt] of Object.entries(form.role_options)) {
    if (opt.timeout.trim() && Number.isNaN(Number(opt.timeout))) {
      errors.push(`role_options.${role}.timeout 必须是数字（秒）`);
    }
  }
  return errors;
}

/** role_map 下拉的可选项：provider:model 别名（不含裸名——具体模型才可路由）。 */
export function roleTargetOptions(form: FormState): string[] {
  return providerAliases(form);
}

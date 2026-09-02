// P5 provider 自配页纯逻辑契约：表单态 ↔ 保存 payload、掩码继承透传、校验可操作。
import { describe, expect, it } from "vitest";
import {
  KEY_MASK,
  roleTargetOptions,
  toFormState,
  toPayload,
  validateForm,
  ProvidersFile,
} from "../providers";

const FILE: ProvidersFile = {
  providers: {
    dashscope: {
      base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
      api_key: KEY_MASK,
      models: ["kimi-k3", "GLM-5.3"],
    },
  },
  default_provider: "dashscope",
  role_map: { research: "dashscope:kimi-k3" },
  role_options: { research: { effort: "max", timeout: 300 } },
};

describe("toFormState / toPayload 往返", () => {
  it("文件视图 → 表单态：models 逗号串、role_options 字符串化", () => {
    const form = toFormState(FILE);
    expect(form.providers).toHaveLength(1);
    expect(form.providers[0]).toMatchObject({
      name: "dashscope",
      models: "kimi-k3, GLM-5.3",
      api_key: KEY_MASK,
    });
    expect(form.role_options.research).toEqual({ effort: "max", timeout: "300" });
  });

  it("往返无损：掩码 key 原样透传（后端继承语义）", () => {
    const payload = toPayload(toFormState(FILE)) as unknown as ProvidersFile;
    expect(payload.providers!.dashscope.api_key).toBe(KEY_MASK);
    expect(payload.providers!.dashscope.models).toEqual(["kimi-k3", "GLM-5.3"]);
    expect(payload.role_map).toEqual({ research: "dashscope:kimi-k3" });
    expect(payload.role_options).toEqual({ research: { effort: "max", timeout: 300 } });
    expect(payload.default_provider).toBe("dashscope");
  });

  it("空配置给一行空白起步", () => {
    const form = toFormState(null);
    expect(form.providers).toHaveLength(1);
    expect(validateForm(form)).toContain("至少保留一个 provider——要回到 pi/.env 兜底配置请用「恢复默认」");
  });

  it("空 effort/timeout 不进 payload", () => {
    const form = toFormState(FILE);
    form.role_options.fast = { effort: "", timeout: "" };
    const payload = toPayload(form) as unknown as ProvidersFile;
    expect(payload.role_options).not.toHaveProperty("fast");
  });
});

describe("validateForm", () => {
  it("缺 base_url / 重复名 / role_map 指空 都拦截", () => {
    const form = toFormState(FILE);
    form.providers[0].base_url = " ";
    form.providers.push({ ...form.providers[0] }); // 同名重复
    form.role_map["fast"] = "dashscope:no-such-model";
    const errors = validateForm(form);
    expect(errors.some((e) => e.includes("base_url"))).toBe(true);
    expect(errors.some((e) => e.includes("重复"))).toBe(true);
    expect(errors.some((e) => e.includes("no-such-model"))).toBe(true);
  });

  it("timeout 非数字拦截；合法表单零错误", () => {
    const form = toFormState(FILE);
    form.role_options.research = { effort: "max", timeout: "abc" };
    expect(validateForm(form).some((e) => e.includes("数字"))).toBe(true);
    expect(validateForm(toFormState(FILE))).toEqual([]);
  });
});

describe("roleTargetOptions", () => {
  it("每个 provider 的每个 model 出 provider:model 别名", () => {
    expect(roleTargetOptions(toFormState(FILE))).toEqual([
      "dashscope:kimi-k3",
      "dashscope:GLM-5.3",
    ]);
  });
});

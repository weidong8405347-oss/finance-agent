// 离线导出数据层（§11.4）：installOfflineData 后，读方法短路到内嵌冻结数据
// （快照/模块/证据/产物），服务器写/算操作一律 503——导出的自包含 HTML 与
// 在线页面跑同一套组件，数据层在此分流，不得静默发网络请求。
import { afterEach, describe, expect, it } from "vitest";

import {
  ApiError, dossierApi, installOfflineData, isOfflineExport, type DossierExportData,
} from "../api";

const FAKE: DossierExportData = {
  kind: "dossier-html-export",
  format_version: 1,
  exported_at: "2025-06-01T00:00:00Z",
  snapshot: {
    entity: { kind: "stock", id: "BE", name: "Bloom Energy" },
    context: {
      snapshot_id: "dossier-test-1", as_of: "2025-06-01T00:00:00Z",
      generated_at: "2025-06-01T00:00:01Z", mode: "live", namespace: "prod",
    },
  } as any,
  modules: {
    investment_snapshot: { module: "investment_snapshot", status: "ok" } as any,
  },
  evidence: {
    "ev-1": { evidence_id: "ev-1", verbatim_quote: "revenue 1500 million" } as any,
  },
  artifacts: {
    "art-1": { artifact_id: "art-1", title: "BE 研究报告" } as any,
  },
};

afterEach(() => installOfflineData(null));

describe("离线导出数据层", () => {
  it("安装后进入离线模式，读方法命中内嵌数据", async () => {
    expect(isOfflineExport()).toBe(false);
    installOfflineData(FAKE);
    expect(isOfflineExport()).toBe(true);

    await expect(dossierApi.snapshot("dossier-test-1")).resolves.toBe(FAKE.snapshot);
    await expect(dossierApi.openDossier("stock", "BE")).resolves.toBe(FAKE.snapshot);
    await expect(dossierApi.module("dossier-test-1", "investment_snapshot"))
      .resolves.toBe(FAKE.modules.investment_snapshot);
    await expect(dossierApi.evidence("dossier-test-1", "ev-1"))
      .resolves.toBe(FAKE.evidence["ev-1"]);
    await expect(dossierApi.artifact("art-1")).resolves.toBe(FAKE.artifacts["art-1"]);
  });

  it("离线不包含的对象 → 404（不伪装成功、不发网络请求）", async () => {
    installOfflineData(FAKE);
    await expect(dossierApi.snapshot("dossier-other")).rejects.toMatchObject({ status: 404 });
    await expect(dossierApi.module("dossier-test-1", "peers")).rejects.toMatchObject({ status: 404 });
    await expect(dossierApi.evidence("dossier-test-1", "ev-nope")).rejects.toMatchObject({ status: 404 });
    await expect(dossierApi.artifact("art-nope")).rejects.toMatchObject({ status: 404 });
    await expect(dossierApi.openDossier("stock", "PLUG")).rejects.toMatchObject({ status: 404 });
  });

  it("服务器写/算操作离线一律 503", async () => {
    installOfflineData(FAKE);
    await expect(dossierApi.requestResearch({ entity_id: "BE" }))
      .rejects.toMatchObject({ status: 503 });
    await expect(dossierApi.valuationPreview({
      entity_id: "BE", formula_id: "reverse_dcf", inputs: [], assumptions: {},
    })).rejects.toMatchObject({ status: 503 });
    await expect(dossierApi.saveScenario({
      base_snapshot: "dossier-test-1", assumption_hash: "h", validated_calculation_id: "c",
    })).rejects.toMatchObject({ status: 503 });
    await expect(dossierApi.exportSnapshot("dossier-test-1", "html"))
      .rejects.toMatchObject({ status: 503 });
    await expect(dossierApi.entities()).rejects.toMatchObject({ status: 503 });
    await expect(dossierApi.compare({ entities: "stock:BE", metric: "revenue" }))
      .rejects.toMatchObject({ status: 503 });
    const err = await dossierApi.requestResearch({ entity_id: "BE" })
      .catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).message).toContain("离线导出");
  });

  it("参数化模块读取离线不可用（内嵌数据按无参数收集）", async () => {
    installOfflineData(FAKE);
    await expect(dossierApi.module("dossier-test-1", "key_kpi", { frequency: "FY" }))
      .rejects.toMatchObject({ status: 503 });
  });

  it("changes 返回空 diff（冻结文件没有「其他版本」可比，页面不触发变更提示）", async () => {
    installOfflineData(FAKE);
    const diff = await dossierApi.changes("dossier-test-1", "dossier-test-1");
    expect(diff.changed_modules).toEqual([]);
    expect(diff.current.snapshot_id).toBe("dossier-test-1");
  });

  it("清空后恢复在线模式", async () => {
    installOfflineData(FAKE);
    installOfflineData(null);
    expect(isOfflineExport()).toBe(false);
  });
});

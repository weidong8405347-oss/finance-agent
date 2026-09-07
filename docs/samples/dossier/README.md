# 冻结 Dossier 样本（M0 交付件，附录 A.1）

由 `scripts/migrate_dossier.py shadow` 从生产知识库（27 实体）投影生成的三个对照样本，
生成于 2026-09-07（projector_version=1，schema_version=1.0）：

| 文件 | 对照目的 |
| --- | --- |
| `dossier-stock-BE.json` | 真实股票（工业设备配方识别命中 `fuel cell` 提示）：旧文本字段 → 模块 partial + needs_normalization；typed 指标缺口如实显示 |
| `dossier-stock-688507SH-sparse.json` | 缺数据实体：绝大多数模块 missing + 降级原因，验证「不以空图宣称完成」 |
| `dossier-industry-ai-for-science.json` | 行业实体：industry 配方、产业链/候选池 legacy 字段投影 |

样本用于契约与布局验收（单位、期间、来源、缺失、冲突、as_of 的表达方式）。
其中数字与摘录来自当时的研究投影，**不是投资建议**；进入生产阅读前以实时
`/api/v2` 投影为准。每个样本的 `data_hash` 记录了投影输入版本集，可对照
`data/migration/shadow-*/manifest.json` 的来源 fact_id 清单做对账。

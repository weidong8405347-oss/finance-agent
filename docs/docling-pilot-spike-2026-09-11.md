# Docling 试点记录（spike，2026-09-11）

> 方案 §5.1 实现顺序 5 的对照试点：「Docling 作为结构解析首选试点；现有 pypdf
> 做轻量路径；只对低质量页做 OCR。用同一批中文扫描、跨页表和英文年报对照后再
> 决定生产组合。」本记录是 Docling 半边的证据；Unstructured 半边未跑（证据对
> 目标失败类已具决定性，双装的价值留给需要时再证）。

## 样本（全部来自真实研究运行的证据台账，权威来源 URL）

| 样本 | URL | 目标失败类 |
| --- | --- | --- |
| 2228.HK 中报（pypdf 判定 garbled） | hkexnews 2026/0819/2026081900920_c.pdf | 中文乱码 PDF 的 OCR 修复 |
| 3988.HK 2025 年报 pp.315–320（分部报告附注） | hkexnews 2026/0330/2026033000533_c.pdf | 巨型年报后半部跨页表结构还原 |
| 2228.HK 公告（文本型对照组） | hkexnews 2026/0325/2026032501035.pdf | 正常文本页两路径应一致（不退化） |

运行：`uv run python scripts/pilot_docling.py`（docling 2.126.0，CPU，
RapidOCR PP-OCRv6 中文模型；pypdf 为现网轻量路径）。
原始数据：`data/pilot/docling/pilot-report.json`。

## 结果

| 样本 | pypdf（现状） | Docling+OCR | 判定 |
| --- | --- | --- | --- |
| 2228 中报 | garbled，2339 字符乱码（mojibake 不可读） | **ok**，5028 字符干净繁体中文 + 1 表 | OCR 决定性修复 |
| 3988 年报 pp.315–320 | partial，6570 字符，无表结构 | **ok**，20189 字符 + **4 个表**（10×16/13×17/9×13/11×14，单元格 121/129/99/109） | 表结构决定性还原 |
| 2228 公告（对照） | ok，8059 字符 | ok，8087 字符，内容一致 | 无退化 |

耗时（CPU）：pypdf 亚秒；Docling 每样本 5.2–43.8s（OCR 页是大头）。
生产含义：**Docling 只跑低质量页与表格页**（质量闸路由），全量年报不整本走 OCR。

## 结论（试点决策建议，待架构确认后落地）

1. **接入 Docling 作为 OCR/表结构路径**：documents 层加 `parser_mode`——
   pypdf 为默认轻量路径；`quality in (garbled, needs_ocr)` 的页或 extract_table
   命中候选时走 Docling（OCR + 表结构）；解析器版本进文档元数据（方案 §4.1
   DocumentVersion.parser_version）。
2. **依赖策略**：docling 进 `[project.optional-dependencies] data`（或单独
   `parser` extra），缺装时路径退化为现状并显式 degraded（能力页可见）。
3. Unstructured 对照半边暂不跑：目标失败类（中文乱码/跨页表）已有决定性证据；
   若 Docling 生产化出现新问题类再补对照。
4. 单元格定位与 MetricSpec/数值准入的对接走 extract_table 契约扩展
   （Docling 表格替代启发式候选为更高保真来源，校验纪律不变）。

## 风险与边界

- CPU 上 OCR 慢（单页秒级），必须按页路由而非整本；
- 模型权重首次下载需外网（HF/RapidOCR 模型仓）——生产部署要预置 artifacts；
- OCR 结果仍需逐字证据绑定纪律（OCR 文本是解析产物，引用照常走子串校验）。

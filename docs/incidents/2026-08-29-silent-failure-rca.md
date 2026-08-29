# 归因报告：静默失败事故（2026-08-29）

> 触发：用户一键启动后点「研究」，UI 无反应，服务器日志全是 200 OK，唯一的错误记录埋在事件库里看不到也复制不了。
> 定性：**可观测性基础设施整体缺失**导致的静默失败。不是个例 bug，是体系缺口。

---

## 1. 事故经过（证据）

用户操作日志的取证事实：

```
POST /api/research → 200 OK（实际研究从未运行）
GET /api/approvals/pending × N（全部 200，空列表——审批从未触发）
```

服务器日志**没有任何错误行**。唯一的错误痕迹是事件库里的 `research/error`（"provider 未配置"），它在日志和 UI 中都不可见。

## 2. 直接原因链（逐环取证）

| # | 环节 | 证据 | 问题 |
| --- | --- | --- | --- |
| 1 | 未配置 provider → `LLMRouter.from_env()` 为空 | `llm/router.py` | 正常（设计行为） |
| 2 | `ProviderConfigError` 在**后台线程**被捕获，写事件后 `return` | `cli.py` research_runner | **静默吞掉：无日志、无 stderr** |
| 3 | `POST /api/research` 无条件返回 `200 {status: started}` | `api/app.py:155` | **无前置校验**：失败在响应之后才发现 |
| 4 | 服务器日志只有 uvicorn 访问日志 | 取证：`grep -rn "import logging" src/` = **0 处** | **项目没有 logging 基础设施** |
| 5 | UI 的 `research/error` 事件：无样式映射（默认白卡）、payload 折叠、整个卡片是 `<button>`（浏览器里按钮文字不可选） | `SessionsPage.tsx` EventCard | 错误在 UI 里**看不到、复制不了** |

每一环单独看都"差不多能忍"，串起来就是用户体验的灾难：**错误发生了，但没有任何一个通道告诉用户**。

## 3. 系统性根因（为什么这类问题能活着出厂）

### R1 · 可观测性是零，不是弱

- 全项目 `import logging` **0 处**。错误处理范式是"写事件库然后吞掉"——事件库是数据真相源，但**运维通道（stderr/日志文件）从未建立**。
- V1.0 有 `src/research/logging` 模块，我在 P0 技术选型时写了"沿用"却从未移植。
- 后果：fail-closed 原则只做到了"数据不出错"，没做到"操作员知情"。**事件库 ≠ 日志；记录 ≠ 可见。**

### R2 · 测试体系的结构缺口：测了"功能正确"，没测"失败对人可见"

| 缺口 | 事实 | 后果 |
| --- | --- | --- |
| 真实装配路径零测试 | `cli.py` 无任何测试（grep 命中的都是 TestClient 的误匹配）；所有 API 测试注入假 runner | 真实 runner 的组装错误只能在用户手上炸 |
| 前置失败条件无 API 层测试 | "无 provider" 从未在 POST /api/research 路径上被测 | 200-then-silent-fail 出厂 |
| "失败可见性"没有断言 | 没有任何测试检查"错误发生时，日志/状态/UI 至少一个通道告知用户" | 静默失败不被测试视为失败 |
| 前端零测试 | copyability、错误样式从未被检查 | 按钮包文字、错误无样式 |
| 无冷启动 E2E | "用户打开应用→操作→看到结果或错误"的完整旅程从未跑过 | 集成缝隙全漏 |

### R3 · 设计文档写了，实现时被绕过

DESIGN.md §4.1 要求 StepReport 可读记录；§4.4 要求 UI 投影完整。**"错误必须在投影里有一等位置"从未被显式化**，于是在工期压力下被静默放弃。教训：没有断言保护的设计意图 = 没有设计意图。

## 4. 测试体系整改方案（对齐稿）

### 两条永久规矩（写进工程约定，CI 强制）

**规矩 1 · 失败可见性三通道不变量**
任何 fail-closed / 错误路径的测试，必须同时断言三个通道：
1. **事件**：错误事件落 EventStore（可回放）
2. **日志**：stderr/日志文件有对应行（运维可见）
3. **用户可见状态**：API 状态投影 / UI 能展示（用户知情 + 可复制）

缺一个通道 = 测试失败。错误处理代码不允许"静默 return"。

**规矩 2 · 真实装配测试**
每条用户可达的命令路径（`serve`、`research`、每个 POST 端点）至少一个测试用**真实装配**（不注入假 runner/fake 服务）；依赖注入只允许在系统边界（LLM provider、网络数据源）。

### 测试层整改（在现有 unit/integration 之上补齐）

| 层 | 内容 | 验收断言示例 |
| --- | --- | --- |
| L0 logging 基底 | 结构化日志模块（INFO→stderr，ERROR 必现）；error 类事件自动镜像到日志（EventStore 挂 subscriber） | 触发 research/error → caplog 里有对应 ERROR 行 |
| L1 失败可见性 | 无 provider / provider 中途挂掉 / 审批超时 / 数据源失败 / 网关 fail-closed | 每条路径三通道断言齐全 |
| L2 CLI 测试 | `serve` 启动冒烟、`research --mock` 全跑通、缺配置时快速失败且信息可操作 | subprocess 起服务 → 200 → 关断 |
| L3 冷启动 E2E（TestClient 级全旅程） | 新库 → 发研究 → 失败 → 状态可见 → 错误文案含修复指引（"请在 .env 配置…"） | 全程无 mock 装配 |
| L4 前端契约 | 错误卡片样式存在、payload 可复制（不是 button 包文本）、会话状态徽章 | vitest 组件测试或契约测试 |

### 顺带要修的产品缺陷（整改完成后再动手）

1. POST /api/research 加 preflight（无 provider → 422 + 可操作指引）——测试已在 `tests/unit/test_api_failure_ux.py`（红态待实现）
2. 会话状态投影（running/done/error/cancelled + status_detail）
3. EventCard 重构：错误事件一等样式 + 文本可复制
4. logging 基底 + 事件镜像
5. `research/completed` 完成事件（现在连"跑完了"都没有信号）

## 5. 当前工作区状态（诚实交代）

- `tests/unit/test_api_failure_ux.py`：上一轮 TDD 红态遗留，未提交，当前失败（引用了尚未实现的 `research_preflight` 参数）——正好作为整改的起点用例
- 其余 109 tests 全绿

## 6. 待对齐决策点

| # | 决策点 | 我的建议 |
| --- | --- | --- |
| A | 两条永久规矩（三通道 / 真实装配） | 采纳，写进 README 工程约定 + 评审 checklist |
| B | 整改范围 | L0-L3 本轮全做；L4 前端做契约级（vitest 组件测试基础设施要新增，工作量单列） |
| C | logging 方案 | Python 标准 logging + JSON formatter（自用单机足够），事件镜像用 EventStore subscriber |
| D | 验收方式 | 整改后由你按 L3 旅程手工验收一次（故意不配 provider / 故意断网各跑一次） |

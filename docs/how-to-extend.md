# 加能力配方（Q1 定稿：代码即配置）

> 本项目的扩展点全部在代码层（显式、类型安全、可测）。
> 三条铁律不变：① 数据采集必须走 DataGateway（PIT 分级）；② 证据只能登记自实际检索内容
> （chunk 子串校验）；③ 落库必经 ProfileWriter（单写者）。新增能力不得绕过这三条。

---

## 配方 1：加数据源（如新闻、公告、宏观）

1. `src/finance_agent/gateway/adapters/<name>.py`：实现 `SourceAdapter` 协议
   ```python
   class MyAdapter:
       def capability(self) -> SourceCapability:
           return SourceCapability(
               source_id="my_source",
               pit_grade=PitGrade.A,      # A=精确可知时刻 / B=可编辑快照 / C=无 PIT 保证
               server_side_asof=True,     # 能否源头过滤 as_of
               description="…",
           )
       def query(self, request: dict, as_of: datetime | None = None) -> list[DataRecord]:
           # 每条记录必须带 available_at；给不出 → 只能标 C（评估模式自动禁用）
   ```
2. `gateway/tools.py` 的 `GATEWAY_TOOL_SCHEMAS` 加 `query_my_source` schema。
3. `cli.py build_orchestrator()` 注册：`gateway.register(MyAdapter())`。
4. `commands/steps.py` 的 `STEP_MANIFEST` 里把新工具加进相关 step 的 tools（能力页可见）。
5. 测试：仿 `test_stooq_adapter`——monkeypatch httpx，验 CSV/JSON 解析 + as_of 过滤 + PIT 等级。
   **不要**在测试里打真实网络。

## 配方 2：加工具（挂在某个 step agent 或主 agent 上）

1. 在对应模块写工具函数：`fn(args: dict) -> {"content": str, "provenance": [...]}`；
   结果必须带 provenance（leakage-audit 的审计锚点）。
2. 挂进 step 的 tools dict（如 `commands/steps.py` 各 step）或主 agent `main_agent.py _tools()`；
   schema 加进对应 `*_TOOL_SCHEMAS`（真实 provider 的 function calling 需要）。
3. `STEP_MANIFEST` / 能力页同步登记。
4. 测试：MockLLM 脚本调用该工具 → 断言事件与产出。

## 配方 3：加 step agent / command

1. `commands/steps.py`：写 `step_xxx(deps, ctx) -> StepResult`（自己的 kernel/loop、独立子 run）；
   登记进 `STEPS` / `STEP_TITLES` / `STEP_MANIFEST`。
2. `commands/registry.py`：把 step 编进某个 command 的 steps，或新建 CommandSpec。
3. 测试：仿 `test_decide_pipeline_runs_all_steps_in_order` 断言事件序。

## 配方 4：MCP（后置——有真实需求时）

写一个 `McpBridgeAdapter`（SourceAdapter 实现）：把 MCP server 的工具调用包成
`query()`，**强制人工声明 PIT 等级**（大多数 MCP 工具无 PIT 保证 → 只能标 C，
评估模式自动 fail-closed）。没有 PIT 语义审查的 MCP 不得进入评估路径。

## 配方 5：前端加一类对话节点

1. 事件生产者落新事件类型（`eventstore/events.py` 登记常量，模型可见性默认否）。
2. `frontend/src/lib/assemble.ts`：加节点类型 + match/update 逻辑；
   **先写 `src/lib/__tests__/assemble.test.ts` 的装配用例**（vitest）。
3. `frontend/src/components/nodes.tsx`：加渲染器（卡片/折叠，参考 ToolCard/ReportFoldCard）。

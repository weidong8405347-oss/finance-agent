# 会话与知识档案清理手册

> 2026-09-09 · 分支 `fix/research-audit-a2cce641-remediation`
> 覆盖三件用户实际遇到的问题：① 在跑的会话显示失败；② Sessions 里一堆「空闲、点开
> 什么都没有」的 `migration-shadow`/`dossier`；③ 需要能删掉历史低质量的会话与档案。
> 所有删除都留审计记录；实体删除默认**可恢复**。

## 1. 会话状态口径（为什么在跑却显示失败）

旧实现把 `error` 做成 sticky：上一条命令 `outcome=blocked`（研究停滞、管道拦停）之后，
`status` 永久钉在 error，`if status != "error"` 才判 running —— 新命令开跑也翻不回来。
`live-a2cce641` 就是这个形态：`cmd-7a61bfa5` blocked，`cmd-9e8c41ea` 正在跑，页面显示失败。

现在的口径（`api/app.py::_session_summary`）：

| 优先级 | status | 触发条件 |
| --- | --- | --- |
| 1 | `running` | 有未闭合的 command 或 turn，**且**没有更晚的硬错误 |
| 2 | `error` | `outcome=error` 或 `research/decision/turn error` 事件 |
| 3 | `blocked` | `outcome=blocked`（研究停滞拦停）——**不再冒充失败** |
| 4 | `cancelled` | 用户停止或拒绝审批 |
| 5 | `done` | 有 `outcome=completed` |
| 6 | `idle` | 什么都没有 |

两个口径分开返回，UI 不再互相污染：

- `status` / `running_commands` / `open_turns`：当前活动状态；
- `last_outcome` / `last_blocked` / `last_error` / `status_detail`：上一条命令的结果与原因；
- `possibly_stale`：`running` 但超过 30 分钟无事件 → 进程可能被杀，诚实标「可能已中断」，
  不假装活着。

真实库复核：`live-a2cce641` → `running`（上一条 = completed）；三个 9-02 的僵尸会话 →
`running（可能已中断）`。

## 2. 什么才算「会话」（幽灵会话从哪来）

`/api/sessions` 过去是「按 run_id 分组 events 表」，于是任何往事件库写过东西的 run 都成了
会话。两类幽灵：

| 形态 | 例子 | 产生原因 |
| --- | --- | --- |
| 投影/维护 run | `migration-shadow`（27 条 `dossier/published`）、`dossier`（快照发布）、`kb-*`（人工裁决审计）、`eval-*` | `scripts/migrate_dossier.py shadow` 用 `run_id=migration-shadow` 批量发布快照；`DossierService.open` 在无命令上下文时默认 `run_id=dossier`。它们只有投影副作用事件，没有对话/命令内容 → 点开必然空白 |
| 漏落 `run/created` 的子 run | `live-2be18efd--cmd-5492192f-6-committee--ABCL-cio` 等 6 个 | 委员会 CIO 综合 run 没写 `run/created`（同函数的各视角 run 写了），`child_run_ids()` 认不出它是子 run |

修复：

- `EventStore.is_session_run()`：系统前缀（`kb-`/`migration-`/`dossier`/`eval-`/`system-`/
  `live-gateway`）+ 「全部事件都是投影副作用」（`PROJECTION_ONLY_TYPES`）→ 不是会话；
- `EventStore.child_run_ids()`：`run/created.parent_run_id` **或** run_id 含嵌套分隔符 `--`
  （命名约定 `<会话>--<step>`，兜住历史上漏落 `run/created` 的子 run）；
- 根因修复：委员会 CIO run 补落 `run/created`（带 parent_run_id）。

真实库复核：411 个 run 中，会话数 34 → **28**（6 个 CIO 幽灵归位为子 run，
`migration-shadow`/`dossier`/19 个 `kb-*` 不再进列表）。

## 3. 删除能力

### 3.1 两级删除（知识实体）

| 模式 | 做什么 | 可恢复 | 何时用 |
| --- | --- | --- | --- |
| `tombstone`（默认） | 写实体墓碑 + `knowledge/purged` 审计事件；**所有读路径过滤**（v1/v2 列表、实体详情 410、档案页 410）；事实/观测行保留 | ✅ `restore` | 想让页面立刻干净，但保留审计与后悔药 |
| `hard` | 墓碑 + 真删 `kb.facts`、metrics 九表（观测/计算/计划/论断/产物/快照/裁决/修订）、`decisions` 行；清「两个库都不再引用」的**孤儿证据**；删磁盘存档 | ❌ | 确定是垃圾，要回收体积 |

设计约束（为什么不是一键 wipe）：

- 事件日志是唯一真相源，**保留**（含 `knowledge/purged` 审计）——重放不会静默复活已删实体，
  删除本身可追责；
- 证据是全局表（无实体列），只删两个库都不再引用的孤儿，**共用证据不动**
  （冒烟实测：删 `industry:smoke-bad` 清掉 1 条孤儿、保留被 `stock:GOOD` 引用的 1 条）；
- `data/reports/` 按 child_run_id 分目录、不带实体标识 → 实体删除**不猜归属**，
  只删目录名显式含实体 id 的；要清报告请删对应会话；
- 回执逐项可核对：`counts`（逐表行数）/`orphan_evidence_deleted`/`evidence_remaining`/
  `files_removed`/`restorable`/`warnings`（未给原因会警告）。

### 3.2 会话删除

`DELETE /api/sessions/{run_id}?force=&reason=`：级联删全部子 run 事件 + 报告目录。
**正在跑的默认拒删（409）**：先 `POST /api/sessions/{run_id}/stop`，或显式 `force=true`。
删除落 `session/deleted` 审计到 `system-purge` run。
幽灵/系统 run 不允许按会话删（422）——投影审计事件不该被当会话清掉。

### 3.3 三个入口

**页面**

- Sessions 侧栏每行 `✕`：二次确认 → 运行中会先停再删 → 回执/失败原因显示在侧栏底部；
- Knowledge 列表每行「删除」（墓碑）/「彻底删」（hard，二次确认 + 原因输入）；
  顶部「已删除」抽屉列出墓碑（模式/时间/原因）并给「恢复」按钮。

**HTTP**

```bash
curl -X DELETE "localhost:8000/api/sessions/live-xxxx?reason=低质量历史"
curl -X DELETE "localhost:8000/api/knowledge/industry/ai-for-science-美股港股?reason=旧口径重复档案"
curl -X DELETE "localhost:8000/api/knowledge/stock/LODE?mode=hard&reason=垃圾内容"
curl -X POST   "localhost:8000/api/knowledge/stock/LODE/restore?reason=误删"
curl "localhost:8000/api/knowledge/purged"
curl "localhost:8000/api/knowledge/entities?include_purged=true"
```

注意：`reason` 含中文时必须百分号编码（浏览器/前端已用 `encodeURIComponent`；
裸 curl 传 UTF-8 会被 h11 判为 `Invalid HTTP request`）。

**CLI（干跑默认，`--apply` 才落库）**

```bash
uv run python scripts/purge.py --data-dir data list-sessions            # 标注 session/child/system-ghost
uv run python scripts/purge.py --data-dir data list-entities --sort-by rows
uv run python scripts/purge.py --data-dir data session --run-id live-b1aa25c9            # 干跑
uv run python scripts/purge.py --data-dir data session --run-id live-b1aa25c9 --apply
uv run python scripts/purge.py --data-dir data entity --entity stock:LODE --reason "低质量" --apply
uv run python scripts/purge.py --data-dir data entity --batch purge-list.txt --mode hard --reason "批量清理" --apply
uv run python scripts/purge.py --data-dir data restore --entity stock:LODE --apply
```

## 4. 当前库的清理候选（2026-09-09 只读盘点，未改任何数据）

**僵尸会话（`running` 但可能已中断，9-02 至今无事件）**

```
live-b1aa25c9   open=cmd-ce14044c  events=15  last=2026-09-02T04:34
live-97322d15   open=cmd-66211e20  events=27  last=2026-09-02T02:29
live-bd05c145   open=cmd-91cfd7dc  events=26  last=2026-09-02T02:04
```

删除需 `--force`（它们的 command 永远不会有 `command/done`）：

```bash
for r in live-b1aa25c9 live-97322d15 live-bd05c145; do
  uv run python scripts/purge.py --data-dir data session --run-id $r --force \
      --reason "僵尸会话：进程被杀，command 未闭合" --apply
done
```

**质量分最低的档案（27 个实体中的 14 个；全部 obs=0 claims=0，即只有旧文本字段、
没有 typed 观测与论断）**

```
0.12 stock:688507.SH   facts=10     0.54 stock:300857.SZ  facts=19
0.44 stock:BE          facts=5      0.55 stock:002837.SZ  facts=7
0.44 stock:BNTX        facts=5      0.57 stock:ABSI/LODE/SES
0.50 stock:2315.HK     facts=6      0.72 stock:1548.HK    facts=7
0.50 stock:688222.SH   facts=6      0.75 stock:688041.SH/GENB/TWST
```

建议先用墓碑模式（可恢复），确认页面与下游都干净后再决定是否 hard：

```bash
printf '%s\n' "stock:688507.SH" "stock:2315.HK" "stock:688222.SH" > purge-list.txt
uv run python scripts/purge.py --data-dir data entity --batch purge-list.txt \
    --reason "只有旧文本字段、质量分低、无 typed 观测" --apply
```

**不要删** `industry:ai-for-science`（71 facts / 133 行，是本次审计与整改的现场基线，
`live-a2cce641` 正在写它）；`ai-for-science-美股港股` 是旧口径重复档案，可删。

## 5. 重启须知

服务端进程仍在跑**旧代码**（`ps` 里 3 个 `finance_agent serve`）。改动要生效必须重启，
但重启会打断 `live-a2cce641` 正在跑的 `cmd-3975ce29`：

- 想保住这次运行 → 等它出 `command/done` 再重启；
- 不在意 → 直接重启；已落库的成果不会丢，且新的部分发布路径（`research/partial_published`
  检查点 + partial 产物）会在下一次运行的终止路径上生效。

```bash
pkill -f "finance_agent serve" ; uv run python -m finance_agent serve   # 会自动构建前端
```

## 6. 验证

```bash
uv run pytest tests/unit/test_purge_and_session_status.py   # 19 例
uv run pytest tests                                          # 651 passed, 9 skipped
cd frontend && npm test && npm run build                     # 40 passed；构建通过
```

覆盖：运行中不被旧 blocked 染成失败、blocked≠error、进程被杀判 error 不永远 running、
stale 标记、四类幽灵 run 不进列表、子 run 不单独成会话；会话删除级联子 run + 报告目录、
运行中拒删/force 可删、幽灵 run 不许按会话删；实体墓碑对 v1/v2 列表 + 详情 + 档案页全路径
生效且行保留、恢复可用、hard 真删行 + 孤儿证据（共用证据保留）+ 存档且不可恢复、
hard 不影响其他实体。另有一次真实 uvicorn 端到端冒烟（墓碑 → 410 → 恢复 → hard → 409）。

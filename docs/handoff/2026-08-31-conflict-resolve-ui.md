# Handoff：冲突人工裁决 UI + gitignore 误伤源码事故修复（2026-08-31 晚）

> 给新会话的唯一入口文档（取代 [2026-08-31 行情源版](2026-08-31-prices-eval-first-run.md)，
> 历史见 git log：`1532936 feat(kb)` → `a5e8cf4 fix(repo)` → `edd0c33 style(knowledge)`——本次三个提交）。
> 设计文档不变：`docs/redesign-interaction-orchestration.md`（§5 决策记录）、
> `DESIGN.md`、`docs/evaluation-design.md`、`docs/how-to-extend.md`。
> 工程铁律见 [2026-08-30 收官版](2026-08-30-current-state.md) §2（七条，未变）。

---

## 0. 一句话现状

backlog #2 完成：**档案详情页冲突裁决闭环**（「以此版本为准」→ 单写者 append-only 补写/清标记
→ `fact/conflict_resolved` 可审计 → 详情页投影反映），对话流新增冲突节点卡；
TDD 全程（3 pytest + 4 vitest 先红后绿）。**过程中抓出并修复一个事故级 bug：
`src/finance_agent/knowledge/` 整个模块从未进 git**（.gitignore 裸目录名误伤）。
pytest **176 passed + 3 skipped** + vitest **9** + ruff 全绿 + fresh-clone 自洽验证通过。

## 1. 事故：.gitignore 误伤源码模块（本次最重要发现）

- `.gitignore` 里 `knowledge/`（R3 时为忽略运行期 HTML 存档目录而加）**无前导斜杠**，
  匹配任意深度 → `src/finance_agent/knowledge/`（双时态存储/单写者/证据绑定，969 行）
  自 R1 起**从未被 git 追踪**。fresh clone 无法运行，但本机磁盘上有文件所以一直没暴露。
- 连带假象：**ruff 默认尊重 .gitignore**，所以历届「ruff 全绿」也没覆盖过该模块
  （修复后暴露 12 处欠账，已清：E501×9 / UP035 / UP037×2 / B905）。
- 暴露路径：本会话编辑 `writer.py` 后 `git status` 不显示、`git show HEAD:<file>` 不存在。
- 修复：`knowledge/` → `/knowledge/`（锚定根；运行期目录实际由 `data/` 覆盖）+ 补齐 10 个源文件。
- **教训**：gitignore 加裸目录名前先 `git check-ignore -v` 验证；「工具全绿」的前提是工具看得见文件。

## 2. 冲突人工裁决（backlog #2，TDD）

**链路**：详情页冲突行 → 裁决面板 → `POST /api/knowledge/{kind}/{id}/resolve` →
`ProfileWriter`（铁律 3 单写者）→ 版本链落事件 → 详情页重载反映。

1. **API**（`api/app.py`，先在 `test_api.py` 写失败测试）：
   - `POST /api/knowledge/{kind}/{entity_id}/resolve` `{field, keep_fact_id, note?}`；
   - keep 非最新版本 → 经 writer **补写同值新版本**（append-only 不破，证据/事件时点沿用被裁决版本，
     numeric-guard 等硬门禁照跑），随后清除该字段全部竞争标记；
   - keep 最新版本 → 仅清标记（`cleared=1`）；
   - 审计事件落 **`kb-<kind>-<entity_id>` 维护 run**（`fact/asserted` + `fact/conflict_raised` +
     `fact/conflict_resolved`——补写引发的标记也诚实记录，随即清除）；sessions 列表排除 `kb-*`；
   - **仅 prod**：不接受 namespace 参数（eval 命名空间裁决权属回放纪律，铁律 6）。
2. **投影**：`entity_profile` / `fact_series` 每条带 `fact_id`（裁决锚点）；
   `fact/conflict_raised` 事件补 `entity` 字段。
3. **前端**（照 how-to-extend 配方 5，先 assemble.ts + vitest）：
   - `assemble.ts` 新增 `conflict` 节点：`fact/conflict_raised` → raised，`fact/conflict_resolved`
     → 同实体同字段节点迁移 resolved（裁决后再冲突回到 raised）；vitest 4 例；
   - `nodes.tsx` ConflictNode：raised 琥珀卡（「去裁决 →」跳 Knowledge 页）/ resolved 绿卡（note + 清除数）；
   - `KnowledgePage` 详情页：冲突行下挂裁决面板——「以此版本为准」（keep 当前投影值）+
     「查看版本链」（series 逐版本裁决，含 event_time/knowledge_time/竞争标记），成功后重载投影。
4. 真实装配冒烟（`build_orchestrator` → `create_app`）全流程通过：冲突投影 → keep v1=100 →
   补写 v3 → 投影回 100 / conflict=False / 实体列表冲突计数归零 / sessions 无污染。

## 3. 运行与验证（基线更新）

```bash
uv run pytest                        # 176 passed + 3 skipped（网络冒烟默认跳过）
uv run ruff check src tests          # 全绿（注意：docs/demo/gen_demo_archives.py 有 19 个预存 E501，
                                     #   历史 demo 脚本，与 src/tests 无关，未动）
cd frontend && npx vitest run        # 9 passed
uv run python -m finance_agent serve # 自动构建前端 + 开浏览器（:8000）
```

- fresh clone 自洽已验证：`uv sync --extra data --extra dev` + pytest 176+3s + ruff + vitest 9 + build 全过
  （缺 `--extra data` 时 pandas/yfinance 相关 2 例转 skip，属环境差异）。
- 真实行情/评估运行的代理注意事项不变（见 08-31 行情源版 handoff §1：`HTTPS_PROXY=http://127.0.0.1:7897`）。

## 4. 铁律自查（本次改动对照）

- 铁律 1：`fact/conflict_resolved` 不进 `MODEL_VISIBLE_TYPES` 白名单 ✓（未动白名单）；
- 铁律 3：裁决补写走 ProfileWriter 单写者 + append-only（keep 旧版=新版本而非改历史）✓；
- 铁律 5：serve 与测试共用同一装配；测试全离线 ✓；
- 铁律 7：新测试显式 tmp_path，不吃默认路径 ✓。

## 5. Backlog（更新）

1. ~~行情源解锁 + /evaluate 真实首跑~~ ✅（08-31 行情源版）。
2. ~~冲突人工裁决 UI~~ ✅ 本次完成。
3. **steer**（运行中改方向注入子 run；Q6 后置项）——下一个建议任务（纯编排层，无需网络）。
4. 主 agent 对 stalled 研究的重试上限收紧（观察项）。
5. 评估配置的对话式微调（参数微调靠主 agent 口头）。
6. **决策保守度调优**：首跑 kb_delta=0（全 watch 零敞口）——研究 rubric/decision prompt 的
   出手阈值值得迭代；`canary: true` 打开复跑验证诱饵防线。
7. serve 的代理感知：`.env`/启动脚本固化 `HTTPS_PROXY`（勿在代码里探测系统代理）。
8. （新，小）`fact/conflict_resolved` 目前不进 `_BRIDGE_TYPES`——研究 agent 轮内自动裁决在
   对话流只有冲突 raised 的进度行，无 resolved 回显；如需可见可加桥接 + `_progress_summary` 分支。

## 6. 新会话开工建议

- 先读本文档 + `docs/redesign-interaction-orchestration.md` §5 + `docs/how-to-extend.md`。
- 改动前 `uv run pytest`（176+3s）+ `npx vitest run`（9）+ `ruff check src tests` 建基线。
- TDD 先写失败测试；UI 改动先 assemble.ts + vitest 再渲染器。
- 加 gitignore 条目时用 `/dir/` 锚定根目录或先 `git check-ignore -v` 验证（§1 教训）。

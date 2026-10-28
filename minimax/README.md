# 规有所依 —— 面向软件持续演进的 SDD 上下文管理系统

> MiniMax × OceanBase 参赛作品 ｜ 分支 `2026-competition-ecosystem` ｜ 目录 `minimax/`

---

## 1. 一页技术说明

### 1.1 问题与做法

AI 开发助手改代码时，最容易出错的地方不是"不会写代码"，而是**用错了约束**：不知道当前该遵守
哪条规格、该动哪个文件、跑哪个测试，任务被打断后又忘了上次做到哪。

本作品实现一个**工程上下文模块**：以任务为中心，从数据库里找回适用的规格 / 代码 / 测试，
组装成带来源的开发上下文交给 MiniMax 完成代码修改，并把任务进展持久化到 PowerContext，
让下一次（哪怕是重启后的新进程）能接着做。

### 1.2 模块结构

```
                    ┌──────────────┐
   规格 specs/*.md  │              │  ┌───────────────┐
   代码 src/**/*.py ├─► importer ──┼─►│ seekdb 1.4.0  │  存储 + FULLTEXT 检索
   测试 tests/**.py │              │  │  spec_item    │
                    └──────────────┘  │  code_unit    │
                                     │  │  test_unit   │
   任务 tasks/*.md ──► retriever ────┴─►└───────────────┘
                          │                   ▲
                          ▼                   │ 命中结果（带来源）
                   context_builder            │
                          │                   │
                          ├──► MiniMax-M3 ────┴──► patcher ──► 工作区代码改动
                          │        （Anthropic Messages 协议）      │
                          │                                       ▼
                          │                                  pytest（目标 + 回归）
                          │                                       │
                          ▼                                       ▼
                    state_store ──► PowerContext 1.4.1 ◄───────────┘
                      （快照 + 记忆条目 + 结构化任务结果）
```

| 模块 | 文件 | 职责 |
| --- | --- | --- |
| 导入器 | `sdd_context/importer.py` | 把规格条目、代码文件/符号、测试文件/用例解析成结构化单元并 upsert 进 seekdb |
| 存储与检索 | `sdd_context/seekdb_store.py` | 建库建表、建 FULLTEXT 索引、执行 `MATCH ... AGAINST` 检索 |
| 分词 | `sdd_context/tokenize.py` | 中英文混合分词（中文切单字 + 二元组），让空格分词器也能召回中文 |
| 检索器 | `sdd_context/retriever.py` | 任务 → 候选资料，并逐条校验"必需来源真实存在" |
| 上下文组装 | `sdd_context/context_builder.py` | 渲染给人看的上下文报告 + 给模型的提示词 |
| 模型调用 | `sdd_context/minimax_client.py` | MiniMax 调用（Anthropic / OpenAI 双协议） |
| 补丁应用 | `sdd_context/patcher.py` | 解析并严格应用 unified diff（允许 hunk 整体偏移） |
| 状态持久化 | `sdd_context/state_store.py` + `powercontext_client.py` | 任务状态写入 / 读回 PowerContext |
| 编排 | `sdd_context/pipeline.py` | 端到端流水线与证据落盘 |
| 入口 | `sdd_context/cli.py` | `doctor` / `import` / `context` / `run` / `test` / `state` |

### 1.3 上下文是怎么选出来的

1. **任务定义**（`minimax/demo/tasks/TASK-001-status-filter.md`）用 YAML frontmatter 声明
   `required`：本次任务**必须**出现的规格条目、代码符号、测试文件。
2. **查询语句**由任务标题 + 目标 + 正文拼成，经中英文分词后在 seekdb 里做全文检索，
   候选集完全由数据库给出（`MATCH(body_tokens) AGAINST(...)`，带相关度分值排序）。
3. **必需来源逐条回查主键**（`spec_item.item_id` / `code_unit.unit_id` / `test_unit.unit_id`）：
   任何一条在数据库里不存在就报错退出——保证"输出中的来源真实存在"。
4. 上下文里每条资料都带 **来源路径 + 版本 + 内容哈希**，便于核对；
   规格重新导入后内容哈希变化，上下文随之变化，不会返回固定答案。
5. 组装结果同时产出两份：**给人看的报告**（`evidence/context.md`）与
   **给模型的提示词**（含待改文件的完整源码与测试全文）。

### 1.4 三项技术的分工

| 技术 | 在本作品里承担的角色 | 具体体现 |
| --- | --- | --- |
| **MiniMax** | 参与本次任务的代码生成 / 修改 | 上下文（含规格条目、待改文件全文、测试全文）作为提示词交给 `MiniMax-M3`，模型输出 unified diff，由模块应用；模型原始请求/响应完整留存于 `evidence/minimax_call_1.json` |
| **seekdb / OceanBase** | 存储工程资料或索引，查询结果用于构造任务上下文 | 规格 / 代码 / 测试写入 `sdd_context` 库三张表并建 FULLTEXT 索引；上下文的候选集由 `MATCH ... AGAINST` 在库内检索得到；导入批次、行数、命中单元记录在 `evidence/import.json` 与 `evidence/context.json` |
| **PowerContext** | 保存任务进展或验证结果，并在后续运行中读取、使用 | 任务状态三写（结构化任务结果 / 字节级快照 / 可检索记忆条目），跨进程读回（`sdd_context state load`），读回的"已完成 / 待办 / 已有验证结果"会进入下一次上下文提示词 |

> MiniMax 只负责"写代码"这一步；规格解释、资料检索、补丁应用、测试与状态管理都由模块自己完成，
> 三项技术都处在真实的数据流上，而不是只被"安装"或"提及"。

### 1.5 一次完整运行的数据流

```
import  →  seekdb(spec/code/test)         ← 资料入库（幂等，内容变才更新）
retrieve→  seekdb(MATCH ... AGAINST)       ← 库内检索 + 必需来源校验
context →  上下文报告 / 模型提示词
MiniMax →  unified diff                    ← 模型真实参与代码修改
patch   →  应用到工作区
pytest  →  修改前基线（失败）/ 修改后（通过）
state   →  PowerContext（快照 + 记忆 + 任务结果）
reload  →  新进程读回状态并继续
```

---

## 2. 材料索引

| 材料 | 位置 |
| --- | --- |
| 完整源码 | `minimax/sdd_context/`（模块）、`minimax/demo/`（业务工程） |
| 运行说明 | [`RUNBOOK.md`](RUNBOOK.md) |
| 验证样例 | [`docs/验证样例.md`](docs/验证样例.md) |
| 技术使用记录 | [`docs/技术使用记录.md`](docs/技术使用记录.md) |
| 代码与测试（快照 / 差异 / 日志） | [`evidence/`](evidence/) |
| 一页技术说明 | 本文件 |
| 六项必做项自检脚本 | `scripts/selfcheck.py` |

---

## 3. 实测结果摘要

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| 能够运行 | ✅ | `RUNBOOK.md` §4；`evidence/import.json`、`evidence/context.json` |
| MiniMax 实际参与 | ✅ | `evidence/minimax_call_1.json`（完整请求 / 响应）、`evidence/patch_1.diff`、`evidence/diff/` |
| 上下文正确 | ✅ | `evidence/context.md`（规格 / 代码 / 测试来源齐备）、`evidence/spec-update/`（规格更新后上下文随之变化） |
| 数据库实际参与 | ✅ | `evidence/import.json`、`evidence/context.json`（`db_evidence`）、`evidence/db-queries.log` |
| 状态能够复用 | ✅ | `evidence/state_before.json`（空状态）、`evidence/state_save.json`、`evidence/state_after_restart.json`（新进程读回） |
| 代码修改有效 | ✅ | `evidence/test_before.log`（12 passed / 5 failed）、`evidence/test_after_attempt_1.log`（17 passed / 0 failed） |

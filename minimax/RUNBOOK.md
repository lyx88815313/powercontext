# 运行说明（MiniMax × OceanBase —— 面向软件持续演进的 SDD 上下文管理系统）

> 适用分支：`2026-competition-ecosystem`
> 作品目录：`minimax/`

---

## 1. 组件与依赖版本

| 组件 | 作用 | 本次使用版本 | 安装/启动方式 |
| --- | --- | --- | --- |
| Python | 上下文模块运行时 | 3.13.9（要求 ≥ 3.11） | `python -m venv .venv` |
| seekdb（OceanBase seekdb） | 存放规格 / 代码 / 测试并做全文检索 | `5.7.25-OceanBase seekdb-v1.4.0.0` | 见 §2.1，MySQL 协议端口 `10100` |
| PowerContext | 保存 / 读回任务状态 | `1.4.1` | `pip install "powercontext[cli,server]==1.4.1"`，`powercontext server run` |
| MiniMax | 参与本次代码生成 | `MiniMax-M3`（走 Anthropic Messages 协议） | MiniMax 开放平台 API Key 或本机 MiniMax Code 运行时网关 |
| PyMySQL / httpx / PyYAML / pytest | 模块依赖 | 1.2.3 / 0.28.1 / 6.0.3 / 9.2.0 | `pip install -r minimax/requirements.txt` |

> PowerContext 的 `server` / `cli` 子命令由 `cli` 与 `server` 两个 extra 提供，
> 只安装主包会缺少这两个子命令（见 PowerContext `pyproject.toml`）。

---

## 2. 环境准备

### 2.1 seekdb（OceanBase / seekdb）

模块通过 **MySQL 协议**访问数据库，因此任何兼容的实例都可以，包括 OceanBase 社区版。

本机复现环境（WSL2 Ubuntu 内由 obd 部署的单机 seekdb）：

```bash
# WSL Ubuntu 内
~/seekdb-ctl.sh start          # 启动单机实例
~/seekdb-ctl.sh status         # 查看状态
# MySQL 端口 10100，账号 root（空密码）
```

Windows 侧可直接连接（`localhostForwarding=true`）：

```bash
mysql -h 127.0.0.1 -P 10100 -u root
```

数据库与表由模块自动创建，无需手工建表：

| 表 | 内容 |
| --- | --- |
| `sdd_context.spec_item` | 规格条目（条目号、版本、标题、正文、来源路径、内容哈希） |
| `sdd_context.code_unit` | 代码文件与代码符号 |
| `sdd_context.test_unit` | 测试文件与测试用例 |
| `sdd_context.import_manifest` | 每次导入的批次清单 |

三张内容表都带一列 `body_tokens`（中英文分词后的空格串）并建立 **FULLTEXT 索引**
（`WITH PARSER space PARSER_PROPERTIES=(min_token_size=1)`），检索走
`MATCH(body_tokens) AGAINST(...)`；实例不支持全文索引时自动降级为 `LIKE` 召回。

### 2.2 PowerContext

```bash
pip install "powercontext[cli,server]==1.4.1"

# 数据目录（默认写在用户数据目录，建议显式指定，便于"重置到空状态"）
export POWERCONTEXT_HOME=/path/to/pc-home

# 启动（默认监听 127.0.0.1:8000；本机 8000 被占用，改用 8100）
powercontext server run --host 127.0.0.1 --port 8100 --no-env-file
```

启动后自检：

```bash
curl -s http://127.0.0.1:8100/health/ready
curl -s http://127.0.0.1:8100/v1/scopes/default
```

> **不要**把仓库根目录的 `.env.example` 直接复制成 `.env`：
> 它默认要求 `OPENAI_API_KEY` 并启用 embedding 模型，未配置时 `/health/ready` 会变成
> `degraded`。本作品只用 PowerContext 的**显式 Memory / Source / Work 接口**，不需要推理模型。

### 2.3 MiniMax

两种接入方式，任选其一：

**方式 A：MiniMax 开放平台 API Key（推荐用于评审复现）**

```bash
export MINIMAX_API_KEY=<你的 MiniMax API Key>
export MINIMAX_BASE_URL=https://api.minimaxi.com/anthropic/v1     # Anthropic Messages 协议
# 或
export MINIMAX_BASE_URL=https://api.minimaxi.com/v1               # OpenAI Chat Completions 协议
export MINIMAX_PROTOCOL=anthropic        # anthropic | openai
export MINIMAX_MODEL=MiniMax-M2.1
```

**方式 B：本机已登录 MiniMax Code 运行时（开发环境回落）**

不设置任何环境变量时，模块会依次回落：

1. 读取 MiniMax Code 的 `config.yaml` 中的 `provider.minimax`（baseURL、模型、协议）；
2. 若 baseURL 指向 `agent.minimax.cn` 网关，则改用本机登录态里的 Bearer 令牌。

> 凭据只在运行时从本机读取，**不会写入仓库、证据文件或日志**。
> `sdd_context doctor` 输出的配置里凭据字段一律显示为 `***`。

---

## 3. 安装模块依赖

```bash
cd <仓库根目录>
python -m venv .venv
.venv/Scripts/python -m pip install -r minimax/requirements.txt      # Windows
# .venv/bin/python -m pip install -r minimax/requirements.txt        # Linux / macOS
```

把 `minimax/` 加入模块搜索路径（PowerShell）：

```powershell
$env:PYTHONPATH = "$PWD\minimax"
$env:SDD_SEEKDB_HOST = "127.0.0.1"
$env:SDD_SEEKDB_PORT = "10100"
$env:SDD_SEEKDB_USER = "root"
$env:SDD_POWERCONTEXT_URL = "http://127.0.0.1:8100"
```

（Linux / macOS 用 `export PYTHONPATH="$PWD/minimax"`。）

---

## 4. 调用入口

模块提供 **CLI 入口**（`python -m sdd_context <子命令>`），每个子命令的输入 / 输出见下表。

| 子命令 | 输入 | 输出 | 说明 |
| --- | --- | --- | --- |
| `doctor` | 环境变量 | JSON 报告 | 自检 seekdb / PowerContext / MiniMax 是否就绪；退出码非 0 表示依赖未就绪 |
| `import` | `--project <业务工程目录>` | JSON 导入清单 | 把规格 / 代码 / 测试导入 seekdb（幂等，内容变化才更新） |
| `context` | `--task <任务定义.md>` | Markdown 报告（`--output` 可指定文件） | 检索并组装开发上下文，定位规格 / 代码 / 测试来源 |
| `run` | `--task`、`--project` | JSON 汇总 + 证据目录 | **完整流水线**：导入 → 上下文 → MiniMax 改码 → 测试 → 状态写回 |
| `test` | `--project` | JSON + 可选日志 | 运行业务工程测试（`pytest tests -v --tb=short`） |
| `state load` | `--task <任务号>` | JSON（状态 + 证据） | 从 PowerContext 读回任务状态；没有状态时退出码 `2` |
| `state save` | `--task`、`--phase`、`--done`、`--todo` | JSON 回执 | 手动写入任务状态 |
| `state reset` | `--task` | JSON | 换到一个新的 scope，实现"从空状态开始" |

### 4.1 最小可运行序列（完整复现验证样例）

```bash
# 0) 自检
python -m sdd_context doctor

# 1) 导入工程资料到 seekdb
python -m sdd_context import --project minimax/demo

# 2) 检索并输出开发上下文
python -m sdd_context context \
  --task minimax/demo/tasks/TASK-001-status-filter.md \
  --output minimax/evidence/context.md

# 3) 完整流水线（导入 → 上下文 → MiniMax 改码 → 测试 → 状态写回）
python -m sdd_context run \
  --task minimax/demo/tasks/TASK-001-status-filter.md \
  --project minimax/demo

# 4) 业务工程测试
cd minimax/demo && python -m pytest tests -v && cd ../..

# 5) 模块自身测试
cd minimax && python -m pytest tests -v && cd ..

# 6) 六项必做项自检（对照题目验收条件逐项核对）
python minimax/scripts/selfcheck.py
```

### 4.2 跨会话续做（重启模块后读回状态）

```bash
# 第一次：跑完流水线，状态已写入 PowerContext
python -m sdd_context run --task minimax/demo/tasks/TASK-001-status-filter.md --project minimax/demo

# 模拟"重启上下文模块"：新开一个进程
python -m sdd_context state load --task TASK-001
```

`state load` 会先列出 PowerContext 里的记忆条目，定位本任务最新的 active 条目，
再按条目里的快照 id 回读**字节级快照原文**并还原成任务状态。

---

## 5. 配置项

全部通过环境变量覆盖，默认值见括号。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SDD_SEEKDB_HOST` | `127.0.0.1` | seekdb / OceanBase 地址 |
| `SDD_SEEKDB_PORT` | `10100` | MySQL 协议端口 |
| `SDD_SEEKDB_USER` | `root` | 账号 |
| `SDD_SEEKDB_PASSWORD` | 空 | 口令 |
| `SDD_SEEKDB_DATABASE` | `sdd_context` | 模块使用的库名（自动创建） |
| `SDD_POWERCONTEXT_URL` | `http://127.0.0.1:8000` | PowerContext 服务地址 |
| `SDD_POWERCONTEXT_TOKEN` | 空 | 开启鉴权时的 Bearer 令牌 |
| `SDD_POWERCONTEXT_SCOPE_ID` | 空 | 显式指定 scope；留空则用固定 `idempotency_key` 自动创建 |
| `SDD_POWERCONTEXT_SCOPE_KEY` | 空（用服务端默认 scope） | scope 的 `idempotency_key`，同一个 key 永远映射到同一个 scope；留空表示使用 PowerContext 启动时自动创建的默认 scope |
| `MINIMAX_API_KEY` | 空 | MiniMax 凭据 |
| `MINIMAX_BASE_URL` | 空 | MiniMax 接口地址（留空则回落到本机运行时配置） |
| `MINIMAX_MODEL` | `MiniMax-M3` | 模型名 |
| `MINIMAX_PROTOCOL` | 自动 | `anthropic` / `openai` |
| `MINIMAX_AUTH_STYLE` | 自动 | `x-api-key` / `bearer` |
| `MINIMAX_TIMEOUT` | `300` | 单次调用超时（秒） |
| `MINIMAX_MAX_TOKENS` | `8192` | 单次调用最大输出 token |
| `MINIMAX_TEMPERATURE` | `0.2` | 采样温度 |

---

## 6. 验证样例

见 [`docs/验证样例.md`](docs/验证样例.md)：任务描述、有效规格、初始数据、操作步骤、
预期结果、重置方法，以及"上下文中必须包含哪些来源"的清单。

---

## 7. 已知限制

1. **业务工程是随作品提供的最小工程**（`minimax/demo/`，纯标准库 + pytest），
   目的是让评审能在干净环境零依赖复现；接入真实业务工程时把 `--project` 指向其根目录即可，
   导入器只认 `specs/*.md`、`src/**/*.py`、`tests/**/*.py` 三类约定目录。
2. **规格导入器只解析 Markdown**：二级标题（`## SPEC-XX-01 标题`）被当作可独立引用的条目，
   YAML frontmatter 表格里的 `spec_id` / `version` / `owner` 等字段会被读入；
   其它格式（如 YAML / OpenAPI 规格）需要扩展 `importer.parse_spec_file`。
3. **代码切分基于 Python AST**：非 Python 语言需要替换 `importer.parse_code_file`。
4. **全文检索对中文做了自建分词**（单字 + 二元组）以适配 seekdb 的空格分词器；
   英文按单词切分。语义检索（向量）未启用，因为 seekdb 的 hybrid 向量索引需要注册
   外部 embedding 模型，会引入额外的凭据依赖。
5. **补丁应用要求上下文逐行完全一致**，但允许 hunk 起始行号整体偏移（与 `git apply` 同策略）；
   上下文真的不一致时直接失败，并把失败原因回灌给 MiniMax 重试（默认最多 3 次）。
6. **PowerContext 的结构化任务结果**（`/v1/work/outcomes/record`）对文本字段有"必须已 trim、
   不能带首尾空白"的约束，`produced_artifacts` 只能引用 PowerContext 里真实存在的构件；
   本模块因此把改动文件记在 `observations` 里，`produced_artifacts` 留空。
7. `sdd_context state reset` 通过**新建 scope** 实现"空状态"，旧 scope 的数据仍保留；
   要彻底清空，停止 PowerContext 并删除 `POWERCONTEXT_HOME` 下的 `powercontext.db` 后重启。
8. 模块默认连接 `127.0.0.1`；PowerContext 在非回环地址且未开鉴权时会拒绝启动，
   这是它的安全策略，请保持回环监听或显式配置鉴权令牌。

---

## 8. 目录结构

```
minimax/
├── README.md                 # 一页技术说明（模块结构 / 上下文选择方式 / 技术分工）
├── RUNBOOK.md                # 本文件：运行说明
├── requirements.txt          # 模块依赖
├── sdd_context/              # 上下文模块源码
│   ├── cli.py                # CLI 入口
│   ├── config.py             # 配置（seekdb / PowerContext / MiniMax）
│   ├── importer.py           # 规格 / 代码 / 测试导入器
│   ├── seekdb_store.py       # seekdb 存储与检索
│   ├── tokenize.py           # 中英文分词（供 FULLTEXT 索引）
│   ├── retriever.py          # 任务 → 相关资料检索
│   ├── context_builder.py    # 上下文渲染（报告 + 模型提示词）
│   ├── minimax_client.py     # MiniMax 调用（anthropic / openai 双协议）
│   ├── patcher.py            # unified diff 解析与应用
│   ├── powercontext_client.py# PowerContext REST 客户端
│   ├── state.py              # 任务状态模型
│   ├── state_store.py        # 状态写入 / 读回（PowerContext）
│   ├── pipeline.py           # 端到端流水线
│   └── taskdef.py            # 任务定义解析
├── tests/                    # 模块自身测试
├── demo/                     # 业务工程（规格 + 代码 + 测试 + 任务定义）
├── docs/
│   ├── 验证样例.md
│   └── 技术使用记录.md
└── evidence/                 # 实际运行记录（命令、退出码、日志、补丁、差异）
```

# fulfillment-svc —— 演示用业务工程

一个能独立运行、带 pytest 测试的最小业务工程，用来承载"SDD 上下文模块"的开发任务。

## 目录

```
demo/
├── specs/                       # 规格（SDD）
│   ├── SPEC-ORDER-QUERY.md      # 订单列表查询规格（5 个可独立引用的条目）
│   └── SPEC-PRICING.md          # 结算金额规格（3 个条目，回归保护范围）
├── src/fulfillment/             # 业务代码
│   ├── __init__.py
│   ├── errors.py                # 异常类型（FulfillmentError / InvalidFilterError / InvalidAmountError）
│   ├── models.py                # 领域模型（Order）
│   ├── pricing.py               # 结算金额计算（回归保护范围）
│   └── query.py                 # 订单列表查询 ← 本次任务的修改目标
├── tests/                       # 测试
│   ├── conftest.py              # 把 src 加入 sys.path
│   ├── test_query_target.py     # 目标功能测试（随规格引入，修改前 5 个失败）
│   ├── test_query_regression.py # 查询模块回归测试（修改前后都通过）
│   └── test_pricing.py          # 结算模块回归测试（修改前后都通过）
└── tasks/
    └── TASK-001-status-filter.md # 任务定义（机器可读的验证样例）
```

## 运行

```bash
cd minimax/demo
python -m pytest tests -v
```

无需安装业务包（`tests/conftest.py` 会把 `src` 加入导入路径），只依赖 pytest。

## 三类测试的分工

| 文件 | 角色 | 修改前 | 修改后 |
| --- | --- | --- | --- |
| `test_query_target.py` | **目标功能测试**：验证 SPEC-ORDER-QUERY-01/02/05 要求的新行为 | 5 failed / 2 passed | 7 passed |
| `test_query_regression.py` | **回归测试**：验证 SPEC-ORDER-QUERY-03/04 等既有行为不变 | 4 passed | 4 passed |
| `test_pricing.py` | **回归测试**：验证结算模块完全不受影响 | 6 passed | 6 passed |

目标测试文件在**代码修改之前**就随规格一起引入 —— 这样"修改前失败"证明的是
**目标行为未实现**，而不是环境缺失。

## 修改前后的代码

见 `../evidence/diff/`：

- `src__fulfillment__query.py.before` —— 修改前快照
- `src__fulfillment__query.py.after` —— 修改后快照
- `src__fulfillment__query.py.diff` —— 两者之间的统一 diff
- `../evidence/patch_1.diff` —— MiniMax 模型原始输出的补丁

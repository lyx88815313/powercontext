"""验证"规格更新 → 重新导入 → 上下文使用更新内容"。

流程：

1. 用当前规格导入 seekdb 并输出上下文 → ``context_before.md``；
2. 对规格做一次**真实的版本更新**（版本号 + 一条新增约束），重新导入并输出上下文
   → ``context_after.md``；
3. 还原规格并重新导入（让数据库与仓库状态保持一致）→ ``context_restored.md``。

脚本会把三份上下文与规格差异都写进 ``--out`` 目录，并在结束时断言：
更新后的上下文确实与更新前不同（规格条目版本 / 内容哈希 / 正文均发生变化）。

用法::

    python minimax/scripts/verify_spec_update.py --out minimax/evidence/spec-update
"""

from __future__ import annotations

import argparse
import difflib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sdd_context.config import load_settings  # noqa: E402
from sdd_context.context_builder import render_report  # noqa: E402
from sdd_context.importer import import_project  # noqa: E402
from sdd_context.retriever import retrieve_context  # noqa: E402
from sdd_context.seekdb_store import SeekdbStore  # noqa: E402
from sdd_context.taskdef import load_task  # noqa: E402

#: 更新后的规格：版本号 1.2.0 -> 1.3.0，并新增一条约束条目。
NEW_SPEC = """# SPEC-ORDER-QUERY 订单列表查询规格

| 项 | 值 |
| --- | --- |
| spec_id | SPEC-ORDER-QUERY |
| version | 1.3.0 |
| owner | fulfillment-team |
| applies_to | `fulfillment.query` |
| status | effective |

## SPEC-ORDER-QUERY-01 订单状态筛选（目标条目）

`fulfillment.query.list_orders` 必须支持按订单状态 `status` 过滤。

- 当 `status` 为 `None` 时**不得**过滤，返回全部订单（必须保持既有行为）。
- 当 `status` 为非空字符串时，只返回 `order.status == status` 的订单（必须）。
- 当 `status` 不属于允许集合 `{PAID, SHIPPED, CANCELLED, REFUNDED}` 时，必须抛出
  `fulfillment.errors.InvalidFilterError`，**不得**静默返回空列表（必须）。

## SPEC-ORDER-QUERY-02 金额下界筛选（目标条目）

- `min_amount` 必须按“大于等于”语义过滤，即保留 `order.amount >= min_amount` 的订单（必须）。
- `min_amount == 0` 必须等价于不过滤（必须保持）。
- `min_amount < 0` 必须抛出 `fulfillment.errors.InvalidFilterError`（必须）。

## SPEC-ORDER-QUERY-03 结果稳定性

筛选结果必须保持入参顺序，**不得**重新排序，**不得**去重（必须保持既有行为）。

## SPEC-ORDER-QUERY-04 空输入

传入空序列时必须返回空序列，**不得**抛出异常（必须保持既有行为）。

## SPEC-ORDER-QUERY-05 参数互不干扰

同时给出 `status` 与 `min_amount` 时，两个条件之间是“与”关系（必须）。
任一条件为 `None` 时只应用另一个条件（必须）。

## SPEC-ORDER-QUERY-06 状态集合来源

允许的订单状态集合必须与本规格条目 SPEC-ORDER-QUERY-01 中列出的集合保持一致，
**不得**在代码里另行硬编码一份不同的集合（必须）。
"""


def _render(store: SeekdbStore, task_path: Path) -> tuple[str, str]:
    """导入并渲染上下文，返回（报告文本, digest）。"""
    settings = load_settings()
    task = load_task(task_path)
    import_project(store, ROOT / "demo")
    context = retrieve_context(store, task, settings)
    return render_report(context), context.digest()


def main() -> int:
    """执行规格更新验证。"""
    parser = argparse.ArgumentParser(description="验证规格更新后的上下文变化")
    parser.add_argument(
        "--task",
        default=str(ROOT / "demo" / "tasks" / "TASK-001-status-filter.md"),
        help="任务定义文件",
    )
    parser.add_argument("--out", default=str(ROOT / "evidence" / "spec-update"), help="输出目录")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    spec_path = ROOT / "demo" / "specs" / "SPEC-ORDER-QUERY.md"
    # 用 newline="" 读写，避免换行符被平台改写（保证还原后与原文件逐字节一致）
    original = spec_path.read_text(encoding="utf-8", newline="")
    backup = out_dir / "SPEC-ORDER-QUERY.original.md"
    backup.write_text(original, encoding="utf-8", newline="")

    settings = load_settings()
    task_path = Path(args.task)
    store = SeekdbStore(settings)
    try:
        before, before_digest = _render(store, task_path)
        (out_dir / "context_before.md").write_text(before, encoding="utf-8", newline="")

        # ---- 真实的规格更新 ----
        spec_path.write_text(NEW_SPEC, encoding="utf-8", newline="")
        after, after_digest = _render(store, task_path)
        (out_dir / "context_after.md").write_text(after, encoding="utf-8", newline="")
        (out_dir / "spec_change.diff").write_text(
            "".join(
                difflib.unified_diff(
                    original.splitlines(keepends=True),
                    NEW_SPEC.splitlines(keepends=True),
                    fromfile="a/specs/SPEC-ORDER-QUERY.md",
                    tofile="b/specs/SPEC-ORDER-QUERY.md",
                )
            ),
            encoding="utf-8",
            newline="",
        )

        # ---- 还原规格并让数据库与仓库保持一致 ----
        spec_path.write_text(original, encoding="utf-8", newline="")
        restored, restored_digest = _render(store, task_path)
        (out_dir / "context_restored.md").write_text(restored, encoding="utf-8", newline="")
    finally:
        store.close()

    changed = before != after
    summary = (
        "# 规格更新验证\n\n"
        f"- 更新前上下文 digest：`{before_digest}`\n"
        f"- 更新后上下文 digest：`{after_digest}`\n"
        f"- 还原后上下文 digest：`{restored_digest}`\n"
        f"- 更新前后上下文是否不同：**{'是' if changed else '否'}**\n"
        f"- 还原后是否与更新前一致：**{'是' if restored == before else '否'}**\n\n"
        "规格更新内容见 `spec_change.diff`；更新前后的上下文分别见 "
        "`context_before.md` / `context_after.md`；`context_restored.md` 是把规格还原后"
        "重新导入得到的结果（用于证明上下文跟着规格走，而不是固定答案）。\n"
    )
    (out_dir / "README.md").write_text(summary, encoding="utf-8", newline="")
    print(summary)
    if not changed:
        print("ERROR: 规格更新后上下文没有变化", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

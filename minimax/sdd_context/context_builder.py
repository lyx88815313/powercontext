"""开发上下文的渲染。

同一份上下文有两种呈现：

* **报告**（``render_report``）—— 给人看，明确列出"适用的规格条目 / 需要修改的代码 /
  用于验证的测试 / 已有任务状态"，每一条都带真实来源路径与版本。
* **提示词**（``render_prompt``）—— 给 MiniMax 看，包含实现所需的完整文件内容。
"""

from __future__ import annotations

from typing import Any

from .retriever import DevelopmentContext
from .seekdb_store import RetrievedItem
from .state import TaskState

SYSTEM_PROMPT = """你是一名严格遵循规格驱动开发（SDD）规范的开发助手。
你会收到：本次开发任务、适用的规格条目、需要修改的代码文件、用于验证的测试。

工作要求：
1. 只实现规格明确要求的行为，不得改变规格中标注为"必须保持"的既有行为。
2. 改动必须是最小化的：不要重排、重命名、格式化与本次任务无关的代码。
3. 不得修改测试文件来让测试通过；不得删除或跳过任何断言。
4. 如果规格与现有代码冲突，以规格为准。

输出格式（必须严格遵守）：
- 只输出一个 ```diff 代码块，内容是标准 unified diff。
- diff 中的文件路径必须相对项目根目录，例如 `src/fulfillment/query.py`。
- 不要在代码块之外输出任何解释性文字。
"""


def _group_by_path(items: list[RetrievedItem]) -> dict[str, list[RetrievedItem]]:
    grouped: dict[str, list[RetrievedItem]] = {}
    for item in items:
        grouped.setdefault(item.path, []).append(item)
    return grouped


def _file_body(items: list[RetrievedItem]) -> tuple[str, str]:
    """返回（文件内容, 使用的单元说明）。"""
    for item in items:
        if item.extra.get("kind") in {"file", "test_file"}:
            return item.body, item.unit_id
    first = items[0]
    return first.body, first.unit_id


def render_report(context: DevelopmentContext, state: TaskState | None = None) -> str:
    """渲染给人看的上下文报告。"""
    lines: list[str] = []
    task = context.task
    lines.append(f"# 开发上下文：{task.task_id} {task.title}")
    lines.append("")
    lines.append("## 1. 本次任务")
    lines.append("")
    lines.append(task.objective or task.body)
    lines.append("")
    lines.append(f"- 上下文摘要（digest）：`{context.digest()}`")
    lines.append(f"- 数据库：{context.db_evidence.get('server_version', '')} / 库 `{context.db_evidence.get('database', '')}`")
    modes = context.db_evidence.get("retrieval_mode", {})
    lines.append(f"- 检索方式：{', '.join(f'{k}={v}' for k, v in modes.items())}")
    lines.append("")

    lines.append("## 2. 适用的规格条目")
    lines.append("")
    if context.specs:
        for item in context.specs:
            extra = item.extra
            lines.append(f"### {item.title}")
            lines.append("")
            lines.append(f"- 来源：`{item.source_path}`")
            lines.append(f"- 规格编号：`{extra.get('spec_id')}` / 条目 `{extra.get('item_id')}`")
            lines.append(f"- 版本：`{extra.get('version')}`　内容哈希：`{str(extra.get('content_hash'))[:16]}`")
            lines.append("")
            lines.append(item.body.strip())
            lines.append("")
    else:
        lines.append("_（检索未命中任何规格条目）_")
        lines.append("")

    lines.append("## 3. 需要修改的代码")
    lines.append("")
    for path, items in _group_by_path(context.codes).items():
        body, unit_id = _file_body(items)
        symbols = [i.extra.get("symbol") for i in items if i.extra.get("symbol")]
        lines.append(f"### `{path}`")
        lines.append("")
        lines.append(f"- 来源单元：`{unit_id}`")
        if symbols:
            lines.append(f"- 涉及符号：{', '.join(f'`{s}`' for s in symbols)}")
        lines.append("")
        lines.append("```python")
        lines.append(body.rstrip("\n"))
        lines.append("```")
        lines.append("")

    lines.append("## 4. 用于验证功能及兼容性的测试")
    lines.append("")
    for path, items in _group_by_path(context.tests).items():
        body, unit_id = _file_body(items)
        names = [i.extra.get("test_name") for i in items if i.extra.get("test_name")]
        lines.append(f"### `{path}`")
        lines.append("")
        lines.append(f"- 来源单元：`{unit_id}`")
        if names:
            lines.append(f"- 用例：{', '.join(f'`{n}`' for n in names)}")
        lines.append("")
        lines.append("```python")
        lines.append(body.rstrip("\n"))
        lines.append("```")
        lines.append("")

    lines.append("## 5. 任务状态（来自 PowerContext）")
    lines.append("")
    if state is None:
        lines.append("_（当前无已保存的任务状态，即空状态）_")
    else:
        lines.append(f"- 阶段：`{state.phase}`　更新时间：{state.updated_at}")
        lines.append(f"- 已完成：{_bullets(state.done)}")
        lines.append(f"- 待办：{_bullets(state.todo)}")
        lines.append(f"- 已有验证结果：")
        if state.checks:
            for check in state.checks:
                exit_code = "" if check.exit_code is None else f"，exit={check.exit_code}"
                lines.append(f"  - `{check.name}` → {check.status}{exit_code}")
        else:
            lines.append("  - （无）")
        if state.next_action:
            lines.append(f"- 下一步：{state.next_action}")
    lines.append("")
    return "\n".join(lines)


def _bullets(items: list[str]) -> str:
    if not items:
        return "（无）"
    return "；".join(items)


def render_prompt(
    context: DevelopmentContext,
    *,
    state: TaskState | None = None,
    feedback: str = "",
) -> str:
    """渲染给 MiniMax 的用户提示词。"""
    task = context.task
    parts: list[str] = []

    parts.append("## 本次开发任务")
    parts.append("")
    parts.append(f"任务编号：{task.task_id}")
    parts.append(f"任务标题：{task.title}")
    parts.append("")
    parts.append(task.objective or task.body)
    parts.append("")

    parts.append("## 适用的规格条目（必须遵守）")
    parts.append("")
    for item in context.specs:
        extra = item.extra
        parts.append(
            f"### {extra.get('spec_id')} / {extra.get('item_id')} {item.title} "
            f"（来源 `{item.source_path}`，版本 `{extra.get('version')}`）"
        )
        parts.append("")
        parts.append(item.body.strip())
        parts.append("")

    parts.append("## 需要修改的代码")
    parts.append("")
    for path, items in _group_by_path(context.codes).items():
        body, unit_id = _file_body(items)
        symbols = [i.extra.get("symbol") for i in items if i.extra.get("symbol")]
        parts.append(f"### 文件 `{path}`（来源单元 `{unit_id}`）")
        if symbols:
            parts.append(f"涉及符号：{', '.join(symbols)}")
        parts.append("")
        parts.append("```python")
        parts.append(body.rstrip("\n"))
        parts.append("```")
        parts.append("")

    parts.append("## 用于验证的测试（只读，不得修改）")
    parts.append("")
    for path, items in _group_by_path(context.tests).items():
        body, unit_id = _file_body(items)
        parts.append(f"### 测试文件 `{path}`（来源单元 `{unit_id}`）")
        parts.append("")
        parts.append("```python")
        parts.append(body.rstrip("\n"))
        parts.append("```")
        parts.append("")

    if state is not None and (state.done or state.todo or state.checks):
        parts.append("## 已有任务状态（跨会话续做时提供）")
        parts.append("")
        parts.append(f"- 阶段：`{state.phase}`")
        parts.append(f"- 已完成：{_bullets(state.done)}")
        parts.append(f"- 待办：{_bullets(state.todo)}")
        if state.checks:
            parts.append("- 已有验证结果：")
            for check in state.checks:
                parts.append(f"  - `{check.name}` → {check.status}")
        parts.append("")

    if feedback:
        parts.append("## 上一次尝试的失败反馈（请据此修正）")
        parts.append("")
        parts.append(feedback.strip())
        parts.append("")

    parts.append("## 输出要求")
    parts.append("")
    parts.append("请输出实现上述规格的 unified diff，只包含一个 ```diff 代码块。")
    return "\n".join(parts)


def context_sources(context: DevelopmentContext) -> list[dict[str, Any]]:
    """导出上下文中的全部来源（便于核对"来源真实存在"）。"""
    sources: list[dict[str, Any]] = []
    for item in context.specs + context.codes + context.tests:
        sources.append(
            {
                "category": item.category,
                "unit_id": item.unit_id,
                "path": item.path,
                "source_path": item.source_path,
                "title": item.title,
                "version": item.extra.get("version"),
                "content_hash": item.extra.get("content_hash"),
                "updated_at": item.extra.get("updated_at"),
            }
        )
    return sources

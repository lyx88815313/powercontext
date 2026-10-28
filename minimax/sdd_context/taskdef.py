"""开发任务定义（验证样例的机器可读形式）。

任务文件是带 YAML frontmatter 的 Markdown：

```markdown
---
task_id: TASK-001
title: 订单状态筛选
required:
  spec_items: [SPEC-ORDER-QUERY-01, SPEC-ORDER-QUERY-02]
  code_units: ["src/fulfillment/query.py::list_orders"]
  test_units: ["tests/test_query_target.py"]
---
```

``required`` 里的每一项都必须在 seekdb 里真实存在，模块会在组装上下文时逐条校验，
缺失即报错 —— 保证输出中的来源真实存在。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class TaskDefinition:
    """一个开发任务。"""

    task_id: str
    title: str
    objective: str
    body: str
    required_spec_items: list[str] = field(default_factory=list)
    required_code_units: list[str] = field(default_factory=list)
    required_test_units: list[str] = field(default_factory=list)
    project_root: str = ""
    source_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "objective": self.objective,
            "required": {
                "spec_items": self.required_spec_items,
                "code_units": self.required_code_units,
                "test_units": self.required_test_units,
            },
            "source_path": self.source_path,
        }


def _split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """拆分 YAML frontmatter 与正文。"""
    if not raw.startswith("---"):
        return {}, raw
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, raw
    meta = yaml.safe_load(parts[1]) or {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, parts[2].lstrip("\n")


def load_task(path: Path) -> TaskDefinition:
    """从 Markdown 文件加载任务定义。"""
    raw = path.read_text(encoding="utf-8")
    meta, body = _split_frontmatter(raw)
    required = meta.get("required") or {}
    if not isinstance(required, dict):
        required = {}

    def _as_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return [str(item) for item in value]

    task = TaskDefinition(
        task_id=str(meta.get("task_id") or path.stem),
        title=str(meta.get("title") or path.stem),
        objective=str(meta.get("objective") or ""),
        body=body.strip(),
        required_spec_items=_as_list(required.get("spec_items")),
        required_code_units=_as_list(required.get("code_units")),
        required_test_units=_as_list(required.get("test_units")),
        project_root=str(meta.get("project_root") or ""),
        source_path=str(path),
    )
    if not task.objective:
        # 没有单独写 objective 时，取正文第一段作为目标描述
        for paragraph in body.split("\n\n"):
            cleaned = paragraph.strip()
            if cleaned and not cleaned.startswith("#"):
                task.objective = cleaned
                break
    return task

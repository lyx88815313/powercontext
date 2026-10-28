"""任务状态模型。

状态内容对应题目要求的"已完成事项、待办事项和已有验证结果"，
序列化后写入 PowerContext（内容源 + 记忆条目 + 工作结果三处）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

CHECK_STATUSES = ("passed", "failed", "skipped", "timed_out", "unavailable", "cancelled", "unknown")


@dataclass
class CheckResult:
    """一次验证（测试）的结果。"""

    name: str
    status: str = "unknown"
    details: str = ""
    command: str = ""
    exit_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "details": self.details,
            "command": self.command,
            "exit_code": self.exit_code,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CheckResult":
        return cls(
            name=str(data.get("name", "")),
            status=str(data.get("status", "unknown")),
            details=str(data.get("details", "")),
            command=str(data.get("command", "")),
            exit_code=data.get("exit_code"),
        )


@dataclass
class TaskState:
    """一个开发任务的进展快照。"""

    task_id: str
    title: str = ""
    objective: str = ""
    phase: str = "init"
    updated_at: str = ""
    done: list[str] = field(default_factory=list)
    todo: list[str] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    produced_artifacts: list[str] = field(default_factory=list)
    applied_spec_items: list[str] = field(default_factory=list)
    changed_code_units: list[str] = field(default_factory=list)
    verification_tests: list[str] = field(default_factory=list)
    next_action: str = ""
    context_digest: str = ""
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.updated_at:
            self.updated_at = datetime.now().isoformat(timespec="seconds")

    # ------------------------------------------------------------------ 序列化

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的字典。"""
        return {
            "task_id": self.task_id,
            "title": self.title,
            "objective": self.objective,
            "phase": self.phase,
            "updated_at": self.updated_at,
            "done": list(self.done),
            "todo": list(self.todo),
            "checks": [check.to_dict() for check in self.checks],
            "produced_artifacts": list(self.produced_artifacts),
            "applied_spec_items": list(self.applied_spec_items),
            "changed_code_units": list(self.changed_code_units),
            "verification_tests": list(self.verification_tests),
            "next_action": self.next_action,
            "context_digest": self.context_digest,
            "notes": list(self.notes),
        }

    def to_json(self) -> str:
        """转成规范化 JSON 文本（写入 PowerContext 的字节级快照）。"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskState":
        """从字典恢复状态。"""
        return cls(
            task_id=str(data.get("task_id", "")),
            title=str(data.get("title", "")),
            objective=str(data.get("objective", "")),
            phase=str(data.get("phase", "init")),
            updated_at=str(data.get("updated_at", "")),
            done=[str(x) for x in data.get("done", [])],
            todo=[str(x) for x in data.get("todo", [])],
            checks=[CheckResult.from_dict(x) for x in data.get("checks", [])],
            produced_artifacts=[str(x) for x in data.get("produced_artifacts", [])],
            applied_spec_items=[str(x) for x in data.get("applied_spec_items", [])],
            changed_code_units=[str(x) for x in data.get("changed_code_units", [])],
            verification_tests=[str(x) for x in data.get("verification_tests", [])],
            next_action=str(data.get("next_action", "")),
            context_digest=str(data.get("context_digest", "")),
            notes=[str(x) for x in data.get("notes", [])],
        )

    @classmethod
    def from_json(cls, text: str) -> "TaskState":
        """从 JSON 文本恢复状态。"""
        return cls.from_dict(json.loads(text))

    # ------------------------------------------------------------------ 便捷方法

    def mark_done(self, item: str) -> None:
        """标记一项已完成（幂等）。"""
        if item and item not in self.done:
            self.done.append(item)

    def mark_todo(self, item: str) -> None:
        """加入待办（幂等）。"""
        if item and item not in self.todo:
            self.todo.append(item)

    def remove_todo(self, item: str) -> None:
        """从待办移除（幂等）。"""
        if item in self.todo:
            self.todo.remove(item)

    def add_check(self, check: CheckResult) -> None:
        """写入一条验证结果（同名覆盖）。"""
        for index, existing in enumerate(self.checks):
            if existing.name == check.name:
                self.checks[index] = check
                return
        self.checks.append(check)

    def compact_summary(self) -> str:
        """生成写入记忆条目的紧凑摘要（需控制在 8192 字节内）。"""
        payload = {
            "task_id": self.task_id,
            "phase": self.phase,
            "updated_at": self.updated_at,
            "done": self.done,
            "todo": self.todo,
            "checks": [
                {"name": c.name, "status": c.status, "exit_code": c.exit_code}
                for c in self.checks
            ],
            "next_action": self.next_action,
            "context_digest": self.context_digest,
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

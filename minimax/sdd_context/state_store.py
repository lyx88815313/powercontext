"""任务状态的持久化与读回（PowerContext）。

写入三处，职责不同：

* ``/v1/work/outcomes/record`` —— 结构化任务结果（PowerContext 原生的任务语义：
  objective / observations / checks / produced_artifacts / remaining_work）。
* ``/v1/sources/content``     —— 字节级快照（状态的真相来源）。
* ``/v1/memory/remember``     —— 可检索的记忆条目，文本里带上快照 id 作为指针。

读回时：先列出记忆条目，找到本任务最新的 active 条目 → 取出快照 id →
回读快照原文 → 解析成 :class:`~sdd_context.state.TaskState`。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .powercontext_client import PowerContextClient
from .state import CheckResult, TaskState

MEMORY_KIND_PREFIX = "sdd-task-state"
OUTCOME_KIND_PREFIX = "sdd-task-outcome"


def memory_kind(task_id: str) -> str:
    """记忆条目的 kind（按任务隔离）。"""
    return f"{MEMORY_KIND_PREFIX}:{task_id}"


def outcome_source_id(task_id: str, digest: str) -> str:
    """结构化任务结果的 source_id（按内容摘要隔离，重复写入幂等）。"""
    return f"{OUTCOME_KIND_PREFIX}:{task_id}:{digest[:12]}"


def snapshot_source_id(task_id: str, digest: str) -> str:
    """状态快照的 source_id（内容寻址，重复写入幂等）。"""
    return f"{MEMORY_KIND_PREFIX}:{task_id}:{digest[:12]}"


@dataclass
class SaveReceipt:
    """一次状态写入的回执。"""

    scope_id: str
    memory_kind: str
    snapshot_source_id: str
    outcome_source_id: str
    memory_changed: bool
    digest: str
    outcome_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "memory_kind": self.memory_kind,
            "snapshot_source_id": self.snapshot_source_id,
            "outcome_source_id": self.outcome_source_id,
            "memory_changed": self.memory_changed,
            "digest": self.digest,
            "outcome_error": self.outcome_error,
        }


def build_outcome(state: TaskState) -> dict[str, Any]:
    """把任务状态转成 PowerContext 的 ``TaskOutcome`` 结构。

    注意 PowerContext 的契约约束（来自其 OpenAPI 与实现）：

    * ``observations`` 至少要有一条；
    * 所有文本字段必须**已 trim**（不能带首尾空白），``remaining_work`` 的条目还必须唯一；
    * ``produced_artifacts`` 只能引用 PowerContext 里**真实存在**的构件，
      因此这里留空，改动的代码文件记在 ``observations`` 里。
    """
    observations = [
        {"text": item.strip(), "basis": "declared", "evidence": []}
        for item in state.done
        if item and item.strip()
    ]
    if state.changed_code_units:
        observations.append(
            {
                "text": ("本次修改的代码文件：" + ", ".join(state.changed_code_units)).strip(),
                "basis": "declared",
                "evidence": [],
            }
        )
    if not observations:
        observations.append(
            {
                "text": f"任务 {state.task_id} 当前阶段：{state.phase}".strip(),
                "basis": "declared",
                "evidence": [],
            }
        )
    checks = [
        {
            "name": check.name.strip(),
            "status": check.status,
            "details": (check.details or check.name).strip(),
            "basis": "declared",
            "evidence": [],
        }
        for check in state.checks
    ]
    remaining: list[str] = []
    for item in state.todo:
        text = item.strip()
        if text and text not in remaining:
            remaining.append(text)
    return {
        "schema": "powercontext.task-outcome.v1",
        "trust": "untrusted_observation",
        "objective": (state.objective or state.title or state.task_id).strip(),
        "status": _outcome_status(state),
        "summary": (state.next_action or state.phase or state.task_id).strip(),
        "observations": observations[:64],
        "checks": checks[:64],
        "produced_artifacts": [],
        "remaining_work": remaining[:64],
    }


def _outcome_status(state: TaskState) -> str:
    """根据检查结果推断任务状态。"""
    statuses = {check.status for check in state.checks}
    if not statuses:
        return "partial"
    if "failed" in statuses:
        return "failed"
    if statuses <= {"passed", "skipped"}:
        return "succeeded"
    return "partial"


def save_state(client: PowerContextClient, state: TaskState) -> SaveReceipt:
    """把任务状态写入 PowerContext。"""
    scope_id = client.ensure_scope()
    snapshot = state.to_json()
    digest = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
    source_id = snapshot_source_id(state.task_id, digest)
    outcome_id = outcome_source_id(state.task_id, digest)

    client.capture_content(
        source_id,
        snapshot,
        metadata={
            "kind": "sdd_task_state",
            "task_id": state.task_id,
            "phase": state.phase,
        },
    )

    outcome_error = ""
    try:
        client.record_outcome(outcome_id, build_outcome(state))
    except Exception as exc:  # 结构化结果写入失败不影响快照与记忆条目
        outcome_error = f"{type(exc).__name__}: {exc}"

    pointer = json.loads(state.compact_summary())
    pointer["snapshot_source_id"] = source_id
    pointer["outcome_source_id"] = outcome_id
    pointer["snapshot_digest"] = digest
    memory_text = json.dumps(pointer, ensure_ascii=False, sort_keys=True)
    if len(memory_text.encode("utf-8")) > 8192:
        memory_text = json.dumps(
            {
                "task_id": state.task_id,
                "phase": state.phase,
                "updated_at": state.updated_at,
                "snapshot_source_id": source_id,
                "outcome_source_id": outcome_id,
                "snapshot_digest": digest,
                "next_action": state.next_action,
                "truncated": True,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    remembered = client.remember(
        memory_kind(state.task_id),
        memory_text,
        reason="SDD 任务状态写入",
    )
    return SaveReceipt(
        scope_id=scope_id,
        memory_kind=memory_kind(state.task_id),
        snapshot_source_id=source_id,
        outcome_source_id=outcome_id,
        memory_changed=bool(remembered.get("changed")) if isinstance(remembered, dict) else False,
        digest=digest,
        outcome_error=outcome_error,
    )


def _entry_state_name(entry: dict[str, Any]) -> str:
    """取记忆条目的生命周期状态名（兼容字符串与对象两种形态）。"""
    state = entry.get("state")
    if isinstance(state, str):
        return state
    if isinstance(state, dict):
        return str(state.get("state") or state.get("name") or "active")
    return "active"


def load_state(client: PowerContextClient, task_id: str) -> tuple[TaskState | None, dict[str, Any]]:
    """从 PowerContext 读回任务状态。

    返回 ``(状态, 证据)``；没有状态时状态为 ``None``（即空状态）。
    """
    evidence: dict[str, Any] = {"scope_id": client.ensure_scope(), "found": False}
    kind = memory_kind(task_id)
    entries = client.list_entries(limit=100)
    evidence["memory_entries"] = len(entries)
    candidates = [
        entry
        for entry in entries
        if entry.get("kind") == kind and _entry_state_name(entry) == "active"
    ]
    if not candidates:
        evidence["reason"] = "未找到本任务的记忆条目"
        return None, evidence

    def _revision(entry: dict[str, Any]) -> int:
        artifact = entry.get("artifact") or {}
        revision = artifact.get("revision")
        try:
            return int(revision)
        except (TypeError, ValueError):
            return 0

    def _state_version(entry: dict[str, Any]) -> int:
        version = entry.get("state_version")
        try:
            return int(version)
        except (TypeError, ValueError):
            return 0

    def _pointer_of(entry: dict[str, Any]) -> dict[str, Any]:
        try:
            pointer = json.loads(entry.get("text") or "{}")
        except json.JSONDecodeError:
            return {}
        return pointer if isinstance(pointer, dict) else {}

    # 同 kind 的多次写入会留下多个条目，按"指针里的更新时间"取最新，
    # 再用 state_version / revision 做次序兜底。
    candidates.sort(
        key=lambda entry: (
            str(_pointer_of(entry).get("updated_at") or ""),
            _state_version(entry),
            _revision(entry),
        ),
        reverse=True,
    )
    latest = candidates[0]
    evidence["memory_entry_revision"] = _revision(latest)
    pointer = _pointer_of(latest)
    evidence["pointer"] = pointer
    source_id = pointer.get("snapshot_source_id")
    if not source_id:
        evidence["reason"] = "记忆条目中没有快照指针"
        return None, evidence

    snapshot = client.read_content(source_id)
    evidence["snapshot_source_id"] = source_id
    if snapshot is None:
        evidence["reason"] = "快照内容读不到"
        return None, evidence
    state = TaskState.from_json(snapshot)
    evidence["found"] = True
    evidence["snapshot_digest"] = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
    return state, evidence


def summarize_state(state: TaskState | None) -> dict[str, Any]:
    """把状态转成便于展示的摘要。"""
    if state is None:
        return {"present": False, "done": [], "todo": [], "checks": []}
    return {
        "present": True,
        "task_id": state.task_id,
        "phase": state.phase,
        "updated_at": state.updated_at,
        "done": state.done,
        "todo": state.todo,
        "checks": [check.to_dict() for check in state.checks],
        "next_action": state.next_action,
    }

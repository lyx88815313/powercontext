"""端到端流水线：导入 → 检索 → MiniMax 改码 → 测试 → 状态持久化。

一次 ``run`` 会依次完成：

1. 把工程资料导入 seekdb（可选，默认开启）；
2. 依据任务在 seekdb 中检索适用资料并组装开发上下文；
3. 从 PowerContext 读回已有任务状态（空状态时为空）；
4. 运行"修改前"测试，留存基线日志；
5. 把上下文交给 MiniMax 生成补丁并应用，失败则带着测试反馈重试；
6. 运行"修改后"测试；
7. 把任务状态写回 PowerContext。
"""

from __future__ import annotations

import difflib
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .context_builder import render_prompt, render_report
from .importer import ImportResult, import_project
from .minimax_client import MiniMaxCall, MiniMaxClient
from .patcher import PatchError, apply_unified_diff, extract_unified_diff, summarize_diff
from .powercontext_client import PowerContextClient
from .retriever import DevelopmentContext, retrieve_context
from .seekdb_store import SeekdbStore
from .state import CheckResult, TaskState
from .state_store import load_state, save_state, summarize_state
from .taskdef import TaskDefinition

_SUMMARY = re.compile(
    r"(?P<passed>\d+)\s+passed|(?P<failed>\d+)\s+failed|(?P<error>\d+)\s+error"
)


@dataclass
class TestRun:
    """一次 pytest 运行的结果。"""

    command: str
    exit_code: int
    stdout: str
    duration: float
    passed: int = 0
    failed: int = 0
    errors: int = 0
    log_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration, 3),
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "log_path": self.log_path,
        }


@dataclass
class PipelineResult:
    """一次流水线执行的结果汇总。"""

    task: TaskDefinition
    import_result: ImportResult | None = None
    context: DevelopmentContext | None = None
    state_before: TaskState | None = None
    state_after: TaskState | None = None
    test_before: TestRun | None = None
    test_after: TestRun | None = None
    minimax_calls: list[MiniMaxCall] = field(default_factory=list)
    patches: list[dict[str, Any]] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    evidence_dir: str = ""
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task.to_dict(),
            "import": self.import_result.summary() if self.import_result else None,
            "context": self.context.to_dict() if self.context else None,
            "state_before": summarize_state(self.state_before),
            "state_after": summarize_state(self.state_after),
            "test_before": self.test_before.to_dict() if self.test_before else None,
            "test_after": self.test_after.to_dict() if self.test_after else None,
            "minimax_calls": [call.to_dict(include_prompts=False) for call in self.minimax_calls],
            "patches": self.patches,
            "changed_files": self.changed_files,
            "evidence_dir": self.evidence_dir,
            "errors": self.errors,
        }


def run_pytest(
    project_root: Path,
    *,
    tests_dir: str = "tests",
    python: str | None = None,
    log_path: Path | None = None,
    label: str = "pytest",
) -> TestRun:
    """在业务工程里运行 pytest。"""
    interpreter = python or sys.executable
    command = [interpreter, "-m", "pytest", tests_dir, "-v", "--tb=short"]
    started = time.time()
    process = subprocess.run(
        command,
        cwd=str(project_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    duration = time.time() - started
    output = (process.stdout or "") + (process.stderr or "")
    passed = failed = errors = 0
    for match in _SUMMARY.finditer(output):
        if match.group("passed"):
            passed = int(match.group("passed"))
        if match.group("failed"):
            failed = int(match.group("failed"))
        if match.group("error"):
            errors = int(match.group("error"))
    run = TestRun(
        command=" ".join(command),
        exit_code=process.returncode,
        stdout=output,
        duration=duration,
        passed=passed,
        failed=failed,
        errors=errors,
    )
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            f"# command: {run.command}\n"
            f"# cwd: {project_root}\n"
            f"# exit_code: {run.exit_code}\n"
            f"# duration_seconds: {round(duration, 3)}\n"
            f"# label: {label}\n"
            f"# timestamp: {datetime.now().isoformat(timespec='seconds')}\n\n"
        )
        log_path.write_text(header + output, encoding="utf-8", newline="")
        run.log_path = str(log_path)
    return run


def _snapshot_files(paths: list[str], project_root: Path) -> dict[str, str]:
    """备份待改文件的当前内容（重试前用于回滚）。"""
    snapshot: dict[str, str] = {}
    for rel in paths:
        target = project_root / rel
        if target.exists():
            snapshot[rel] = target.read_text(encoding="utf-8")
    return snapshot


def _restore_files(snapshot: dict[str, str], project_root: Path) -> None:
    """回滚文件到快照状态。"""
    for rel, content in snapshot.items():
        target = project_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")


def run_pipeline(
    settings: Settings,
    task: TaskDefinition,
    *,
    project_root: Path,
    evidence_dir: Path,
    do_import: bool = True,
    do_patch: bool = True,
    do_test: bool = True,
    max_attempts: int = 3,
) -> PipelineResult:
    """执行一次完整流水线。"""
    project_root = project_root.resolve()
    evidence_dir = evidence_dir.resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    result = PipelineResult(task=task, evidence_dir=str(evidence_dir))

    # 每次运行生成一份独立的 PowerContext 请求审计日志
    request_log = evidence_dir / "powercontext_requests.jsonl"
    request_log.write_text("", encoding="utf-8", newline="")

    # ---------------------------------------------------------------- 1. 导入
    store = SeekdbStore(settings)
    try:
        if do_import:
            import_result = import_project(store, project_root)
            result.import_result = import_result
            _write_json(evidence_dir / "import.json", import_result.summary())

        # ---------------------------------------------------------- 2. 检索上下文
        context = retrieve_context(store, task, settings)
        result.context = context
        _write_json(evidence_dir / "context.json", context.to_dict())
        if context.missing_sources:
            result.errors.append(
                "以下必需来源在 seekdb 中不存在：" + ", ".join(context.missing_sources)
            )

        # ------------------------------------------------- 3. 读回已有任务状态
        pc_client = PowerContextClient(settings, evidence_log=evidence_dir / "powercontext_requests.jsonl")
        try:
            state_before, state_evidence = load_state(pc_client, task.task_id)
            result.state_before = state_before
            _write_json(evidence_dir / "state_before.json", state_evidence)
            _write_text(evidence_dir / "context.md", render_report(context, state_before))

            # --------------------------------------------------- 4. 修改前测试基线
            if do_test:
                before = run_pytest(
                    project_root,
                    log_path=evidence_dir / "test_before.log",
                    label="before-change",
                )
                result.test_before = before
                _write_json(evidence_dir / "test_before.json", before.to_dict())

            # ------------------------------------------------ 5. MiniMax 生成补丁
            if do_patch and not context.missing_sources:
                client = MiniMaxClient(settings)
                target_paths = sorted({item.path for item in context.codes})
                snapshot = _snapshot_files(target_paths, project_root)
                _write_code_snapshots(evidence_dir / "diff", snapshot, project_root, suffix=".before")
                feedback = ""

                for attempt in range(1, max_attempts + 1):
                    prompt = render_prompt(context, state=state_before, feedback=feedback)
                    try:
                        call = client.complete(render_prompt_system(), prompt)
                    except Exception as exc:
                        result.errors.append(f"MiniMax 调用失败：{exc}")
                        break
                    result.minimax_calls.append(call)
                    _write_json(
                        evidence_dir / f"minimax_call_{attempt}.json", call.to_dict()
                    )

                    try:
                        diff = extract_unified_diff(call.response_text)
                        summary = summarize_diff(diff)
                    except PatchError as exc:
                        result.errors.append(f"第 {attempt} 次补丁解析失败：{exc}")
                        feedback = f"上一次输出无法解析为 unified diff：{exc}"
                        _write_text(evidence_dir / f"patch_{attempt}.diff", call.response_text)
                        continue

                    _write_text(evidence_dir / f"patch_{attempt}.diff", diff)
                    try:
                        changed = apply_unified_diff(diff, project_root)
                    except PatchError as exc:
                        result.errors.append(f"第 {attempt} 次补丁应用失败：{exc}")
                        feedback = f"上一次补丁无法应用：{exc}"
                        continue

                    result.changed_files = changed
                    result.patches.append({"attempt": attempt, **summary})
                    _write_code_snapshots(
                        evidence_dir / "diff", snapshot, project_root, suffix=".after"
                    )
                    _write_text(evidence_dir / "diff" / "changes.diff", diff)

                    if not do_test:
                        break

                    test_run = run_pytest(
                        project_root,
                        log_path=evidence_dir / f"test_after_attempt_{attempt}.log",
                        label=f"after-change-attempt-{attempt}",
                    )
                    if test_run.exit_code == 0:
                        result.test_after = test_run
                        _write_json(evidence_dir / "test_after.json", test_run.to_dict())
                        break

                    result.errors.append(
                        f"第 {attempt} 次修改后测试未通过（exit={test_run.exit_code}）"
                    )
                    feedback = _build_feedback(test_run)
                    _restore_files(snapshot, project_root)
                else:
                    if result.test_after is None and do_test:
                        result.errors.append("所有尝试均未通过测试")
        finally:
            pc_client.close()
    finally:
        store.close()

    # -------------------------------------------------------- 6. 写回任务状态
    state_after = result.state_after or _build_state_after(result)
    result.state_after = state_after
    pc_client = PowerContextClient(settings, evidence_log=evidence_dir / "powercontext_requests.jsonl")
    try:
        receipt = save_state(pc_client, state_after)
        _write_json(evidence_dir / "state_save.json", receipt.to_dict())
        if receipt.outcome_error:
            result.errors.append("结构化任务结果写入失败：" + receipt.outcome_error)
    except Exception as exc:
        result.errors.append(f"任务状态写入失败：{type(exc).__name__}: {exc}")
    finally:
        pc_client.close()

    _write_json(evidence_dir / "pipeline_result.json", result.to_dict())
    return result


def render_prompt_system() -> str:
    """返回系统提示词。"""
    from .context_builder import SYSTEM_PROMPT

    return SYSTEM_PROMPT


def _build_feedback(test_run: TestRun) -> str:
    """把失败测试输出整理成给模型的反馈。"""
    lines = test_run.stdout.splitlines()
    tail = lines[-120:] if len(lines) > 120 else lines
    return (
        "上一次修改没有通过测试，请修正后重新输出完整的 unified diff。\n"
        f"命令：{test_run.command}\n"
        f"退出码：{test_run.exit_code}\n\n"
        "```text\n" + "\n".join(tail) + "\n```"
    )


def _build_state_after(result: PipelineResult) -> TaskState:
    """根据流水线结果生成要保存的任务状态。"""
    task = result.task
    state = TaskState(
        task_id=task.task_id,
        title=task.title,
        objective=task.objective,
        phase="patched" if result.changed_files else "contexted",
    )
    if result.import_result:
        state.mark_done(
            f"导入工程资料到 seekdb（规格 {len(result.import_result.spec_items)} 条、"
            f"代码 {len(result.import_result.code_units)} 个、"
            f"测试 {len(result.import_result.test_units)} 个）"
        )
    if result.context:
        state.mark_done(
            "在 seekdb 中检索并组装开发上下文（digest="
            f"{result.context.digest()[:16]}）"
        )
        state.applied_spec_items = [item.unit_id for item in result.context.specs]
        state.verification_tests = [item.unit_id for item in result.context.tests]
        state.context_digest = result.context.digest()
    if result.test_before is not None:
        state.mark_done(
            f"记录修改前测试基线（exit={result.test_before.exit_code}，"
            f"failed={result.test_before.failed}）"
        )
    if result.minimax_calls:
        state.mark_done(
            f"MiniMax 生成补丁 {len(result.minimax_calls)} 次"
            f"（模型 {result.minimax_calls[-1].model}）"
        )
    if result.changed_files:
        state.changed_code_units = result.changed_files
        state.mark_done("应用 MiniMax 补丁：" + ", ".join(result.changed_files))
        state.produced_artifacts.extend(result.changed_files)
    if result.test_after is not None:
        state.add_check(
            CheckResult(
                name="pytest-after-change",
                status="passed" if result.test_after.exit_code == 0 else "failed",
                details=(
                    f"passed={result.test_after.passed} failed={result.test_after.failed} "
                    f"errors={result.test_after.errors}"
                ),
                command=result.test_after.command,
                exit_code=result.test_after.exit_code,
            )
        )
        if result.test_after.exit_code == 0:
            state.mark_done("修改后目标测试与回归测试全部通过")
            state.next_action = "提交代码差异与验证材料"
        else:
            state.next_action = "根据失败反馈继续修正"
    else:
        state.next_action = "运行 MiniMax 代码修改"
    if result.test_before is not None and result.test_before.exit_code != 0:
        state.add_check(
            CheckResult(
                name="pytest-before-change",
                status="failed",
                details=(
                    f"passed={result.test_before.passed} failed={result.test_before.failed} "
                    f"errors={result.test_before.errors}（目标行为未实现，符合预期）"
                ),
                command=result.test_before.command,
                exit_code=result.test_before.exit_code,
            )
        )
    if result.errors:
        state.notes.extend(result.errors)
    return state


def _write_json(path: Path, payload: Any) -> None:
    """写 JSON 文件（固定 LF 行尾，避免跨平台差异）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
        newline="",
    )


def _write_code_snapshots(
    diff_dir: Path,
    snapshot: dict[str, str],
    project_root: Path,
    *,
    suffix: str,
) -> None:
    """保存代码文件的修改前 / 修改后快照。"""
    diff_dir.mkdir(parents=True, exist_ok=True)
    for rel in sorted(snapshot):
        target = diff_dir / (rel.replace("/", "__") + suffix)
        if suffix == ".before":
            target.write_text(snapshot[rel], encoding="utf-8", newline="")
            continue
        current = project_root / rel
        if not current.exists():
            continue
        after = current.read_text(encoding="utf-8")
        target.write_text(after, encoding="utf-8", newline="")
        unified = difflib.unified_diff(
            snapshot[rel].splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )
        (diff_dir / (rel.replace("/", "__") + ".diff")).write_text(
            "".join(unified), encoding="utf-8", newline=""
        )


def _write_text(path: Path, text: str) -> None:
    """写文本文件（固定 LF 行尾）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")

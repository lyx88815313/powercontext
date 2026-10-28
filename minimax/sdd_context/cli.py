"""命令行入口。

```
python -m sdd_context doctor
python -m sdd_context import   --project minimax/demo
python -m sdd_context context  --task minimax/demo/tasks/TASK-001-status-filter.md
python -m sdd_context run      --task minimax/demo/tasks/TASK-001-status-filter.md
python -m sdd_context state load  --task TASK-001
python -m sdd_context state save  --task TASK-001 --phase patched
python -m sdd_context state reset --task TASK-001
```
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import Settings, load_settings
from .context_builder import render_report
from .importer import import_project
from .minimax_client import MiniMaxClient
from .powercontext_client import PowerContextClient
from .retriever import retrieve_context
from .seekdb_store import SeekdbStore
from .state import TaskState
from .state_store import load_state, save_state, summarize_state
from .taskdef import load_task

DEFAULT_PROJECT = Path("minimax/demo")
DEFAULT_EVIDENCE = Path("minimax/evidence")


def _resolve(path_text: str) -> Path:
    return Path(path_text).expanduser().resolve()


def _dump(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _settings(args: argparse.Namespace) -> Settings:
    settings = load_settings()
    if getattr(args, "evidence_dir", None):
        settings.evidence_dir = _resolve(args.evidence_dir)
    if getattr(args, "scope_key", None):
        settings.powercontext_scope_key = args.scope_key
    return settings


# ---------------------------------------------------------------------- 子命令


def cmd_doctor(args: argparse.Namespace) -> int:
    """检查三项依赖（seekdb / PowerContext / MiniMax）是否就绪。"""
    settings = _settings(args)
    report: dict[str, Any] = {"config": settings.as_dict()}

    store = SeekdbStore(settings)
    try:
        store.ensure_schema()
        report["seekdb"] = {
            "ok": True,
            "server_version": store.server_version(),
            "database": settings.seekdb_database,
            "fulltext_available": store.fulltext_available,
            "row_counts": {
                "spec_item": store.count_rows("spec_item"),
                "code_unit": store.count_rows("code_unit"),
                "test_unit": store.count_rows("test_unit"),
            },
        }
    except Exception as exc:
        report["seekdb"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        store.close()

    client = PowerContextClient(settings)
    try:
        health = client.health()
        scope_id = client.ensure_scope()
        report["powercontext"] = {
            "ok": True,
            "url": settings.powercontext_url,
            "scope_id": scope_id,
            "ready": health.get("ready", {}).get("status"),
        }
    except Exception as exc:
        report["powercontext"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        client.close()

    minimax = MiniMaxClient(settings)
    report["minimax"] = {
        "ok": bool(minimax.base_url and minimax.api_key),
        "base_url": minimax.base_url,
        "model": minimax.model,
        "protocol": minimax.protocol,
        "source": settings.minimax_source,
        "error": "" if (minimax.base_url and minimax.api_key) else "缺少 MINIMAX_API_KEY / MINIMAX_BASE_URL",
    }

    _dump(report)
    return 0 if report["seekdb"].get("ok") and report["powercontext"].get("ok") else 1


def cmd_import(args: argparse.Namespace) -> int:
    """把业务工程的规格 / 代码 / 测试导入 seekdb。"""
    settings = _settings(args)
    project = _resolve(args.project)
    store = SeekdbStore(settings)
    try:
        result = import_project(store, project, project_name=args.project_name)
        _dump(result.summary())
        return 0
    finally:
        store.close()


def cmd_context(args: argparse.Namespace) -> int:
    """检索并渲染开发上下文。"""
    settings = _settings(args)
    task = load_task(_resolve(args.task))
    store = SeekdbStore(settings)
    try:
        context = retrieve_context(store, task, settings)
    finally:
        store.close()

    state = None
    if not args.no_state:
        client = PowerContextClient(settings)
        try:
            state, _ = load_state(client, task.task_id)
        finally:
            client.close()

    report = render_report(context, state)
    if args.output:
        out = _resolve(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"context written to {out}")
    else:
        print(report)
    if context.missing_sources:
        print("\n[警告] 以下必需来源在 seekdb 中不存在：", ", ".join(context.missing_sources))
        return 1
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """执行完整流水线。"""
    from .pipeline import run_pipeline

    settings = _settings(args)
    task = load_task(_resolve(args.task))
    project = _resolve(args.project) if args.project else _resolve(args.project)
    result = run_pipeline(
        settings,
        task,
        project_root=project,
        evidence_dir=settings.evidence_dir,
        do_import=not args.no_import,
        do_patch=not args.no_patch,
        do_test=not args.no_test,
        max_attempts=args.max_attempts,
    )
    _dump(
        {
            "task": result.task.task_id,
            "changed_files": result.changed_files,
            "test_before": result.test_before.to_dict() if result.test_before else None,
            "test_after": result.test_after.to_dict() if result.test_after else None,
            "minimax_calls": len(result.minimax_calls),
            "state_after": summarize_state(result.state_after),
            "errors": result.errors,
            "evidence_dir": result.evidence_dir,
        }
    )
    return 0 if result.test_after is not None and result.test_after.exit_code == 0 else 1


def cmd_test(args: argparse.Namespace) -> int:
    """运行业务工程的测试。"""
    from .pipeline import run_pytest

    project = _resolve(args.project)
    log = _resolve(args.log) if args.log else None
    run = run_pytest(project, log_path=log, label="manual")
    _dump(run.to_dict())
    if run.exit_code != 0:
        print(run.stdout[-4000:])
    return run.exit_code


def cmd_state_load(args: argparse.Namespace) -> int:
    """从 PowerContext 读回任务状态。"""
    settings = _settings(args)
    client = PowerContextClient(settings)
    try:
        state, evidence = load_state(client, args.task)
        _dump({"state": summarize_state(state), "evidence": evidence})
        return 0 if state is not None else 2
    finally:
        client.close()


def cmd_state_save(args: argparse.Namespace) -> int:
    """把任务状态写入 PowerContext。"""
    settings = _settings(args)
    client = PowerContextClient(settings)
    try:
        payload: dict[str, Any] = {}
        if args.state_file:
            payload = json.loads(_resolve(args.state_file).read_text(encoding="utf-8"))
        state = TaskState.from_dict(
            {
                "task_id": args.task,
                "title": args.title or "",
                "objective": args.objective or "",
                "phase": args.phase,
                "done": args.done or [],
                "todo": args.todo or [],
                **payload,
            }
        )
        receipt = save_state(client, state)
        _dump(receipt.to_dict())
        return 0
    finally:
        client.close()


def cmd_state_reset(args: argparse.Namespace) -> int:
    """把任务状态重置为空（新建一个 scope）。

    新的 scope 里没有任何本任务的记忆条目与快照，因此"从空状态开始"成立。
    """
    settings = _settings(args)
    scope_key = args.scope_key or f"{args.task}-reset-{args.epoch}"
    settings.powercontext_scope_key = scope_key
    client = PowerContextClient(settings)
    try:
        scope_id = client.ensure_scope()
        state, evidence = load_state(client, args.task)
        _dump(
            {
                "reset_scope_key": scope_key,
                "scope_id": scope_id,
                "state": summarize_state(state),
                "evidence": evidence,
                "hint": (
                    "后续运行请设置 SDD_POWERCONTEXT_SCOPE_KEY="
                    f"{scope_key}，或删除 PowerContext 数据目录后重启服务，回到绝对空状态。"
                ),
            }
        )
        return 0
    finally:
        client.close()


# ---------------------------------------------------------------------- 解析器


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="sdd_context", description="SDD 上下文模块（MiniMax × seekdb × PowerContext）"
    )
    parser.add_argument("--evidence-dir", default=str(DEFAULT_EVIDENCE), help="证据输出目录")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="检查 seekdb / PowerContext / MiniMax 就绪情况")
    doctor.set_defaults(func=cmd_doctor)

    imp = sub.add_parser("import", help="把工程资料导入 seekdb")
    imp.add_argument("--project", default=str(DEFAULT_PROJECT), help="业务工程根目录")
    imp.add_argument("--project-name", default="fulfillment-svc", help="工程名（写入导入清单）")
    imp.set_defaults(func=cmd_import)

    ctx = sub.add_parser("context", help="检索并渲染开发上下文")
    ctx.add_argument("--task", required=True, help="任务定义文件")
    ctx.add_argument("--project", default=str(DEFAULT_PROJECT), help="业务工程根目录")
    ctx.add_argument("--output", default="", help="把报告写到文件")
    ctx.add_argument("--no-state", action="store_true", help="不读取 PowerContext 任务状态")
    ctx.set_defaults(func=cmd_context)

    run = sub.add_parser("run", help="执行完整流水线（导入→上下文→改码→测试→状态）")
    run.add_argument("--task", required=True, help="任务定义文件")
    run.add_argument("--project", default=str(DEFAULT_PROJECT), help="业务工程根目录")
    run.add_argument("--no-import", action="store_true", help="跳过导入")
    run.add_argument("--no-patch", action="store_true", help="跳过 MiniMax 代码修改")
    run.add_argument("--no-test", action="store_true", help="跳过测试")
    run.add_argument("--max-attempts", type=int, default=3, help="MiniMax 最多尝试次数")
    run.set_defaults(func=cmd_run)

    test = sub.add_parser("test", help="运行业务工程测试")
    test.add_argument("--project", default=str(DEFAULT_PROJECT), help="业务工程根目录")
    test.add_argument("--log", default="", help="把输出写入日志文件")
    test.set_defaults(func=cmd_test)

    state = sub.add_parser("state", help="任务状态（PowerContext）")
    state_sub = state.add_subparsers(dest="state_command", required=True)

    state_load = state_sub.add_parser("load", help="读回任务状态")
    state_load.add_argument("--task", required=True)
    state_load.set_defaults(func=cmd_state_load)

    state_save = state_sub.add_parser("save", help="写入任务状态")
    state_save.add_argument("--task", required=True)
    state_save.add_argument("--title", default="")
    state_save.add_argument("--objective", default="")
    state_save.add_argument("--phase", default="manual")
    state_save.add_argument("--done", action="append", default=[])
    state_save.add_argument("--todo", action="append", default=[])
    state_save.add_argument("--state-file", default="", help="额外状态字段的 JSON 文件")
    state_save.set_defaults(func=cmd_state_save)

    state_reset = state_sub.add_parser("reset", help="重置为空的 scope")
    state_reset.add_argument("--task", required=True)
    state_reset.add_argument("--scope-key", default="", help="新 scope 的 idempotency key")
    state_reset.add_argument("--epoch", default="1", help="重置批次号（用于生成不同的 scope key）")
    state_reset.set_defaults(func=cmd_state_reset)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except Exception as exc:
        print(f"[error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

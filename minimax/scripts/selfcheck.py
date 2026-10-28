"""六项必做项的自检脚本。

逐项核对题目里的验收条件并打印结论，供提交前自查与评审复核。

用法::

    python minimax/scripts/selfcheck.py

依赖：seekdb / PowerContext / MiniMax 三者都要就绪（先跑 ``python -m sdd_context doctor``），
证据文件要放在 ``minimax/evidence/``（由 ``sdd_context run`` 生成）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

MINIMAX = Path(__file__).resolve().parents[1]
REPO = MINIMAX.parent
EV = MINIMAX / "evidence"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def main() -> int:
    """执行六项必做项自检。"""
    # ---------------------------------------------------------- 1 能够运行
    env = {**os.environ, "PYTHONPATH": str(MINIMAX)}
    doctor = subprocess.run(
        [sys.executable, "-m", "sdd_context", "doctor"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    try:
        doc = json.loads(doctor.stdout)
        ok = doc["seekdb"]["ok"] and doc["powercontext"]["ok"] and doc["minimax"]["ok"]
        detail = (
            f"seekdb={doc['seekdb']['server_version']} scope={doc['powercontext']['scope_id']} "
            f"minimax={doc['minimax']['model']}({doc['minimax']['protocol']})"
        )
    except Exception as exc:  # pragma: no cover
        ok, detail = False, f"doctor 输出无法解析：{exc}"
    check("能够运行", ok, detail)

    # ------------------------------------------------ 2 MiniMax 实际参与
    call = load_json(EV / "minimax_call_1.json")
    patch = (EV / "patch_1.diff").read_text(encoding="utf-8")
    committed = (MINIMAX / "demo" / "src" / "fulfillment" / "query.py").read_text(
        encoding="utf-8"
    )
    ok = (
        call["model"].startswith("MiniMax")
        and "@@" in patch
        and "src/fulfillment/query.py" in patch
        and "InvalidFilterError" in committed
        and "ALLOWED_STATUSES" in committed
    )
    check(
        "MiniMax 实际参与",
        ok,
        f"model={call['model']} calls={len(call.get('request_body', {}) and [1] or [])} "
        f"patch={len(patch)}B -> src/fulfillment/query.py",
    )

    # ---------------------------------------------------- 3 上下文正确
    ctx = load_json(EV / "context.json")
    report = (EV / "context.md").read_text(encoding="utf-8")
    required_spec = ["SPEC-ORDER-QUERY-01", "SPEC-ORDER-QUERY-02", "SPEC-ORDER-QUERY-05"]
    required_code = ["src/fulfillment/query.py", "list_orders"]
    required_test = [
        "tests/test_query_target.py",
        "tests/test_query_regression.py",
        "tests/test_pricing.py",
    ]
    ok = (
        not ctx["missing_sources"]
        and all(s in report for s in required_spec)
        and all(c in report for c in required_code)
        and all(t in report for t in required_test)
    )
    check(
        "上下文正确",
        ok,
        f"规格 {len(ctx['specs'])} 条 / 代码 {len(ctx['codes'])} 个 / 测试 {len(ctx['tests'])} 个，"
        f"缺失来源 {ctx['missing_sources'] or '无'}",
    )

    spec_update = (EV / "spec-update" / "README.md").read_text(encoding="utf-8")
    ok_update = "更新前后上下文是否不同：**是**" in spec_update and (
        "还原后是否与更新前一致：**是**" in spec_update
    )
    check("规格更新后使用新内容", ok_update, "更新前/更新后上下文 digest 不同，还原后回到原 digest")

    # ---------------------------------------------- 4 数据库实际参与
    db_log = (EV / "db-queries.log").read_text(encoding="utf-8")
    counts = ctx["db_evidence"]["row_counts"]
    ok = (
        counts["spec_item"] > 0
        and "MATCH(body_tokens) AGAINST" in db_log
        and ctx["db_evidence"]["retrieval_mode"]["spec_item"] == "fulltext"
    )
    check(
        "数据库实际参与",
        ok,
        f"spec_item={counts['spec_item']} code_unit={counts['code_unit']} "
        f"test_unit={counts['test_unit']}，检索方式=fulltext",
    )

    # ------------------------------------------------ 5 状态能够复用
    before = load_json(EV / "state_before.json")
    after = load_json(EV / "state_after_restart.json")
    save = load_json(EV / "state_save.json")
    ok = (
        before["found"] is False
        and before["scope_id"] == after["evidence"]["scope_id"]
        and after["state"]["present"] is True
        and len(after["state"]["done"]) > 0
        and len(after["state"]["checks"]) > 0
        and not save.get("outcome_error")
    )
    check(
        "状态能够复用",
        ok,
        f"空状态 found={before['found']} -> 新进程读回 present={after['state']['present']}，"
        f"已完成 {len(after['state']['done'])} 项 / 验证 {len(after['state']['checks'])} 项",
    )

    # ---------------------------------------------- 6 代码修改有效
    before_test = load_json(EV / "test_before.json")
    after_test = load_json(EV / "test_after.json")
    ok = (
        before_test["exit_code"] != 0
        and before_test["failed"] == 5
        and before_test["errors"] == 0
        and after_test["exit_code"] == 0
        and after_test["passed"] == 17
    )
    check(
        "代码修改有效",
        ok,
        f"修改前 exit={before_test['exit_code']} "
        f"(passed={before_test['passed']} failed={before_test['failed']})"
        f" -> 修改后 exit={after_test['exit_code']} (passed={after_test['passed']})",
    )

    print("=" * 78)
    print("六项必做项自检")
    print("=" * 78)
    for name, ok, detail in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    print("=" * 78)
    all_ok = all(ok for _, ok, _ in results)
    print(f"结论：{'全部通过' if all_ok else '存在未通过项'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

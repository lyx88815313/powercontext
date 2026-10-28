"""分词、规格解析、任务定义与状态模型的自测。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sdd_context.importer import parse_spec_file  # noqa: E402
from sdd_context.state import CheckResult, TaskState  # noqa: E402
from sdd_context.state_store import build_outcome  # noqa: E402
from sdd_context.taskdef import load_task  # noqa: E402
from sdd_context.tokenize import index_text, query_tokens, tokenize  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO = REPO_ROOT / "minimax" / "demo"


def test_tokenize_splits_chinese_into_chars_and_bigrams():
    tokens = tokenize("订单状态筛选")
    assert "订" in tokens
    assert "订单" in tokens
    assert "状态" in tokens


def test_index_text_is_space_separated():
    text = index_text("订单状态筛选")
    assert " " in text
    assert "订单" in text.split()


def test_query_tokens_deduplicates_and_drops_stopwords():
    tokens = query_tokens("订单 订单 the filter")
    assert tokens.count("订单") == 1
    assert "the" not in tokens


def test_parse_spec_file_extracts_items():
    items = parse_spec_file(DEMO / "specs" / "SPEC-ORDER-QUERY.md", DEMO)
    ids = [item["item_id"] for item in items]
    assert "SPEC-ORDER-QUERY-01" in ids
    assert "SPEC-ORDER-QUERY-05" in ids
    first = items[0]
    assert first["spec_id"] == "SPEC-ORDER-QUERY"
    assert first["version"] == "1.2.0"
    assert first["source_path"] == "specs/SPEC-ORDER-QUERY.md"
    assert "InvalidFilterError" in first["body"]


def test_load_task_reads_frontmatter():
    task = load_task(DEMO / "tasks" / "TASK-001-status-filter.md")
    assert task.task_id == "TASK-001"
    assert "SPEC-ORDER-QUERY-01" in task.required_spec_items
    assert "src/fulfillment/query.py::list_orders" in task.required_code_units
    assert any(t.startswith("tests/test_query_target.py") for t in task.required_test_units)
    assert task.objective


def test_task_state_roundtrip():
    state = TaskState(task_id="T-1", title="t", objective="o")
    state.mark_done("第一步")
    state.mark_todo("第二步")
    state.add_check(CheckResult(name="pytest", status="passed", details="d", exit_code=0))
    restored = TaskState.from_json(state.to_json())
    assert restored.done == ["第一步"]
    assert restored.todo == ["第二步"]
    assert restored.checks[0].status == "passed"
    assert restored.checks[0].exit_code == 0


def test_state_mark_done_is_idempotent():
    state = TaskState(task_id="T-1")
    state.mark_done("x")
    state.mark_done("x")
    assert state.done == ["x"]


def test_build_outcome_satisfies_powercontext_contract():
    """observations 至少一条；produced_artifacts 只允许引用真实构件，这里留空。"""
    state = TaskState(task_id="T-1", title="t", objective="o")
    outcome = build_outcome(state)
    assert len(outcome["observations"]) >= 1
    assert outcome["produced_artifacts"] == []
    assert outcome["schema"] == "powercontext.task-outcome.v1"
    assert outcome["trust"] == "untrusted_observation"

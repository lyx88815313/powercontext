"""unified diff 解析与应用的自测。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sdd_context.patcher import (  # noqa: E402
    PatchError,
    apply_hunks,
    apply_unified_diff,
    extract_unified_diff,
    parse_unified_diff,
    summarize_diff,
)

ORIGINAL = """line one
line two
line three
line four
"""


def test_extract_diff_from_fenced_block():
    text = """这是模型的回复：

```diff
--- a/x.py
+++ b/x.py
@@ -1,2 +1,2 @@
 line one
-line two
+LINE TWO
```

以上。
"""
    diff = extract_unified_diff(text)
    assert diff.startswith("--- a/x.py")
    assert "@@" in diff


def test_extract_diff_without_fence():
    text = "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n-line one\n+LINE ONE\n"
    assert "--- a/x.py" in extract_unified_diff(text)


def test_extract_diff_raises_when_absent():
    with pytest.raises(PatchError):
        extract_unified_diff("这里没有任何补丁。")


def test_parse_unified_diff_structure():
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n line one\n-line two\n+LINE TWO\n"
    patches = parse_unified_diff(diff)
    assert len(patches) == 1
    assert patches[0].target_path == "x.py"
    assert patches[0].hunks[0].old_start == 1
    assert len(patches[0].hunks[0].lines) == 3


def test_apply_hunks_exact_match():
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n line one\n-line two\n+LINE TWO\n"
    patch = parse_unified_diff(diff)[0]
    assert apply_hunks(ORIGINAL, patch) == "line one\nLINE TWO\nline three\nline four\n"


def test_apply_hunks_tolerates_line_offset():
    """模型偶尔把 @@ 起始行号写偏；上下文一致时应当允许整体偏移。"""
    diff = "--- a/x.py\n+++ b/x.py\n@@ -9,2 +9,2 @@\n line two\n-line three\n+LINE THREE\n"
    patch = parse_unified_diff(diff)[0]
    assert apply_hunks(ORIGINAL, patch) == "line one\nline two\nLINE THREE\nline four\n"


def test_apply_hunks_rejects_real_mismatch():
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n nope\n-line two\n+LINE TWO\n"
    patch = parse_unified_diff(diff)[0]
    with pytest.raises(PatchError):
        apply_hunks(ORIGINAL, patch)


def test_apply_unified_diff_to_disk(tmp_path: Path):
    target = tmp_path / "x.py"
    target.write_text(ORIGINAL, encoding="utf-8")
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n line one\n-line two\n+LINE TWO\n"
    changed = apply_unified_diff(diff, tmp_path)
    assert changed == ["x.py"]
    assert target.read_text(encoding="utf-8") == "line one\nLINE TWO\nline three\nline four\n"


def test_summarize_diff_counts():
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n line one\n-line two\n+LINE TWO\n"
    summary = summarize_diff(diff)
    assert summary["files"] == ["x.py"]
    assert summary["added_lines"] == 1
    assert summary["removed_lines"] == 1

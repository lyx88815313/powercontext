"""工程资料导入器。

把业务工程里的三类资料解析成结构化单元并写入 seekdb：

* ``specs/*.md``  → 规格条目（每个二级标题是一个可独立引用的条目）
* ``src/**.py``   → 代码文件 + 代码符号（函数 / 类）
* ``tests/**.py`` → 测试文件 + 测试用例

导入是幂等的：内容未变化时不会产生新版本；规格内容变化后重新导入，
数据库里的条目会被覆盖为新内容，检索返回的即是更新后的版本。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .seekdb_store import SeekdbStore, sha256_text

_H2 = re.compile(r"^##\s+(?P<item_id>[A-Za-z0-9_.\-]+)\s*(?P<title>.*)$")
_META_ROW = re.compile(r"^\|\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*\|\s*(?P<value>[^|]*?)\s*\|")
_H1 = re.compile(r"^#\s+(?P<title>.+)$")


@dataclass
class ImportResult:
    """一次导入的结果汇总。"""

    batch_id: str
    project: str
    source_root: str
    spec_items: list[dict[str, Any]] = field(default_factory=list)
    code_units: list[dict[str, Any]] = field(default_factory=list)
    test_units: list[dict[str, Any]] = field(default_factory=list)
    changed_spec_items: list[str] = field(default_factory=list)
    changed_code_units: list[str] = field(default_factory=list)
    changed_test_units: list[str] = field(default_factory=list)
    removed_spec_items: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """返回可写入证据文件的摘要。"""
        return {
            "batch_id": self.batch_id,
            "project": self.project,
            "source_root": self.source_root,
            "imported_at": datetime.now().isoformat(timespec="seconds"),
            "spec_items": len(self.spec_items),
            "code_units": len(self.code_units),
            "test_units": len(self.test_units),
            "changed_spec_items": self.changed_spec_items,
            "changed_code_units": self.changed_code_units,
            "changed_test_units": self.changed_test_units,
            "removed_spec_items": self.removed_spec_items,
            "spec_item_ids": [f"{s['spec_id']}:{s['item_id']}" for s in self.spec_items],
            "code_unit_ids": [c["unit_id"] for c in self.code_units],
            "test_unit_ids": [t["unit_id"] for t in self.test_units],
        }


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def parse_spec_file(path: Path, root: Path) -> list[dict[str, Any]]:
    """解析一份规格 Markdown，返回规格条目列表。"""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    meta: dict[str, str] = {}
    for line in lines:
        match = _META_ROW.match(line.strip())
        if match:
            meta[match.group("key").lower()] = match.group("value").strip()
        elif line.strip().startswith("|"):
            continue

    spec_id = meta.get("spec_id", "")
    if not spec_id:
        for line in lines:
            h1 = _H1.match(line.strip())
            if h1:
                spec_id = h1.group("title").split()[0]
                break
    version = meta.get("version", "0.0.0")
    tags = ",".join(
        part for part in (meta.get("owner"), meta.get("status"), meta.get("applies_to")) if part
    )
    source_path = _relative(path, root)

    items: list[dict[str, Any]] = []
    current_id: str | None = None
    current_title = ""
    buffer: list[str] = []

    def flush() -> None:
        if current_id is None:
            return
        body = "\n".join(buffer).strip()
        items.append(
            {
                "spec_id": spec_id,
                "item_id": current_id,
                "version": version,
                "title": current_title.strip(),
                "body": body,
                "tags": tags,
                "source_path": source_path,
            }
        )

    for line in lines:
        h2 = _H2.match(line.strip())
        if h2:
            flush()
            current_id = h2.group("item_id")
            current_title = h2.group("title").strip()
            buffer = []
            continue
        if current_id is not None:
            buffer.append(line)
    flush()
    return items


def _source_segment(node: ast.AST, source: str) -> str:
    """取 AST 节点的源码片段（取不到时退回 ``ast.unparse``）。"""
    segment = ast.get_source_segment(source, node)
    if segment:
        return segment
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - 极端情况
        return ""


def parse_code_file(path: Path, root: Path) -> list[dict[str, Any]]:
    """解析一份 Python 源码，返回文件级 + 符号级代码单元。"""
    source = path.read_text(encoding="utf-8")
    rel_path = _relative(path, root)
    units: list[dict[str, Any]] = [
        {
            "unit_id": rel_path,
            "path": rel_path,
            "symbol": "",
            "kind": "file",
            "language": "python",
            "body": source,
            "source_path": rel_path,
        }
    ]
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return units
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "function"
        elif isinstance(node, ast.ClassDef):
            kind = "class"
        else:
            continue
        symbol = node.name
        units.append(
            {
                "unit_id": f"{rel_path}::{symbol}",
                "path": rel_path,
                "symbol": symbol,
                "kind": kind,
                "language": "python",
                "body": _source_segment(node, source),
                "source_path": rel_path,
            }
        )
    return units


def parse_test_file(path: Path, root: Path) -> list[dict[str, Any]]:
    """解析一份测试文件，返回文件级 + 用例级测试单元。"""
    source = path.read_text(encoding="utf-8")
    rel_path = _relative(path, root)
    units: list[dict[str, Any]] = [
        {
            "unit_id": rel_path,
            "path": rel_path,
            "test_name": "",
            "kind": "test_file",
            "body": source,
            "source_path": rel_path,
        }
    ]
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return units
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            units.append(
                {
                    "unit_id": f"{rel_path}::{node.name}",
                    "path": rel_path,
                    "test_name": node.name,
                    "kind": "test_function",
                    "body": _source_segment(node, source),
                    "source_path": rel_path,
                }
            )
    return units


def _iter_files(root: Path, patterns: Iterable[str]) -> list[Path]:
    found: list[Path] = []
    for pattern in patterns:
        found.extend(sorted(root.glob(pattern)))
    return sorted({p for p in found if p.is_file()})


def import_project(
    store: SeekdbStore,
    project_root: Path,
    *,
    project_name: str = "fulfillment-svc",
    specs_dir: str = "specs",
    source_dir: str = "src",
    tests_dir: str = "tests",
) -> ImportResult:
    """把一个业务工程的规格 / 代码 / 测试导入 seekdb。"""
    project_root = project_root.resolve()
    store.ensure_schema()
    kept_items: dict[str, set[str]] = {}

    batch_id = sha256_text(
        f"{project_name}|{project_root}|{datetime.now().isoformat(timespec='seconds')}"
    )[:16]
    result = ImportResult(
        batch_id=batch_id,
        project=project_name,
        source_root=str(project_root),
    )

    for spec_path in _iter_files(project_root, [f"{specs_dir}/**/*.md"]):
        for item in parse_spec_file(spec_path, project_root):
            changed = store.upsert_spec_item(**item)
            result.spec_items.append(item)
            if changed:
                result.changed_spec_items.append(f"{item['spec_id']}:{item['item_id']}")
            keep = kept_items.setdefault(item["spec_id"], set())
            keep.add(item["item_id"])

    # 规格改版后，旧条目号必须从库里消失（否则检索会继续命中过期条目）
    for spec_id, keep in kept_items.items():
        removed = store.delete_spec_items_not_in(spec_id, sorted(keep))
        if removed:
            result.removed_spec_items.append(f"{spec_id}(-{removed})")

    for code_path in _iter_files(project_root, [f"{source_dir}/**/*.py"]):
        for unit in parse_code_file(code_path, project_root):
            changed = store.upsert_code_unit(**unit)
            result.code_units.append(unit)
            if changed:
                result.changed_code_units.append(unit["unit_id"])

    for test_path in _iter_files(project_root, [f"{tests_dir}/**/*.py"]):
        for unit in parse_test_file(test_path, project_root):
            changed = store.upsert_test_unit(**unit)
            result.test_units.append(unit)
            if changed:
                result.changed_test_units.append(unit["unit_id"])

    store.record_manifest(
        batch_id=batch_id,
        project=project_name,
        source_root=str(project_root),
        spec_count=len(result.spec_items),
        code_count=len(result.code_units),
        test_count=len(result.test_units),
        detail=result.summary(),
    )
    return result

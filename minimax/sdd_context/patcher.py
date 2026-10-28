"""统一 diff（unified diff）解析与应用。

MiniMax 输出的补丁以统一 diff 形式给出，这里负责：

1. 从模型回复中提取 diff 代码块；
2. 解析成文件 / hunk 结构；
3. 严格按上下文把补丁应用到工作目录（上下文不匹配即报错，不做模糊匹配）。

严格匹配是刻意的：它保证"应用后的文件内容"与"模型给出的补丁"严格对应，
不会出现静默丢行的情况。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_FENCE = re.compile(r"```(?:diff|patch)?[ \t]*\r?\n(?P<body>.*?)```", re.DOTALL)
_HUNK = re.compile(r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@")


class PatchError(RuntimeError):
    """补丁解析或应用失败。"""


@dataclass
class Hunk:
    """一个 hunk。"""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str] = field(default_factory=list)


@dataclass
class FilePatch:
    """一个文件的补丁。"""

    old_path: str
    new_path: str
    hunks: list[Hunk] = field(default_factory=list)
    is_new: bool = False
    is_deleted: bool = False

    @property
    def target_path(self) -> str:
        path = self.new_path if self.new_path != "/dev/null" else self.old_path
        for prefix in ("a/", "b/"):
            if path.startswith(prefix):
                path = path[len(prefix) :]
        return path


def extract_unified_diff(text: str) -> str:
    """从模型回复中提取统一 diff。"""
    if not text:
        raise PatchError("模型回复为空，无法提取补丁。")

    candidates: list[str] = []
    for match in _FENCE.finditer(text):
        body = match.group("body")
        if "@@" in body and ("--- " in body or "diff --git" in body):
            candidates.append(body.strip("\n"))
    if not candidates:
        # 模型可能没有使用代码块围栏，直接从整段文本里找
        if "@@" in text and ("--- " in text or "diff --git" in text):
            candidates.append(text.strip("\n"))
    if not candidates:
        raise PatchError(
            "模型回复中未找到统一 diff（需要包含 `---`/`+++` 与 `@@` 的补丁块）。"
        )
    return max(candidates, key=len)


def parse_unified_diff(diff: str) -> list[FilePatch]:
    """解析统一 diff，返回文件补丁列表。"""
    lines = diff.splitlines()
    patches: list[FilePatch] = []
    current: FilePatch | None = None
    hunk: Hunk | None = None
    index = 0

    while index < len(lines):
        line = lines[index]
        if line.startswith("diff --git "):
            current = None
            hunk = None
            index += 1
            continue
        if line.startswith("--- "):
            old_path = line[4:].strip()
            if "\t" in old_path:
                old_path = old_path.split("\t", 1)[0].strip()
            new_path = ""
            if index + 1 < len(lines) and lines[index + 1].startswith("+++ "):
                new_path = lines[index + 1][4:].strip()
                if "\t" in new_path:
                    new_path = new_path.split("\t", 1)[0].strip()
                index += 1
            current = FilePatch(
                old_path=old_path,
                new_path=new_path,
                is_new=old_path == "/dev/null",
                is_deleted=new_path == "/dev/null",
            )
            patches.append(current)
            hunk = None
            index += 1
            continue
        hunk_match = _HUNK.match(line)
        if hunk_match:
            if current is None:
                raise PatchError(f"补丁缺少文件头，却出现 hunk：{line}")
            hunk = Hunk(
                old_start=int(hunk_match.group("old_start")),
                old_count=int(hunk_match.group("old_count") or 1),
                new_start=int(hunk_match.group("new_start")),
                new_count=int(hunk_match.group("new_count") or 1),
            )
            current.hunks.append(hunk)
            index += 1
            continue
        if hunk is not None and line[:1] in {" ", "+", "-"}:
            hunk.lines.append(line)
            index += 1
            continue
        if hunk is not None and line.startswith("\\"):
            # "\ No newline at end of file" —— 忽略
            index += 1
            continue
        index += 1

    patches = [patch for patch in patches if patch.hunks]
    if not patches:
        raise PatchError("补丁中没有可应用的 hunk。")
    return patches


def _hunk_old_lines(hunk: Hunk) -> list[str]:
    """取出 hunk 对应的原始行序列（上下文行 + 被删除行）。"""
    return [line[1:] for line in hunk.lines if line[:1] in {" ", "-"}]


def _locate_hunk(src_lines: list[str], hunk: Hunk, expected: int) -> int:
    """在源文件中定位 hunk 的实际起点。

    模型给出的 ``@@`` 起始行号偶尔会有偏差。这里采用与 ``git apply`` 相同的策略：
    **上下文必须逐行完全一致**，只允许整体偏移。先在期望位置附近找，再全文件兜底。
    """
    old_seq = _hunk_old_lines(hunk)
    if not old_seq:
        return expected

    def matches(pos: int) -> bool:
        if pos < 0 or pos + len(old_seq) > len(src_lines):
            return False
        return src_lines[pos : pos + len(old_seq)] == old_seq

    if matches(expected):
        return expected
    # 全文件范围内寻找与期望位置最接近的匹配点
    best: tuple[int, int] | None = None
    for pos in range(0, len(src_lines) - len(old_seq) + 1):
        if not matches(pos):
            continue
        distance = abs(pos - expected)
        if best is None or distance < best[0]:
            best = (distance, pos)
    if best is not None:
        return best[1]
    raise PatchError(
        f"上下文不匹配：在文件里找不到与 hunk 一致的连续 {len(old_seq)} 行"
        f"（期望起点 {expected + 1}）。"
    )


def apply_hunks(original: str, patch: FilePatch) -> str:
    """把一个文件的 hunk 应用到原始文本上（严格匹配上下文，允许整体行号偏移）。"""
    src_lines = original.splitlines()
    out: list[str] = []
    cursor = 0

    for hunk in patch.hunks:
        start = _locate_hunk(src_lines, hunk, hunk.old_start - 1)
        if start < cursor:
            raise PatchError(
                f"{patch.target_path}: hunk 行号重叠（old_start={hunk.old_start}）。"
            )
        out.extend(src_lines[cursor:start])
        cursor = start

        for line in hunk.lines:
            tag, content = line[0], line[1:]
            if tag == " ":
                if cursor >= len(src_lines) or src_lines[cursor] != content:
                    actual = src_lines[cursor] if cursor < len(src_lines) else "<EOF>"
                    raise PatchError(
                        f"{patch.target_path}: 上下文不匹配（期望 {content!r}，实际 {actual!r}）。"
                    )
                out.append(src_lines[cursor])
                cursor += 1
            elif tag == "-":
                if cursor >= len(src_lines) or src_lines[cursor] != content:
                    actual = src_lines[cursor] if cursor < len(src_lines) else "<EOF>"
                    raise PatchError(
                        f"{patch.target_path}: 删除行不匹配（期望 {content!r}，实际 {actual!r}）。"
                    )
                cursor += 1
            elif tag == "+":
                out.append(content)

    out.extend(src_lines[cursor:])
    text = "\n".join(out)
    if original.endswith("\n") or not original:
        text += "\n"
    return text


def apply_unified_diff(diff: str, root: Path) -> list[str]:
    """把统一 diff 应用到 ``root`` 下的文件，返回被修改的文件列表。"""
    patches = parse_unified_diff(diff)
    changed: list[str] = []
    for patch in patches:
        target = root / patch.target_path
        if patch.is_deleted:
            if target.exists():
                target.unlink()
                changed.append(patch.target_path)
            continue
        if patch.is_new or not target.exists():
            original = ""
        else:
            original = target.read_text(encoding="utf-8")
        updated = apply_hunks(original, patch)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(updated, encoding="utf-8", newline="")
        changed.append(patch.target_path)
    return changed


def summarize_diff(diff: str) -> dict[str, Any]:
    """统计补丁规模（新增 / 删除行数、改动文件数）。"""
    added = 0
    removed = 0
    files: list[str] = []
    for patch in parse_unified_diff(diff):
        files.append(patch.target_path)
        for hunk in patch.hunks:
            for line in hunk.lines:
                if line.startswith("+"):
                    added += 1
                elif line.startswith("-"):
                    removed += 1
    return {"files": files, "added_lines": added, "removed_lines": removed}

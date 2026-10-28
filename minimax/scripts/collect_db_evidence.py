"""导出 seekdb 侧的查询证据。

把"工程资料确实存在数据库里、上下文确实来自数据库查询"这件事变成可核对的日志：
库表结构、各表行数、任务对应的全文检索 SQL 与命中结果、必需来源的主键回查。

用法::

    python minimax/scripts/collect_db_evidence.py --out minimax/evidence/db-queries.log
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sdd_context.config import load_settings  # noqa: E402
from sdd_context.retriever import retrieve_context  # noqa: E402
from sdd_context.seekdb_store import (  # noqa: E402
    TABLE_CODE,
    TABLE_MANIFEST,
    TABLE_SPEC,
    TABLE_TEST,
    SeekdbStore,
)
from sdd_context.taskdef import load_task  # noqa: E402


def main() -> int:
    """采集并打印数据库证据。"""
    parser = argparse.ArgumentParser(description="导出 seekdb 查询证据")
    parser.add_argument(
        "--task",
        default=str(ROOT / "demo" / "tasks" / "TASK-001-status-filter.md"),
        help="任务定义文件",
    )
    parser.add_argument("--out", default="", help="输出文件（留空打印到 stdout）")
    args = parser.parse_args()

    settings = load_settings()
    task = load_task(Path(args.task))
    lines: list[str] = []

    def emit(text: str = "") -> None:
        lines.append(text)

    store = SeekdbStore(settings)
    try:
        emit("=" * 78)
        emit("seekdb 查询证据")
        emit("=" * 78)
        emit(f"连接     : {settings.seekdb_host}:{settings.seekdb_port} (user={settings.seekdb_user})")
        emit(f"库       : {settings.seekdb_database}")
        emit(f"版本     : {store.server_version()}")
        emit()

        emit("--- 表结构 ---")
        for table in (TABLE_SPEC, TABLE_CODE, TABLE_TEST, TABLE_MANIFEST):
            rows = store.execute(f"SHOW CREATE TABLE {table}")
            ddl = next(iter(rows[0].values())) if rows else ""
            emit(f"[{table}]")
            for ddl_line in str(ddl).splitlines():
                emit("  " + ddl_line)
            emit()

        emit("--- 索引（FULLTEXT） ---")
        for table in (TABLE_SPEC, TABLE_CODE, TABLE_TEST):
            rows = store.execute(f"SHOW INDEX FROM {table}")
            for row in rows:
                if row.get("Key_name", "").startswith("ft_"):
                    emit(
                        f"  {table}: {row['Key_name']} {row['Index_type']} "
                        f"column={row['Column_name']} visible={row['Visible']}"
                    )
        emit()

        emit("--- 行数 ---")
        for table in (TABLE_SPEC, TABLE_CODE, TABLE_TEST, TABLE_MANIFEST):
            emit(f"  {table}: {store.count_rows(table)}")
        emit()

        emit("--- 全文检索（任务查询 -> MATCH ... AGAINST） ---")
        from sdd_context.tokenize import query_tokens

        tokens = query_tokens(task.title + "\n" + task.objective)
        match_expr = " ".join(tokens)
        emit(f"  查询 token 数: {len(tokens)}")
        emit(f"  SQL: SELECT *, MATCH(body_tokens) AGAINST(%s) AS relevance "
             f"FROM {TABLE_SPEC} WHERE MATCH(body_tokens) AGAINST(%s) "
             f"ORDER BY relevance DESC LIMIT 12")
        rows = store.execute(
            f"SELECT spec_id, item_id, version, source_path, content_hash, "
            f"MATCH(body_tokens) AGAINST(%s) AS relevance "
            f"FROM {TABLE_SPEC} WHERE MATCH(body_tokens) AGAINST(%s) "
            f"ORDER BY relevance DESC LIMIT 12",
            (match_expr, match_expr),
        )
        for row in rows:
            emit(
                f"    hit {row['spec_id']}/{row['item_id']} v{row['version']} "
                f"relevance={row['relevance']} hash={str(row['content_hash'])[:16]} "
                f"src={row['source_path']}"
            )
        emit()

        emit("--- 必需来源主键回查（来源真实存在性校验） ---")
        context = retrieve_context(store, task, settings)
        for item in context.specs + context.codes + context.tests:
            emit(
                f"    {item.category:5s} {item.unit_id:52s} "
                f"hash={str(item.extra.get('content_hash'))[:16]} src={item.source_path}"
            )
        emit(f"    缺失来源: {context.missing_sources or '（无）'}")
        emit()
        emit(f"--- 上下文 digest: {context.digest()}")
    finally:
        store.close()

    text = "\n".join(lines) + "\n"
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"db evidence written to {out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

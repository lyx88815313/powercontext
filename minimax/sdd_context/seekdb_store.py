"""seekdb（OceanBase seekdb）存储与检索层。

工程资料分三类落在三张表里：

* ``spec_item``   —— 规格条目（含版本、来源路径、内容哈希）
* ``code_unit``   —— 代码文件与代码符号
* ``test_unit``   —— 测试文件与测试用例

每张表都额外维护一列 ``body_tokens``（分词后的空格串）并建 FULLTEXT 索引，
检索走 ``MATCH(...) AGAINST(...)``；若实例不支持全文索引，则自动退化为 LIKE 召回。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

import pymysql
from pymysql.cursors import DictCursor

from .config import Settings
from .tokenize import index_text, query_tokens

TABLE_SPEC = "spec_item"
TABLE_CODE = "code_unit"
TABLE_TEST = "test_unit"
TABLE_MANIFEST = "import_manifest"


def sha256_text(text: str) -> str:
    """计算文本的 sha256（用于导入幂等与版本比对）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class RetrievedItem:
    """一条检索命中。"""

    category: str
    unit_id: str
    path: str
    title: str
    body: str
    source_path: str
    score: float
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "unit_id": self.unit_id,
            "path": self.path,
            "title": self.title,
            "source_path": self.source_path,
            "score": round(self.score, 6),
            "extra": self.extra,
        }


class SeekdbStore:
    """seekdb 资料库封装。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._connection: pymysql.connections.Connection | None = None
        self.fulltext_available: bool | None = None

    # ------------------------------------------------------------------ 连接

    def connect(self) -> pymysql.connections.Connection:
        """建立（并缓存）数据库连接。"""
        if self._connection is not None:
            try:
                self._connection.ping(reconnect=True)
                return self._connection
            except Exception:
                self._connection = None
        self._connection = pymysql.connect(
            host=self.settings.seekdb_host,
            port=self.settings.seekdb_port,
            user=self.settings.seekdb_user,
            password=self.settings.seekdb_password,
            charset=self.settings.seekdb_charset,
            cursorclass=DictCursor,
            connect_timeout=10,
            autocommit=True,
        )
        # 选库（库尚未创建时忽略错误，由 ensure_schema 负责创建）
        try:
            with self._connection.cursor() as cur:
                cur.execute(f"USE `{self.settings.seekdb_database}`")
        except Exception:
            pass
        return self._connection

    def close(self) -> None:
        """关闭连接。"""
        if self._connection is not None:
            try:
                self._connection.close()
            finally:
                self._connection = None

    def __enter__(self) -> "SeekdbStore":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def execute(self, sql: str, args: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        """执行一条查询并返回字典列表。"""
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(sql, args or ())
            return list(cur.fetchall())

    def execute_write(self, sql: str, args: Sequence[Any] | None = None) -> int:
        """执行一条写入语句，返回影响行数。"""
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(sql, args or ())
            return cur.rowcount

    # ------------------------------------------------------------------ 建表

    def ensure_schema(self) -> None:
        """建库建表并创建全文索引（幂等）。"""
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{self.settings.seekdb_database}` "
                "DEFAULT CHARACTER SET utf8mb4"
            )
            cur.execute(f"USE `{self.settings.seekdb_database}`")
        with conn.cursor() as cur:
            cur.execute(f"USE `{self.settings.seekdb_database}`")
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {TABLE_SPEC} (
                    spec_id      VARCHAR(128)  NOT NULL,
                    item_id      VARCHAR(128)  NOT NULL,
                    version      VARCHAR(32)   NOT NULL,
                    title        VARCHAR(512)  NOT NULL,
                    body         TEXT          NOT NULL,
                    body_tokens  TEXT          NOT NULL,
                    tags         TEXT,
                    source_path  VARCHAR(512)  NOT NULL,
                    content_hash VARCHAR(64)   NOT NULL,
                    updated_at   DATETIME      NOT NULL,
                    PRIMARY KEY (spec_id, item_id)
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {TABLE_CODE} (
                    unit_id      VARCHAR(256)  NOT NULL,
                    path         VARCHAR(512)  NOT NULL,
                    symbol       VARCHAR(256)  NOT NULL DEFAULT '',
                    kind         VARCHAR(32)   NOT NULL,
                    language     VARCHAR(32)   NOT NULL DEFAULT '',
                    body         TEXT          NOT NULL,
                    body_tokens  TEXT          NOT NULL,
                    source_path  VARCHAR(512)  NOT NULL,
                    content_hash VARCHAR(64)   NOT NULL,
                    updated_at   DATETIME      NOT NULL,
                    PRIMARY KEY (unit_id)
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {TABLE_TEST} (
                    unit_id      VARCHAR(256)  NOT NULL,
                    path         VARCHAR(512)  NOT NULL,
                    test_name    VARCHAR(256)  NOT NULL DEFAULT '',
                    kind         VARCHAR(32)   NOT NULL,
                    body         TEXT          NOT NULL,
                    body_tokens  TEXT          NOT NULL,
                    source_path  VARCHAR(512)  NOT NULL,
                    content_hash VARCHAR(64)   NOT NULL,
                    updated_at   DATETIME      NOT NULL,
                    PRIMARY KEY (unit_id)
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {TABLE_MANIFEST} (
                    batch_id      VARCHAR(64)  NOT NULL,
                    project       VARCHAR(256) NOT NULL,
                    source_root   VARCHAR(512) NOT NULL,
                    imported_at   DATETIME     NOT NULL,
                    spec_count    INT          NOT NULL,
                    code_count    INT          NOT NULL,
                    test_count    INT          NOT NULL,
                    detail_json   TEXT         NOT NULL,
                    PRIMARY KEY (batch_id)
                )
                """
            )
        self._create_fulltext_indexes()

    def _create_fulltext_indexes(self) -> None:
        """为三张表创建 FULLTEXT 索引（幂等；失败不影响主流程，降级为 LIKE 召回）。"""
        conn = self.connect()
        created: list[bool] = []
        for table, column in (
            (TABLE_SPEC, "body_tokens"),
            (TABLE_CODE, "body_tokens"),
            (TABLE_TEST, "body_tokens"),
        ):
            index_name = f"ft_{table}_tokens"
            try:
                with conn.cursor() as cur:
                    cur.execute(f"USE `{self.settings.seekdb_database}`")
                    cur.execute(f"SHOW INDEX FROM {table} WHERE Key_name = %s", (index_name,))
                    exists = bool(cur.fetchall())
                if exists:
                    created.append(True)
                    continue
                with conn.cursor() as cur:
                    cur.execute(f"USE `{self.settings.seekdb_database}`")
                    cur.execute(
                        f"CREATE FULLTEXT INDEX {index_name} ON {table} ({column}) "
                        "WITH PARSER space PARSER_PROPERTIES=(min_token_size=1, max_token_size=84)"
                    )
                created.append(True)
            except Exception:
                # 实例不支持全文索引：降级为 LIKE 召回
                created.append(False)
        self.fulltext_available = all(created) if created else False

    # ------------------------------------------------------------------ 写入

    def upsert_spec_item(
        self,
        *,
        spec_id: str,
        item_id: str,
        version: str,
        title: str,
        body: str,
        tags: str,
        source_path: str,
    ) -> bool:
        """写入（覆盖）一条规格条目，返回内容是否发生变化。"""
        digest = sha256_text(f"{version}\n{title}\n{body}\n{tags}")
        existing = self.execute(
            f"SELECT content_hash, version FROM {TABLE_SPEC} WHERE spec_id=%s AND item_id=%s",
            (spec_id, item_id),
        )
        changed = not existing or existing[0]["content_hash"] != digest
        self.execute_write(
            f"""
            INSERT INTO {TABLE_SPEC}
                (spec_id, item_id, version, title, body, body_tokens, tags, source_path,
                 content_hash, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                version=VALUES(version), title=VALUES(title), body=VALUES(body),
                body_tokens=VALUES(body_tokens), tags=VALUES(tags),
                source_path=VALUES(source_path), content_hash=VALUES(content_hash),
                updated_at=VALUES(updated_at)
            """,
            (
                spec_id,
                item_id,
                version,
                title,
                body,
                index_text(f"{title}\n{body}\n{tags}"),
                tags,
                source_path,
                digest,
                datetime.now(),
            ),
        )
        return changed

    def upsert_code_unit(
        self,
        *,
        unit_id: str,
        path: str,
        symbol: str,
        kind: str,
        language: str,
        body: str,
        source_path: str,
    ) -> bool:
        """写入（覆盖）一条代码单元。"""
        digest = sha256_text(f"{kind}\n{symbol}\n{body}")
        existing = self.execute(
            f"SELECT content_hash FROM {TABLE_CODE} WHERE unit_id=%s", (unit_id,)
        )
        changed = not existing or existing[0]["content_hash"] != digest
        self.execute_write(
            f"""
            INSERT INTO {TABLE_CODE}
                (unit_id, path, symbol, kind, language, body, body_tokens, source_path,
                 content_hash, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                path=VALUES(path), symbol=VALUES(symbol), kind=VALUES(kind),
                language=VALUES(language), body=VALUES(body),
                body_tokens=VALUES(body_tokens), source_path=VALUES(source_path),
                content_hash=VALUES(content_hash), updated_at=VALUES(updated_at)
            """,
            (
                unit_id,
                path,
                symbol,
                kind,
                language,
                body,
                index_text(f"{path}\n{symbol}\n{body}"),
                source_path,
                digest,
                datetime.now(),
            ),
        )
        return changed

    def upsert_test_unit(
        self,
        *,
        unit_id: str,
        path: str,
        test_name: str,
        kind: str,
        body: str,
        source_path: str,
    ) -> bool:
        """写入（覆盖）一条测试单元。"""
        digest = sha256_text(f"{kind}\n{test_name}\n{body}")
        existing = self.execute(
            f"SELECT content_hash FROM {TABLE_TEST} WHERE unit_id=%s", (unit_id,)
        )
        changed = not existing or existing[0]["content_hash"] != digest
        self.execute_write(
            f"""
            INSERT INTO {TABLE_TEST}
                (unit_id, path, test_name, kind, body, body_tokens, source_path,
                 content_hash, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                path=VALUES(path), test_name=VALUES(test_name), kind=VALUES(kind),
                body=VALUES(body), body_tokens=VALUES(body_tokens),
                source_path=VALUES(source_path), content_hash=VALUES(content_hash),
                updated_at=VALUES(updated_at)
            """,
            (
                unit_id,
                path,
                test_name,
                kind,
                body,
                index_text(f"{path}\n{test_name}\n{body}"),
                source_path,
                digest,
                datetime.now(),
            ),
        )
        return changed

    def delete_spec_items_not_in(
        self, spec_id: str, keep_item_ids: Sequence[str]
    ) -> int:
        """删除某份规格里已经不存在的条目。

        规格改版后，旧的条目号必须从库里消失，否则检索会继续命中过期条目。
        """
        if keep_item_ids:
            placeholders = ", ".join(["%s"] * len(keep_item_ids))
            sql = (
                f"DELETE FROM {TABLE_SPEC} WHERE spec_id=%s "
                f"AND item_id NOT IN ({placeholders})"
            )
            args = [spec_id, *keep_item_ids]
        else:
            sql = f"DELETE FROM {TABLE_SPEC} WHERE spec_id=%s"
            args = [spec_id]
        return self.execute_write(sql, args)

    def record_manifest(
        self,
        *,
        batch_id: str,
        project: str,
        source_root: str,
        spec_count: int,
        code_count: int,
        test_count: int,
        detail: dict[str, Any],
    ) -> None:
        """记录一次导入批次（作为"数据库实际参与"的证据）。"""
        self.execute_write(
            f"""
            INSERT INTO {TABLE_MANIFEST}
                (batch_id, project, source_root, imported_at, spec_count, code_count,
                 test_count, detail_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                imported_at=VALUES(imported_at), spec_count=VALUES(spec_count),
                code_count=VALUES(code_count), test_count=VALUES(test_count),
                detail_json=VALUES(detail_json)
            """,
            (
                batch_id,
                project,
                source_root,
                datetime.now(),
                spec_count,
                code_count,
                test_count,
                json.dumps(detail, ensure_ascii=False, sort_keys=True),
            ),
        )

    # ------------------------------------------------------------------ 检索

    def _fulltext_query(self, table: str, tokens: Sequence[str], limit: int) -> list[dict[str, Any]]:
        """用 FULLTEXT 索引检索（同时取回数据库给出的相关度分值）。"""
        match_expr = " ".join(tokens)
        sql = (
            f"SELECT *, MATCH(body_tokens) AGAINST(%s) AS relevance "
            f"FROM {table} "
            f"WHERE MATCH(body_tokens) AGAINST(%s) "
            f"ORDER BY relevance DESC LIMIT %s"
        )
        return self.execute(sql, (match_expr, match_expr, limit))

    def _like_query(self, table: str, tokens: Sequence[str], limit: int) -> list[dict[str, Any]]:
        """全文检索不可用时的 LIKE 兜底。"""
        if not tokens:
            return []
        conditions = []
        args: list[Any] = []
        for token in tokens[:24]:
            conditions.append("(body_tokens LIKE %s OR path LIKE %s)")
            args.append(f"%{token}%")
            args.append(f"%{token}%")
        sql = (
            f"SELECT * FROM {table} WHERE {' OR '.join(conditions)} LIMIT %s"
        )
        args.append(limit)
        return self.execute(sql, args)

    def search(
        self,
        table: str,
        query: str,
        *,
        limit: int = 20,
        extra_terms: Iterable[str] = (),
    ) -> list[RetrievedItem]:
        """在指定表中检索与查询相关的资料。

        检索完全在 seekdb 内完成（FULLTEXT 优先，LIKE 兜底），
        返回按相关度排序的命中列表。
        """
        tokens = query_tokens(query)
        for term in extra_terms:
            for token in query_tokens(term):
                if token not in tokens:
                    tokens.append(token)
        if not tokens:
            return []

        rows: list[dict[str, Any]] = []
        mode = "like"
        if self.fulltext_available is not False:
            try:
                rows = self._fulltext_query(table, tokens, limit)
                mode = "fulltext"
            except Exception:
                self.fulltext_available = False
                rows = []
        if not rows:
            rows = self._like_query(table, tokens, limit)
            mode = "like"

        items: list[RetrievedItem] = []
        for row in rows:
            item = self._row_to_item(table, row)
            item.extra["retrieval_mode"] = mode
            items.append(item)
        return items

    def _row_to_item(self, table: str, row: dict[str, Any]) -> RetrievedItem:
        """把数据库行转成统一结构。"""
        relevance = row.get("relevance")
        score = float(relevance) if relevance is not None else 0.0
        if table == TABLE_SPEC:
            return RetrievedItem(
                category="spec",
                unit_id=f"{row['spec_id']}:{row['item_id']}",
                path=row["source_path"],
                title=f"{row['spec_id']} / {row['item_id']} {row['title']}",
                body=row["body"],
                source_path=row["source_path"],
                score=score,
                extra={
                    "spec_id": row["spec_id"],
                    "item_id": row["item_id"],
                    "version": row["version"],
                    "tags": row.get("tags") or "",
                    "content_hash": row["content_hash"],
                    "updated_at": str(row["updated_at"]),
                },
            )
        if table == TABLE_CODE:
            return RetrievedItem(
                category="code",
                unit_id=row["unit_id"],
                path=row["path"],
                title=row["symbol"] or row["path"],
                body=row["body"],
                source_path=row["source_path"],
                score=score,
                extra={
                    "symbol": row["symbol"],
                    "kind": row["kind"],
                    "language": row["language"],
                    "content_hash": row["content_hash"],
                    "updated_at": str(row["updated_at"]),
                },
            )
        return RetrievedItem(
            category="test",
            unit_id=row["unit_id"],
            path=row["path"],
            title=row["test_name"] or row["path"],
            body=row["body"],
            source_path=row["source_path"],
            score=score,
            extra={
                "test_name": row["test_name"],
                "kind": row["kind"],
                "content_hash": row["content_hash"],
                "updated_at": str(row["updated_at"]),
            },
        )

    def get_by_unit_id(self, table: str, unit_id: str) -> RetrievedItem | None:
        """按主键精确取一条资料（用于校验"来源必须真实存在"）。"""
        rows = self.execute(f"SELECT * FROM {table} WHERE unit_id=%s", (unit_id,))
        if not rows:
            return None
        return self._row_to_item(table, rows[0])

    def get_spec_item(self, spec_id: str, item_id: str) -> RetrievedItem | None:
        """按 (spec_id, item_id) 精确取一条规格条目。"""
        rows = self.execute(
            f"SELECT * FROM {TABLE_SPEC} WHERE spec_id=%s AND item_id=%s",
            (spec_id, item_id),
        )
        if not rows:
            return None
        return self._row_to_item(TABLE_SPEC, rows[0])

    def get_spec_by_item_id(self, item_id: str) -> RetrievedItem | None:
        """按条目号精确取一条规格条目（条目号全局唯一）。"""
        rows = self.execute(
            f"SELECT * FROM {TABLE_SPEC} WHERE item_id=%s", (item_id,)
        )
        if not rows:
            return None
        return self._row_to_item(TABLE_SPEC, rows[0])

    def count_rows(self, table: str) -> int:
        """统计表行数（作为数据库实际参与的证据）。"""
        rows = self.execute(f"SELECT COUNT(*) AS c FROM {table}")
        return int(rows[0]["c"]) if rows else 0

    def server_version(self) -> str:
        """返回数据库版本字符串。"""
        rows = self.execute("SELECT version() AS v")
        return str(rows[0]["v"]) if rows else ""

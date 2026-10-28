"""任务驱动的资料检索。

检索完全基于 seekdb：

* 候选集由数据库的全文索引（``MATCH ... AGAINST``）给出；
* 任务声明的必需来源逐条回查数据库主键，确认"来源真实存在"；
* 检索模式、行数、命中条目都会记录进 ``db_evidence``，作为数据库实际参与的证据。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .seekdb_store import (
    TABLE_CODE,
    TABLE_SPEC,
    TABLE_TEST,
    RetrievedItem,
    SeekdbStore,
)
from .taskdef import TaskDefinition


@dataclass
class DevelopmentContext:
    """一次检索得到的开发上下文。"""

    task: TaskDefinition
    query_text: str
    specs: list[RetrievedItem] = field(default_factory=list)
    codes: list[RetrievedItem] = field(default_factory=list)
    tests: list[RetrievedItem] = field(default_factory=list)
    db_evidence: dict[str, Any] = field(default_factory=dict)
    missing_sources: list[str] = field(default_factory=list)

    def digest(self) -> str:
        """上下文的稳定摘要（用于判断上下文是否发生变化）。

        摘要与**检索排序无关**（排序会随相关度并列而变化），只取决于
        "选了哪些来源 + 它们的正文内容"。
        """
        def _fingerprint(items: list[RetrievedItem]) -> list[list[str]]:
            rows = [
                [item.unit_id, str(item.extra.get("content_hash") or "")]
                for item in items
            ]
            return sorted(rows)

        payload = json.dumps(
            {
                "task_id": self.task.task_id,
                "specs": _fingerprint(self.specs),
                "codes": _fingerprint(self.codes),
                "tests": _fingerprint(self.tests),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的字典。"""
        return {
            "task": self.task.to_dict(),
            "query_text": self.query_text,
            "digest": self.digest(),
            "specs": [item.to_dict() for item in self.specs],
            "codes": [item.to_dict() for item in self.codes],
            "tests": [item.to_dict() for item in self.tests],
            "missing_sources": self.missing_sources,
            "db_evidence": self.db_evidence,
        }


def _merge(required: list[RetrievedItem], retrieved: list[RetrievedItem]) -> list[RetrievedItem]:
    """把必需来源放在最前，其余按检索顺序追加（按 unit_id 去重）。"""
    merged: list[RetrievedItem] = []
    seen: set[str] = set()
    for item in required + retrieved:
        if item.unit_id in seen:
            continue
        seen.add(item.unit_id)
        merged.append(item)
    return merged


def retrieve_context(
    store: SeekdbStore, task: TaskDefinition, settings: Settings
) -> DevelopmentContext:
    """根据任务在 seekdb 中检索适用资料，组装开发上下文。"""
    query_text = "\n".join(
        part
        for part in (task.title, task.objective, task.body)
        if part and part.strip()
    ).strip()

    limit = settings.max_retrieval_items
    evidence: dict[str, Any] = {
        "server_version": store.server_version(),
        "database": settings.seekdb_database,
        "fulltext_available": store.fulltext_available,
        "query_tokens": len(query_text),
        "queries": {},
        "row_counts": {
            TABLE_SPEC: store.count_rows(TABLE_SPEC),
            TABLE_CODE: store.count_rows(TABLE_CODE),
            TABLE_TEST: store.count_rows(TABLE_TEST),
        },
    }

    required_terms = (
        task.required_spec_items + task.required_code_units + task.required_test_units
    )
    retrieved_specs = store.search(TABLE_SPEC, query_text, limit=limit, extra_terms=required_terms)
    retrieved_codes = store.search(TABLE_CODE, query_text, limit=limit, extra_terms=required_terms)
    retrieved_tests = store.search(TABLE_TEST, query_text, limit=limit, extra_terms=required_terms)
    evidence["queries"] = {
        TABLE_SPEC: [item.unit_id for item in retrieved_specs],
        TABLE_CODE: [item.unit_id for item in retrieved_codes],
        TABLE_TEST: [item.unit_id for item in retrieved_tests],
    }
    evidence["retrieval_mode"] = {
        TABLE_SPEC: retrieved_specs[0].extra.get("retrieval_mode") if retrieved_specs else "none",
        TABLE_CODE: retrieved_codes[0].extra.get("retrieval_mode") if retrieved_codes else "none",
        TABLE_TEST: retrieved_tests[0].extra.get("retrieval_mode") if retrieved_tests else "none",
    }

    missing: list[str] = []
    required_specs: list[RetrievedItem] = []
    for item_id in task.required_spec_items:
        item = store.get_spec_by_item_id(item_id)
        if item is None:
            missing.append(f"spec:{item_id}")
        else:
            required_specs.append(item)

    required_codes: list[RetrievedItem] = []
    for unit_id in task.required_code_units:
        item = store.get_by_unit_id(TABLE_CODE, unit_id)
        if item is None:
            missing.append(f"code:{unit_id}")
        else:
            required_codes.append(item)
            # 符号级引用时，同时带上文件级单元（模型需要完整文件才能产出可应用的 diff）
            if "::" in unit_id:
                file_item = store.get_by_unit_id(TABLE_CODE, unit_id.split("::", 1)[0])
                if file_item is not None:
                    required_codes.append(file_item)

    required_tests: list[RetrievedItem] = []
    for unit_id in task.required_test_units:
        item = store.get_by_unit_id(TABLE_TEST, unit_id)
        if item is None:
            missing.append(f"test:{unit_id}")
        else:
            required_tests.append(item)
            if "::" in unit_id:
                file_item = store.get_by_unit_id(TABLE_TEST, unit_id.split("::", 1)[0])
                if file_item is not None:
                    required_tests.append(file_item)

    context = DevelopmentContext(
        task=task,
        query_text=query_text,
        specs=_merge(required_specs, retrieved_specs),
        codes=_merge(required_codes, retrieved_codes),
        tests=_merge(required_tests, retrieved_tests),
        db_evidence=evidence,
        missing_sources=missing,
    )
    evidence["missing_sources"] = missing
    evidence["selected"] = {
        "specs": [item.unit_id for item in context.specs],
        "codes": [item.unit_id for item in context.codes],
        "tests": [item.unit_id for item in context.tests],
    }
    return context

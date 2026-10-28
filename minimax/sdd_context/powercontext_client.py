"""PowerContext HTTP 客户端。

只使用 PowerContext 公开的 REST 接口（``/v1/**``），全部请求都会被记录到
证据日志里（方法、URL、状态码、耗时、请求体摘要），便于核对"PowerContext 实际参与"。

状态写入采用"双写 + 指针"策略：

1. ``POST /v1/work/outcomes/record``  —— 写入结构化任务结果（已完成 / 待办 / 验证结果）。
2. ``POST /v1/sources/content``       —— 写入任务的字节级快照（真相来源）。
3. ``POST /v1/memory/remember``       —— 写入可检索的记忆条目，指向快照。

读取时先用 ``/v1/memory/entries/list`` 找到本任务最新的记忆条目，再按快照
``source_id`` 回读原始内容，从而保证跨重启后能取回完整任务状态。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx


class PowerContextError(RuntimeError):
    """PowerContext 调用失败。"""


@dataclass
class RequestRecord:
    """一次 HTTP 调用的审计记录。"""

    method: str
    url: str
    status_code: int
    elapsed_ms: float
    request_summary: dict[str, Any] = field(default_factory=dict)
    response_summary: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "url": self.url,
            "status_code": self.status_code,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "request": self.request_summary,
            "response": self.response_summary,
            "error": self.error,
        }


class PowerContextClient:
    """PowerContext 服务的轻量客户端。"""

    def __init__(self, settings: Any, *, evidence_log: Path | None = None):
        self.settings = settings
        self.base_url = settings.powercontext_url.rstrip("/")
        self.timeout = settings.powercontext_timeout
        self.token = settings.powercontext_token
        self.scope_id: str = settings.powercontext_scope_id
        self.scope_key: str = settings.powercontext_scope_key
        self.evidence_log = evidence_log
        self.records: list[RequestRecord] = []
        self._client: httpx.Client | None = None

    # ------------------------------------------------------------------ 基础

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """发起一次 HTTP 请求并记录审计信息。"""
        url = f"{self.base_url}{path}"
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        started = time.time()
        record = RequestRecord(
            method=method,
            url=url,
            status_code=0,
            elapsed_ms=0.0,
            request_summary=_summarize(json_body),
        )
        try:
            response = self._client.request(
                method, url, headers=self._headers(), json=json_body, params=params
            )
            record.status_code = response.status_code
            elapsed = (time.time() - started) * 1000
            record.elapsed_ms = elapsed
            if response.status_code >= 400:
                record.error = response.text[:2000]
                self._finish(record)
                raise PowerContextError(
                    f"{method} {path} -> HTTP {response.status_code}: {response.text[:800]}"
                )
            data: Any
            try:
                data = response.json()
            except Exception:
                data = {"_raw": response.text[:4000]}
            record.response_summary = _summarize(data)
            self._finish(record)
            return data
        except httpx.HTTPError as exc:
            record.error = f"{type(exc).__name__}: {exc}"
            record.elapsed_ms = (time.time() - started) * 1000
            self._finish(record)
            raise PowerContextError(f"{method} {path} 失败：{exc}") from exc

    def _finish(self, record: RequestRecord) -> None:
        self.records.append(record)
        if self.evidence_log is not None:
            self.evidence_log.parent.mkdir(parents=True, exist_ok=True)
            with self.evidence_log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")

    def close(self) -> None:
        """关闭底层 HTTP 连接。"""
        if self._client is not None:
            self._client.close()
            self._client = None

    # ------------------------------------------------------------------ 健康

    def health(self) -> dict[str, Any]:
        """健康检查（``/health/live`` 与 ``/health/ready``）。"""
        live = self._request("GET", "/health/live")
        try:
            ready = self._request("GET", "/health/ready")
        except PowerContextError:
            ready = {}
        return {"live": live, "ready": ready}

    # ------------------------------------------------------------------ Scope

    def ensure_scope(self) -> str:
        """幂等地取得工作 scope。

        优先级：

        1. 显式配置的 ``scope_id``；
        2. 配置了 ``scope_key`` 时，用固定 ``idempotency_key`` 创建/复用专属 scope
           （同一个 key 总是映射到同一个 ``scope_id``）；
        3. 否则使用服务端默认 scope（服务启动时自动创建，跨重启保持不变）。
        """
        if self.scope_id:
            return self.scope_id
        if self.scope_key:
            created = self._request(
                "POST",
                "/v1/scopes",
                json_body={
                    "title": "MiniMax SDD Context",
                    "summary": "SDD 上下文模块的任务状态存储",
                    "idempotency_key": self.scope_key,
                },
            )
            scope_id = created.get("scope_id") if isinstance(created, dict) else None
            if not scope_id:
                raise PowerContextError(f"创建 scope 失败：{created}")
            self.scope_id = scope_id
            return scope_id
        default = self.default_scope()
        scope_id = default.get("scope_id") if isinstance(default, dict) else None
        if not scope_id:
            raise PowerContextError(f"获取默认 scope 失败：{default}")
        self.scope_id = scope_id
        return scope_id

    def default_scope(self) -> dict[str, Any]:
        """返回默认 scope 描述。"""
        return self._request("GET", "/v1/scopes/default")

    # ------------------------------------------------------------------ 内容源

    def capture_content(
        self,
        source_id: str,
        content: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """写入一个内容源（字节级快照）。"""
        body: dict[str, Any] = {
            "scope_id": self.ensure_scope(),
            "source_id": source_id,
            "content": content,
        }
        if metadata:
            body["metadata"] = metadata
        return self._request("POST", "/v1/sources/content", json_body=body)

    def read_content(self, source_id: str) -> str | None:
        """回读内容源原文（找不到返回 ``None``）。"""
        scope_id = self.ensure_scope()
        try:
            data = self._request(
                "GET", f"/v1/scopes/{scope_id}/sources/content/{source_id}"
            )
        except PowerContextError:
            return None
        if isinstance(data, dict):
            return data.get("content")
        return None

    def list_sources(self) -> list[dict[str, Any]]:
        """列出 scope 下的内容源。"""
        scope_id = self.ensure_scope()
        data = self._request("GET", f"/v1/scopes/{scope_id}/sources")
        if isinstance(data, dict):
            items = data.get("items") or data.get("sources") or []
            return list(items)
        return []

    # ------------------------------------------------------------------ 记忆

    def remember(self, kind: str, text: str, *, reason: str = "") -> dict[str, Any]:
        """写入一条记忆条目。"""
        body: dict[str, Any] = {
            "scope_id": self.ensure_scope(),
            "kind": kind,
            "text": text,
        }
        if reason:
            body["reason"] = reason
        return self._request("POST", "/v1/memory/remember", json_body=body)

    def list_entries(self, *, limit: int = 100, include_inactive: bool = False) -> list[dict[str, Any]]:
        """列出记忆条目。"""
        body: dict[str, Any] = {
            "scope_id": self.ensure_scope(),
            "limit": limit,
            "include_inactive": include_inactive,
        }
        data = self._request("POST", "/v1/memory/entries/list", json_body=body)
        if isinstance(data, dict):
            return list(data.get("entries") or [])
        return []

    # ------------------------------------------------------------------ 工作结果

    def record_outcome(self, source_id: str, outcome: dict[str, Any]) -> dict[str, Any]:
        """记录结构化任务结果（已完成 / 待办 / 验证结果）。"""
        body = {
            "scope_id": self.ensure_scope(),
            "source_id": source_id,
            "outcome": outcome,
        }
        return self._request("POST", "/v1/work/outcomes/record", json_body=body)


def _summarize(payload: Any, *, limit: int = 400) -> dict[str, Any]:
    """给请求 / 响应生成摘要（截断长文本，避免证据文件过大）。"""
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        return {"_type": type(payload).__name__}
    summary: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, str):
            summary[key] = value if len(value) <= limit else value[:limit] + f"...(+{len(value) - limit})"
        elif isinstance(value, (int, float, bool)) or value is None:
            summary[key] = value
        elif isinstance(value, list):
            summary[key] = f"[list len={len(value)}]"
        elif isinstance(value, dict):
            summary[key] = {k: v for k, v in list(value.items())[:6]}
        else:
            summary[key] = f"<{type(value).__name__}>"
    return summary

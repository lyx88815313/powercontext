"""MiniMax 调用客户端。

支持两种协议，通过 ``MINIMAX_PROTOCOL`` 或 base URL 自动选择：

* ``anthropic`` —— ``POST {base}/messages``，鉴权头 ``x-api-key``（MiniMax 官方与
  MiniMax Code 网关均提供该协议）。
* ``openai``    —— ``POST {base}/chat/completions``，鉴权头 ``Authorization: Bearer``。

凭据只从环境变量或本机运行时配置读取，**不会写入仓库**。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx


class MiniMaxError(RuntimeError):
    """MiniMax 调用失败。"""


@dataclass
class MiniMaxCall:
    """一次模型调用的记录（用于"技术使用记录"）。"""

    model: str
    protocol: str
    base_url: str
    system_prompt: str
    user_prompt: str
    response_text: str = ""
    request_body: dict[str, Any] = field(default_factory=dict)
    response_body: dict[str, Any] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    attempts: int = 1
    error: str = ""

    def to_dict(self, *, include_prompts: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "model": self.model,
            "protocol": self.protocol,
            "base_url": self.base_url,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "attempts": self.attempts,
            "error": self.error,
        }
        if include_prompts:
            data["system_prompt"] = self.system_prompt
            data["user_prompt"] = self.user_prompt
            data["response_text"] = self.response_text
            data["request_body"] = self.request_body
            data["response_body"] = self.response_body
        return data


class MiniMaxClient:
    """MiniMax 文本模型客户端。"""

    def __init__(self, settings: Any):
        self.settings = settings
        self.protocol = (settings.minimax_protocol or "anthropic").lower()
        self.auth_style = (getattr(settings, "minimax_auth_style", "") or "").lower()
        self.model = settings.minimax_model
        self.base_url = settings.minimax_base_url.rstrip("/")
        self.api_key = settings.minimax_api_key
        self.timeout = settings.minimax_timeout
        self.max_tokens = settings.minimax_max_tokens
        self.temperature = settings.minimax_temperature
        self.extra_headers = dict(getattr(settings, "extra_headers", {}) or {})
        self.last_call: MiniMaxCall | None = None

    # ------------------------------------------------------------------ 校验

    def ensure_configured(self) -> None:
        """确认模型调用所需配置齐全。"""
        missing = []
        if not self.base_url:
            missing.append("MINIMAX_BASE_URL")
        if not self.api_key:
            missing.append("MINIMAX_API_KEY")
        if missing:
            raise MiniMaxError(
                "缺少 MiniMax 调用配置：" + ", ".join(missing)
                + "。请设置环境变量，或在本机配置 MiniMax Code 的 config.yaml。"
            )

    # ------------------------------------------------------------------ 调用

    def complete(self, system: str, user: str) -> MiniMaxCall:
        """发起一次补全请求并返回调用记录。"""
        self.ensure_configured()
        call = MiniMaxCall(
            model=self.model,
            protocol=self.protocol,
            base_url=self.base_url,
            system_prompt=system,
            user_prompt=user,
        )
        started = time.time()
        try:
            with httpx.Client(timeout=self.timeout) as client:
                if self.protocol == "anthropic":
                    url, headers, body = self._build_anthropic(system, user)
                else:
                    url, headers, body = self._build_openai(system, user)
                call.request_body = body
                response = client.post(url, headers=headers, json=body)
                if response.status_code != 200:
                    call.error = f"HTTP {response.status_code}: {response.text[:2000]}"
                    raise MiniMaxError(call.error)
                data = response.json()
                call.response_body = data
                call.response_text = self._extract_text(data)
        except MiniMaxError:
            call.elapsed_seconds = time.time() - started
            self.last_call = call
            raise
        except Exception as exc:  # 网络层异常
            call.elapsed_seconds = time.time() - started
            call.error = f"{type(exc).__name__}: {exc}"
            self.last_call = call
            raise MiniMaxError(call.error) from exc
        call.elapsed_seconds = time.time() - started
        self.last_call = call
        return call

    # ------------------------------------------------------------------ 协议

    def _build_anthropic(
        self, system: str, user: str
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        url = f"{self.base_url}/messages"
        headers = {"Content-Type": "application/json"}
        if self.auth_style == "bearer":
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            headers["x-api-key"] = self.api_key
        headers.update(self.extra_headers)
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": user}],
        }
        if system:
            body["system"] = system
        body["temperature"] = self.temperature
        return url, headers, body

    def _build_openai(
        self, system: str, user: str
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        headers.update(self.extra_headers)
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": messages,
            "temperature": self.temperature,
        }
        return url, headers, body

    def _extract_text(self, data: dict[str, Any]) -> str:
        """从响应里取文本。"""
        if self.protocol == "anthropic":
            blocks = data.get("content") or []
            return "".join(
                block.get("text", "") for block in blocks if block.get("type") == "text"
            )
        choices = data.get("choices") or []
        if choices:
            return (choices[0].get("message") or {}).get("content") or ""
        return ""

    @staticmethod
    def dumps(record: MiniMaxCall) -> str:
        """把调用记录序列化为 JSON（用于证据文件）。"""
        return json.dumps(record.to_dict(), ensure_ascii=False, indent=2)

"""运行配置。

所有配置项都可以用环境变量覆盖，默认值面向"本机单机"场景：

* seekdb：默认 ``127.0.0.1:10100``（本仓库 seekdb WSL 单机部署的端口）。
* PowerContext：默认 ``http://127.0.0.1:8000``（``powercontext server run`` 默认监听）。
* MiniMax：优先读环境变量；未设置时回落到 MiniMax Code 的 ``config.yaml`` provider 配置。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_SEEKDB_HOST = "127.0.0.1"
DEFAULT_SEEKDB_PORT = 10100
DEFAULT_SEEKDB_USER = "root"
DEFAULT_SEEKDB_PASSWORD = ""
DEFAULT_SEEKDB_DATABASE = "sdd_context"

DEFAULT_POWERCONTEXT_URL = "http://127.0.0.1:8000"

#: MiniMax Code 运行时数据目录里存放 provider 配置的位置（用于回落到已配置模型）。
_MAVIS_CONFIG_CANDIDATES = (
    os.environ.get("MINIMAX_DATA_DIR"),
    os.environ.get("__MAVIS_PARENT_DATA_DIR"),
    str(Path.home() / ".minimax"),
)


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return value if value is not None and value != "" else default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    """模块运行期配置。"""

    # ---- seekdb / OceanBase ----
    seekdb_host: str = DEFAULT_SEEKDB_HOST
    seekdb_port: int = DEFAULT_SEEKDB_PORT
    seekdb_user: str = DEFAULT_SEEKDB_USER
    seekdb_password: str = DEFAULT_SEEKDB_PASSWORD
    seekdb_database: str = DEFAULT_SEEKDB_DATABASE
    seekdb_charset: str = "utf8mb4"

    # ---- PowerContext ----
    powercontext_url: str = DEFAULT_POWERCONTEXT_URL
    powercontext_token: str = ""
    powercontext_timeout: float = 30.0
    powercontext_scope_id: str = ""
    powercontext_scope_key: str = ""

    # ---- MiniMax ----
    minimax_api_key: str = ""
    minimax_base_url: str = ""
    minimax_model: str = "MiniMax-M3"
    minimax_protocol: str = ""  # anthropic | openai | auto
    minimax_auth_style: str = ""  # x-api-key | bearer
    minimax_timeout: float = 300.0
    minimax_max_tokens: int = 8192
    minimax_temperature: float = 0.2

    # ---- 模块行为 ----
    evidence_dir: Path = field(default_factory=lambda: Path("evidence"))
    max_context_bytes: int = 120_000
    max_retrieval_items: int = 12
    extra_headers: dict[str, str] = field(default_factory=dict)

    # ---- 运行时探测结果（不参与配置比对） ----
    minimax_source: str = ""

    def as_dict(self) -> dict[str, Any]:
        """导出为可写入证据文件的字典（凭据一律脱敏）。"""
        return {
            "seekdb": {
                "host": self.seekdb_host,
                "port": self.seekdb_port,
                "user": self.seekdb_user,
                "database": self.seekdb_database,
                "password": "***" if self.seekdb_password else "",
            },
            "powercontext": {
                "url": self.powercontext_url,
                "token": "***" if self.powercontext_token else "",
                "scope_key": self.powercontext_scope_key,
            },
            "minimax": {
                "base_url": self.minimax_base_url,
                "model": self.minimax_model,
                "protocol": self.minimax_protocol,
                "auth_style": self.minimax_auth_style,
                "api_key": "***" if self.minimax_api_key else "",
                "source": self.minimax_source,
            },
            "module": {
                "max_context_bytes": self.max_context_bytes,
                "max_retrieval_items": self.max_retrieval_items,
            },
        }


def _load_mavis_oauth_token() -> str:
    """读取本机 MiniMax Code 登录态里的访问令牌。

    MiniMax 网关（``agent.minimax.cn``）使用 Bearer 令牌鉴权，令牌由本地运行时持有。
    该令牌只在运行时从本机读取，**不会写入仓库或证据文件**。
    """
    for candidate in _MAVIS_CONFIG_CANDIDATES:
        if not candidate:
            continue
        auth_root = Path(candidate) / "auth"
        if not auth_root.is_dir():
            continue
        for auth_file in sorted(auth_root.glob("**/auth.json")):
            try:
                data = json.loads(auth_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            records = data.get("records") or {}
            for record in records.values():
                token = record.get("accessToken")
                if isinstance(token, str) and token:
                    return token
    return ""


def _load_mavis_provider() -> dict[str, Any]:
    """从 MiniMax Code 的 ``config.yaml`` 里读取 minimax provider 配置。

    仅用于"本机已有配置"的回落；仓库里不会写入任何真实凭据。
    """
    for candidate in _MAVIS_CONFIG_CANDIDATES:
        if not candidate:
            continue
        path = Path(candidate) / "config.yaml"
        if not path.is_file():
            continue
        try:
            import yaml  # 延迟导入，避免非必要依赖

            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:  # pragma: no cover - 配置损坏时静默回落到环境变量
            continue
        provider = (data.get("provider") or {}).get("minimax")
        if not isinstance(provider, dict):
            continue
        options = provider.get("options") or {}
        models = provider.get("models") or {}
        default_ref = data.get("defaultModel") or ""
        default_model = ""
        if isinstance(default_ref, str) and "/" in default_ref:
            default_model = default_ref.split("/", 1)[1]
        model = default_model if default_model in models else (next(iter(models), ""))
        if not model:
            continue
        npm = provider.get("npm", "")
        protocol = ""
        if npm == "@ai-sdk/anthropic":
            protocol = "anthropic"
        elif npm in {"@ai-sdk/openai", "@ai-sdk/openai-compatible"}:
            protocol = "openai"
        base_url = options.get("baseURL", "")
        api_key = options.get("apiKey", "")
        auth_style = ""
        # 网关地址需要 Bearer 令牌；公开 API 用配置里的 API Key。
        if "agent.minimax.cn" in base_url:
            oauth_token = _load_mavis_oauth_token()
            if oauth_token:
                api_key = oauth_token
                auth_style = "bearer"
        return {
            "api_key": api_key,
            "base_url": base_url,
            "model": model,
            "protocol": protocol,
            "auth_style": auth_style,
            "headers": options.get("headers") or {},
            "source": f"minimax-code config: {path}",
        }
    return {}


def load_settings() -> Settings:
    """从环境变量（可选回落 MiniMax Code 配置）构造 :class:`Settings`。"""
    settings = Settings(
        seekdb_host=_env("SDD_SEEKDB_HOST", DEFAULT_SEEKDB_HOST),
        seekdb_port=_env_int("SDD_SEEKDB_PORT", DEFAULT_SEEKDB_PORT),
        seekdb_user=_env("SDD_SEEKDB_USER", DEFAULT_SEEKDB_USER),
        seekdb_password=os.environ.get("SDD_SEEKDB_PASSWORD", DEFAULT_SEEKDB_PASSWORD),
        seekdb_database=_env("SDD_SEEKDB_DATABASE", DEFAULT_SEEKDB_DATABASE),
        powercontext_url=_env("SDD_POWERCONTEXT_URL", DEFAULT_POWERCONTEXT_URL),
        powercontext_token=os.environ.get("SDD_POWERCONTEXT_TOKEN", ""),
        powercontext_timeout=float(_env("SDD_POWERCONTEXT_TIMEOUT", "30")),
        powercontext_scope_id=os.environ.get("SDD_POWERCONTEXT_SCOPE_ID", ""),
        powercontext_scope_key=_env("SDD_POWERCONTEXT_SCOPE_KEY", ""),
        minimax_api_key=os.environ.get("MINIMAX_API_KEY", ""),
        minimax_base_url=os.environ.get("MINIMAX_BASE_URL", ""),
        minimax_model=_env("MINIMAX_MODEL", "MiniMax-M3"),
        minimax_protocol=_env("MINIMAX_PROTOCOL", ""),
        minimax_auth_style=_env("MINIMAX_AUTH_STYLE", ""),
        minimax_timeout=float(_env("MINIMAX_TIMEOUT", "300")),
        minimax_max_tokens=_env_int("MINIMAX_MAX_TOKENS", 8192),
        minimax_temperature=float(_env("MINIMAX_TEMPERATURE", "0.2")),
    )

    if not (settings.minimax_api_key and settings.minimax_base_url):
        provider = _load_mavis_provider()
        if provider:
            settings.minimax_api_key = settings.minimax_api_key or provider.get("api_key", "")
            settings.minimax_base_url = settings.minimax_base_url or provider.get("base_url", "")
            if not settings.minimax_protocol:
                settings.minimax_protocol = provider.get("protocol", "")
            if not settings.minimax_auth_style:
                settings.minimax_auth_style = provider.get("auth_style", "")
            settings.extra_headers.update(provider.get("headers") or {})
            settings.minimax_source = provider.get("source", "")

    if not settings.minimax_protocol:
        base = settings.minimax_base_url.lower()
        if "/anthropic" in base:
            settings.minimax_protocol = "anthropic"
        elif base.rstrip("/").endswith("/messages"):
            settings.minimax_protocol = "anthropic"
        else:
            settings.minimax_protocol = "openai"

    if settings.minimax_source:
        settings.minimax_source += " (仅本次运行生效，不写入仓库)"

    return settings

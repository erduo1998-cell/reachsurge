"""ReachSurge security helpers shared by the MCP gateway and data sources.

The server is intentionally local/stdio.  These helpers still treat tool inputs
as untrusted because an MCP host may construct them from model output or remote
web content.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import socket
from typing import Any
from urllib.parse import urlsplit, urlunsplit


DEFAULT_USER_ID = os.environ.get("REACHSURGE_USER_ID", "default").strip() or "default"
_MAX_USER_ID_LENGTH = 128
_SENSITIVE_NAME = re.compile(
    r"(?:api[_-]?key|token|secret|password|passwd|authorization|cookie|smtp_password|imap_password)",
    re.IGNORECASE,
)
_URL_CREDENTIALS = re.compile(r"(https?://[^\s/:@]+:)([^@\s/]+)(@)", re.IGNORECASE)
_ASSIGNMENT_SECRET = re.compile(
    r"(?i)(\b(?:api[_-]?key|token|secret|password|passwd|authorization)\b\s*[:=]\s*)([^\s,;]+)"
)


def normalize_user_id(value: Any = None) -> str:
    """Return a bounded, non-path user namespace.

    ``user_id`` is a local storage namespace, not an authentication identity.
    Path separators and control characters are rejected instead of normalized,
    preventing two attacker-controlled values from collapsing onto one file.
    """

    user_id = DEFAULT_USER_ID if value is None or str(value).strip() == "" else str(value).strip()
    if len(user_id) > _MAX_USER_ID_LENGTH:
        raise ValueError(f"user_id 最长 {_MAX_USER_ID_LENGTH} 个字符")
    if user_id in {".", ".."} or any(ch in user_id for ch in ("/", "\\", ":", "\x00")):
        raise ValueError("user_id 不能包含路径分隔符、冒号或空字符")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in user_id):
        raise ValueError("user_id 不能包含控制字符")
    return user_id


def mcp_user_id(value: Any = None) -> str:
    """Resolve the namespace exposed to MCP calls.

    The safe default is one fixed local namespace.  Explicit caller-selected
    namespaces are an advanced compatibility mode, not tenant authentication.
    """

    allow_dynamic = os.environ.get("REACHSURGE_ALLOW_USER_NAMESPACES", "").strip().lower() in {
        "1", "true", "yes", "on"
    }
    return normalize_user_id(value if allow_dynamic else DEFAULT_USER_ID)


def storage_token(user_id: Any = None) -> str:
    """Map a validated namespace to a collision-resistant file token.

    Existing simple ASCII IDs keep their historical filename so upgrades do not
    orphan data.  All other valid IDs use a readable prefix plus a digest.
    """

    user_id = normalize_user_id(user_id)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,63}", user_id):
        return user_id
    readable = re.sub(r"[^A-Za-z0-9._@-]+", "_", user_id).strip("._")[:32] or "user"
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]
    return f"{readable}_{digest}"


def redact_mapping(value: Any) -> Any:
    """Recursively redact secrets without changing the original mapping."""

    if isinstance(value, dict):
        return {
            str(key): ("***REDACTED***" if _SENSITIVE_NAME.search(str(key)) else redact_mapping(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_mapping(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_mapping(item) for item in value)
    return value


def redact_text(value: Any) -> str:
    """Remove common credential forms from exception and diagnostic text."""

    text = str(value)
    text = _URL_CREDENTIALS.sub(r"\1***REDACTED***\3", text)
    text = _ASSIGNMENT_SECRET.sub(r"\1***REDACTED***", text)
    # Also hide exact secrets already present in this process environment.
    for name, secret in os.environ.items():
        if secret and len(secret) >= 8 and _SENSITIVE_NAME.search(name):
            text = text.replace(secret, "***REDACTED***")
    return text


def mask_url_credentials(value: str) -> str:
    """Return only a proxy URL's scheme and host/port for status output.

    User names, passwords, paths, queries and fragments can all carry secrets,
    so status output deliberately drops them instead of guessing which
    individual component is sensitive.
    """

    if not value:
        return ""
    try:
        parts = urlsplit(value)
        if not parts.hostname:
            return redact_text(value)
        host = parts.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parts.port}" if parts.port else ""
        return urlunsplit((parts.scheme, f"{host}{port}", "", "", ""))
    except (ValueError, TypeError):
        return "[invalid proxy URL]"


def _private_network_allowed() -> bool:
    return os.environ.get("REACHSURGE_ALLOW_PRIVATE_NETWORK", "").strip().lower() in {
        "1", "true", "yes", "on"
    }


def validate_public_http_url(url: str) -> str:
    """Validate an outbound website URL and block local/private destinations.

    Set ``REACHSURGE_ALLOW_PRIVATE_NETWORK=1`` only when intentionally scraping
    an intranet.  Redirect targets must be validated separately by callers.
    """

    raw = (url or "").strip()
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("只允许有效的 http/https 网站地址")
    if parts.username or parts.password:
        raise ValueError("网站地址不能包含用户名或密码")
    if _private_network_allowed():
        return raw

    hostname = parts.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
        raise ValueError("出于安全原因，禁止访问本机或内网地址")

    try:
        literal = ipaddress.ip_address(hostname)
        addresses = [literal]
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(hostname, parts.port or 443, type=socket.SOCK_STREAM)
            }
        except socket.gaierror as exc:
            raise ValueError("网站域名无法解析") from exc

    for address in addresses:
        if not address.is_global:
            raise ValueError("出于安全原因，禁止访问本机、私网、链路本地或保留地址")
    return raw


def validate_public_host(host: str, port: int) -> str:
    """Block SMTP/IMAP connections to local/private destinations by default."""

    hostname = (host or "").strip().rstrip(".")
    if not hostname:
        raise ValueError("服务器地址不能为空")
    if not 1 <= int(port) <= 65535:
        raise ValueError("端口必须在 1 到 65535 之间")
    if _private_network_allowed():
        return hostname
    lower = hostname.lower()
    if lower == "localhost" or lower.endswith((".localhost", ".local", ".internal")):
        raise ValueError("出于安全原因，禁止连接本机或内网邮件服务器")
    try:
        addresses = {
            ipaddress.ip_address(item[4][0])
            for item in socket.getaddrinfo(hostname, int(port), type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise ValueError("邮件服务器域名无法解析") from exc
    if any(not address.is_global for address in addresses):
        raise ValueError("出于安全原因，禁止连接本机、私网、链路本地或保留地址")
    return hostname

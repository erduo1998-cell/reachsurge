"""Client-independent first-run state for ReachSurge MCP.

The onboarding marker contains no credentials. API keys and mail passwords stay
in the local .env file and are exposed here only as configured/not configured.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from security import DEFAULT_USER_ID, storage_token
from storage.db import BASE_DATA_DIR, get_user_profile


SCHEMA_VERSION = 1
APP_VERSION = "0.1.0"
_STALE_TEMP_SECONDS = 24 * 60 * 60

PROVIDER_GUIDES = {
    "deepseek": {
        "purpose": "LLM 产品词提取、线索过滤和公司画像",
        "env": "DEEPSEEK_API_KEY",
        "signup": "https://platform.deepseek.com/sign_up",
        "key_page": "https://platform.deepseek.com/api_keys",
        "docs": "https://api-docs.deepseek.com/",
        "steps": [
            "打开 signup 注册并登录 DeepSeek 开放平台",
            "进入 key_page，创建一个专用于 ReachSurge 的 Key",
            "不要发到聊天；在本机 .env 写入 DEEPSEEK_API_KEY",
            "重启 ReachSurge MCP，再调用 setup_status 检查",
        ],
    },
    "serpapi": {
        "purpose": "Google Maps 企业搜索",
        "env": "SERPAPI_API_KEYS",
        "signup": "https://serpapi.com/users/sign_up",
        "key_page": "https://serpapi.com/manage-api-key",
        "docs": "https://serpapi.com/google-maps-api",
        "steps": [
            "打开 signup 创建 SerpApi 账户并登录",
            "进入 key_page 获取 API Key",
            "不要发到聊天；在本机 .env 写入 SERPAPI_API_KEYS；多个 Key 用英文逗号分隔",
            "重启 ReachSurge MCP，再调用 setup_status 检查",
        ],
    },
    "hunter": {
        "purpose": "企业发现和公开邮箱富集",
        "env": "HUNTER_API_KEY",
        "signup": "https://hunter.io/users/sign_up",
        "key_page": "https://hunter.io/api-keys",
        "docs": "https://hunter.io/api-documentation",
        "steps": [
            "打开 signup 创建 Hunter 账户并登录",
            "进入 key_page 创建一个独立 API Key",
            "不要发到聊天；在本机 .env 写入 HUNTER_API_KEY",
            "重启 ReachSurge MCP，再调用 setup_status 检查",
        ],
    },
    "tavily": {
        "purpose": "展会、展商和公开网页搜索",
        "env": "TAVILY_API_KEYS",
        "signup": "https://app.tavily.com/",
        "key_page": "https://app.tavily.com/",
        "docs": "https://docs.tavily.com/documentation/quickstart",
        "steps": [
            "打开 signup 注册或登录 Tavily 控制台",
            "在控制台创建或复制 API Key",
            "不要发到聊天；在本机 .env 写入 TAVILY_API_KEYS；多个 Key 用英文逗号分隔",
            "重启 ReachSurge MCP，再调用 setup_status 检查",
        ],
    },
    "capsolver": {
        "purpose": "Europages 的可选 WAF 处理",
        "env": "CAPSOLVER_API_KEY",
        "signup": "https://dashboard.capsolver.com/",
        "key_page": "https://dashboard.capsolver.com/",
        "docs": "https://docs.capsolver.com/en/guide/getting-started/",
        "steps": [
            "打开 signup 注册或登录 CapSolver 控制台",
            "在控制台首页获取 API Key，并确认账户可用余额",
            "不要发到聊天；在本机 .env 写入 CAPSOLVER_API_KEY",
            "重启 ReachSurge MCP，再调用 setup_status 检查",
        ],
    },
}


def _state_dir() -> Path:
    return Path(BASE_DATA_DIR) / "state"


def _state_paths(user_id: str) -> tuple[Path, Path]:
    token = storage_token(user_id)
    root = _state_dir()
    return (
        root / f"onboarding-{token}.pending.json",
        root / f"onboarding-{token}.complete.json",
    )


def _env_file() -> Path:
    configured = (os.environ.get("LEADGEN_ENV_FILE") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parent / ".env"


def _first_existing(*paths: Path) -> Path | None:
    return next((path for path in paths if path.is_file()), None)


def _installed_asset(relative_path: str) -> Path | None:
    """Resolve an asset in a source checkout or an installed wheel."""
    module_root = Path(__file__).resolve().parent
    return _first_existing(
        module_root / relative_path,
        Path(sys.prefix) / "share" / "reachsurge" / relative_path,
    )


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _atomic_private_json(path: Path, payload: dict[str, Any]) -> None:
    _ensure_private_dir(path.parent)
    temp = path.parent / f".onboarding-{uuid.uuid4().hex}.tmp"
    fd: int | None = None
    try:
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        if fd is not None:
            os.close(fd)
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _cleanup_stale_temps() -> None:
    root = _state_dir()
    if not root.is_dir():
        return
    cutoff = time.time() - _STALE_TEMP_SECONDS
    for path in root.glob(".onboarding-*.tmp"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def _storage_writable() -> tuple[bool, str]:
    root = _state_dir()
    probe = root / f".onboarding-write-probe-{uuid.uuid4().hex}.tmp"
    fd: int | None = None
    try:
        _ensure_private_dir(root)
        fd = os.open(str(probe), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, b"ok")
        os.fsync(fd)
        return True, ""
    except OSError as exc:
        return False, f"首次运行状态目录不可写: {type(exc).__name__}"
    finally:
        if fd is not None:
            os.close(fd)
        try:
            probe.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _load_marker(path: Path) -> tuple[dict[str, Any] | None, str]:
    if not path.exists():
        return None, ""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "首次运行完成标记损坏，需要重新完成本地向导"
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        return None, "首次运行完成标记版本不兼容，需要重新完成本地向导"
    return value, ""


def _profile_state(user_id: str) -> tuple[dict[str, Any], list[str]]:
    profile = get_user_profile(user_id) or {}
    missing = []
    if not str(profile.get("industry") or "").strip():
        missing.append("industry（行业/产品类别）")
    if not str(profile.get("product_description") or "").strip():
        missing.append("product_description（产品、优势、规格等描述）")
    markets = profile.get("target_markets") or []
    if isinstance(markets, str):
        markets = [item.strip() for item in markets.split(",") if item.strip()]
    if not markets:
        missing.append("target_markets（目标国家或地区）")
    return profile, missing


def _configured(name: str) -> bool:
    return bool((os.environ.get(name) or "").strip())


def _enabled(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _capabilities() -> dict[str, Any]:
    capabilities: dict[str, Any] = {
        "zero_key_local": {
            "configured": True,
            "verified": True,
            "purpose": "本地 CRM、知识库和 OSM 等零 Key 能力",
        }
    }
    for key, guide in PROVIDER_GUIDES.items():
        capabilities[key] = {
            "configured": _configured(guide["env"]),
            "verified": False,
            **guide,
        }

    try:
        from sources.gosom import availability as gosom_availability

        gosom_ready, _ = gosom_availability()
    except Exception:
        gosom_ready = False
    capabilities["gosom"] = {
        "configured": gosom_ready,
        "verified": False,
        "purpose": "可选的本机 Google Maps 抓取",
        "env": "GOSOM_BIN",
        "guide": "docs/GOSOM_AGENT_FIX_GUIDE.zh-CN.md",
    }
    capabilities["playwright"] = {
        "configured": importlib.util.find_spec("playwright") is not None,
        "verified": False,
        "purpose": "Europages / ImportYeti 可选浏览器能力；这里只检查 Python 包",
    }
    capabilities["smtp_send"] = {
        "configured": _enabled("REACHSURGE_ENABLE_SEND_EMAIL")
        and _configured("REACHSURGE_SMTP_HOST")
        and _configured("REACHSURGE_SMTP_USER")
        and _configured("REACHSURGE_SMTP_PASSWORD"),
        "verified": False,
        "purpose": "真实发送邮件；首次运行不要求开启",
    }
    capabilities["imap_inbox"] = {
        "configured": _enabled("REACHSURGE_ENABLE_CHECK_INBOX")
        and _configured("REACHSURGE_IMAP_HOST")
        and _configured("REACHSURGE_IMAP_USER")
        and _configured("REACHSURGE_IMAP_PASSWORD"),
        "verified": False,
        "purpose": "读取收件箱；首次运行不要求开启",
    }
    return capabilities


def _secret_env_present() -> bool:
    """Check only whether secrets exist; never read them into status output."""
    return any(
        _configured(name)
        for name in (
            "DEEPSEEK_API_KEY",
            "SERPAPI_API_KEYS",
            "HUNTER_API_KEY",
            "TAVILY_API_KEYS",
            "CAPSOLVER_API_KEY",
            "REACHSURGE_SMTP_PASSWORD",
            "REACHSURGE_IMAP_PASSWORD",
        )
    )


def _env_security() -> tuple[bool | None, str]:
    env_file = _env_file()
    if not env_file.exists() or os.name == "nt":
        return None, ""
    try:
        mode = stat.S_IMODE(env_file.stat().st_mode)
    except OSError as exc:
        return False, f"无法检查 .env 权限: {type(exc).__name__}"
    secure = mode & 0o077 == 0
    if _secret_env_present() and not secure:
        return False, ".env 已配置秘密，但仍允许同组或其他本机用户读取；请执行 chmod 600"
    return secure, "" if secure else ".env 当前未检测到已启用秘密；配置 Key 前请执行 chmod 600"


def _pending_payload(user_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "profile_namespace": user_id,
    }


def is_setup_complete(user_id: str = DEFAULT_USER_ID) -> bool:
    """Fast, side-effect-free gate check used by the MCP dispatcher."""
    _, complete_path = _state_paths(user_id)
    marker, error = _load_marker(complete_path)
    if error or not marker or marker.get("profile_namespace") != user_id:
        return False
    _, missing = _profile_state(user_id)
    env_secure, _ = _env_security()
    return not missing and not (_secret_env_present() and env_secure is False)


def setup_status(user_id: str = DEFAULT_USER_ID, *, create_pending: bool = True) -> dict[str, Any]:
    """Return local-only onboarding status without revealing any credentials."""
    _cleanup_stale_temps()
    storage_ready, storage_error = _storage_writable()
    pending_path, complete_path = _state_paths(user_id)
    profile, missing = _profile_state(user_id)
    marker, marker_error = _load_marker(complete_path)
    if marker and marker.get("profile_namespace") != user_id:
        marker = None
        marker_error = "首次运行完成标记与当前本地命名空间不匹配，需要重新完成向导"
    capabilities = _capabilities()
    env_secure, env_warning = _env_security()

    security_blocked = _secret_env_present() and env_secure is False
    complete = (
        bool(marker) and not marker_error and not missing and storage_ready
        and not security_blocked
    )
    if not storage_ready:
        phase = "blocked"
    elif marker_error or (marker and missing):
        phase = "needs_repair"
    elif security_blocked:
        phase = "needs_security_fix"
    elif complete:
        phase = "complete"
    elif missing:
        phase = "needs_profile"
    else:
        phase = "ready_to_complete"

    cleanup_warning = ""
    if complete and pending_path.exists():
        try:
            pending_path.unlink()
        except OSError as exc:
            cleanup_warning = f"首次运行已完成，但临时状态文件清理失败: {type(exc).__name__}"

    if create_pending and not complete and storage_ready and not pending_path.exists():
        _atomic_private_json(pending_path, _pending_payload(user_id))

    actions = []
    if phase == "blocked":
        actions.append("修复 LEADGEN_DATA_DIR/状态目录写权限，然后重启 ReachSurge")
    elif phase == "needs_profile":
        actions.append("只询问非秘密业务信息，然后一次调用 save_user_config 保存缺失字段")
    elif phase == "needs_security_fix":
        actions.append("在本机修复 .env 文件权限；不要在聊天中发送 .env 或任何秘密")
    elif phase in {"ready_to_complete", "needs_repair"}:
        actions.append("询问用户是否需要可选增强能力；零 Key 也可以完成")
        actions.append("用户确认本地配置结束后调用 complete_setup")
    elif phase == "complete":
        actions.append("首次设置已完成，不要重复询问；直接执行用户的业务任务")

    local_guide = _installed_asset("docs/FIRST_RUN_GUIDE.zh-CN.md")
    env_template = _installed_asset(".env.example")
    return {
        "phase": phase,
        "onboarding_complete": complete,
        "profile": {
            "configured": not missing,
            "missing_fields": missing,
            "saved_name": bool(profile.get("name")),
        },
        "local_checks": {
            "data_dir": str(Path(BASE_DATA_DIR).resolve()),
            "storage_writable": storage_ready,
            "storage_error": storage_error,
            "env_file": str(_env_file()),
            "env_file_exists": _env_file().is_file(),
            "env_template": str(env_template) if env_template else "",
            "env_permissions_secure": env_secure,
            "env_warning": env_warning,
            "cleanup_warning": cleanup_warning,
        },
        "capabilities": capabilities,
        "required_user_inputs": {
            "required_non_secret": [
                "行业或产品类别",
                "产品描述（建议含优势、规格、认证、MOQ、交期）",
                "目标国家或地区",
            ],
            "optional_non_secret": ["用户称呼", "每日发信偏好上限"],
            "never_request_in_chat": [
                "API Key / Token",
                "SMTP/IMAP 密码或应用专用密码",
                "Cookie、代理密码、完整 .env",
            ],
        },
        "required_actions": actions,
        "guide": {
            "local_file": str(local_guide) if local_guide else "",
            "online": "https://github.com/erduo1998-cell/reachsurge/blob/main/docs/FIRST_RUN_GUIDE.zh-CN.md",
        },
        "temporary_state_file": {
            "exists": pending_path.exists(),
            "will_be_deleted_by": "complete_setup",
        },
        "next_tool": {
            "complete": "none",
            "needs_profile": "save_user_config",
            "ready_to_complete": "complete_setup",
            "needs_repair": "complete_setup",
            "needs_security_fix": "setup_status（本机修复并重启 MCP 后）",
            "blocked": "setup_status（修复目录权限并重启 MCP 后）",
        }[phase],
    }


def complete_setup(user_id: str = DEFAULT_USER_ID) -> dict[str, Any]:
    """Validate local prerequisites, write a private marker, and remove pending state."""
    status = setup_status(user_id, create_pending=True)
    if not status["local_checks"]["storage_writable"]:
        raise RuntimeError(status["local_checks"]["storage_error"] or "首次运行状态目录不可写")
    if status["profile"]["missing_fields"]:
        missing = "、".join(status["profile"]["missing_fields"])
        raise ValueError(f"首次设置尚未完成，缺少: {missing}")
    if status["phase"] == "needs_security_fix":
        raise ValueError(status["local_checks"]["env_warning"])

    _, complete_path = _state_paths(user_id)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "app_version": APP_VERSION,
        "profile_namespace": user_id,
    }
    _atomic_private_json(complete_path, payload)
    result = setup_status(user_id, create_pending=False)
    if result["local_checks"]["cleanup_warning"]:
        result["message"] = "首次设置已完成；临时文件将在下次状态检查时继续自动清理。"
    else:
        result["message"] = "首次设置已完成；临时首次运行文件已删除，下次启动不会重复向导。"
    return result


def server_instructions(user_id: str = DEFAULT_USER_ID) -> str:
    if is_setup_complete(user_id):
        return (
            "ReachSurge 首次设置已完成，不要重复向用户索取产品和市场信息。"
            "仍不得索取或接收 API Key、Token、SMTP/IMAP 密码、Cookie 或完整 .env。"
            "找客户优先调用 search_leads，取得 task_id 后调用 get_task_status。"
        )
    return (
        "在执行任何 ReachSurge 业务前必须先调用 setup_status，并逐项完成首次向导。"
        "只能通过 save_user_config 收集非秘密的产品、行业和目标市场。"
        "绝不要求用户把 API Key、Token、邮箱密码、Cookie、代理密码或完整 .env 粘贴到聊天或工具。"
        "可选 Key 由用户在聊天之外写入 setup_status 指示的本机 .env；重启 MCP 后重新检查。"
        "零 Key 也可完成；满足本地条件后调用 complete_setup。"
    )

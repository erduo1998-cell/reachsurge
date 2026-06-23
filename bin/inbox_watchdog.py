#!/usr/bin/env python3
"""ReachSurge 邮箱 watchdog 逻辑（由 cron no-agent 的 .sh wrapper 调起）。

用项目 venv 跑（MCP 客户端自身的 venv 没有 cryptography 等
leadgen 依赖）。复用 mcp_server._handle_check_inbox 拉新邮件入库 inquiries。
有新回复 → stdout 输出摘要；无新/未配 IMAP/出错 → 静默。
"""
import sys, os
sys.path.insert(0, "/home/erduo/leadgen-pipeline")
os.chdir("/home/erduo/leadgen-pipeline")

USER_ID = os.environ.get("REACHSURGE_USER_ID") or "REPLACE_WITH_YOUR_OPEN_ID"

try:
    from mcp_server import _handle_check_inbox
    result = _handle_check_inbox({"user_id": USER_ID, "limit": 20})
    # 只"📬 收到 N 封"才输出；其余静默
    if isinstance(result, str) and result.startswith("📬"):
        print(result)
except Exception as e:
    print(f"⚠️ inbox_watchdog 异常: {type(e).__name__}: {e}")

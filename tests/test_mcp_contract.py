import asyncio
import imaplib
import smtplib
import ssl

import pytest

import mcp_server


def _tool(name):
    return next(tool for tool in mcp_server.TOOLS if tool.name == name)


def test_tool_names_are_unique_and_complete():
    names = [tool.name for tool in mcp_server.TOOLS]
    assert len(names) == 20
    assert len(names) == len(set(names))


def test_all_schemas_are_closed_and_user_id_is_optional():
    for tool in mcp_server.TOOLS:
        schema = tool.inputSchema
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert "user_id" not in schema.get("required", [])


def test_mail_credentials_and_endpoints_are_not_model_arguments():
    properties = _tool("save_user_config").inputSchema["properties"]
    forbidden = {
        "smtp_host", "smtp_port", "smtp_user", "smtp_password",
        "imap_host", "imap_port", "imap_user", "imap_password",
    }
    assert forbidden.isdisjoint(properties)


def test_send_email_requires_confirmation_and_is_disabled_by_default(monkeypatch):
    schema = _tool("send_email").inputSchema
    assert "confirm_send" in schema["required"]
    monkeypatch.delenv("REACHSURGE_ENABLE_SEND_EMAIL", raising=False)
    result = mcp_server._handle_send_email({
        "user_id": "default",
        "to_email": "person@example.com",
        "subject": "test",
        "body": "test",
        "confirm_send": True,
    })
    assert "默认关闭" in result


def test_model_config_cannot_raise_server_daily_send_limit(monkeypatch):
    monkeypatch.setenv("REACHSURGE_ENABLE_SEND_EMAIL", "1")
    monkeypatch.setenv("REACHSURGE_DAILY_SEND_LIMIT", "1")
    monkeypatch.setenv("REACHSURGE_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("REACHSURGE_SMTP_USER", "sender@example.com")
    monkeypatch.setenv("REACHSURGE_SMTP_PASSWORD", "app-password")
    monkeypatch.setattr(mcp_server, "validate_public_host", lambda *args: None)
    monkeypatch.setattr(mcp_server, "get_user_config", lambda *args: {"daily_send_limit": 500})
    monkeypatch.setattr(mcp_server, "reserve_outreach_send", lambda *args: (None, 1))
    result = mcp_server._handle_send_email({
        "user_id": "default", "to_email": "to@example.com", "subject": "s",
        "body": "b", "confirm_send": True,
    })
    assert "达到每日上限 1 封" in result


def test_inbox_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("REACHSURGE_ENABLE_CHECK_INBOX", raising=False)
    result = mcp_server._handle_check_inbox({"user_id": "default", "limit": 1})
    assert "默认关闭" in result


def test_server_side_validation_rejects_bad_values_and_extra_fields():
    with pytest.raises(ValueError):
        mcp_server._arguments_with_defaults("list_leads", {"limit": 0})
    with pytest.raises(ValueError):
        mcp_server._arguments_with_defaults("social_profile_lookup", {
            "platform": "facebook", "username": "demo"
        })
    with pytest.raises(ValueError):
        mcp_server._arguments_with_defaults("list_leads", {"unknown": True})


def test_tool_errors_use_standard_mcp_error_flag():
    result = asyncio.run(mcp_server.call_tool("list_leads", {"limit": 0}))
    assert result.isError is True


def test_model_selected_user_id_is_ignored_by_default(monkeypatch):
    monkeypatch.setenv("REACHSURGE_ALLOW_USER_NAMESPACES", "0")
    values = mcp_server._arguments_with_defaults("list_leads", {"user_id": "victim"})
    assert values["user_id"] == mcp_server.DEFAULT_USER_ID


def test_search_leads_returns_task_id_without_waiting(monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, *args, **kwargs):
            started.append((args, kwargs))

        def start(self):
            return None

    monkeypatch.setattr(mcp_server, "create_task", lambda *args, **kwargs: "task-123")
    monkeypatch.setattr(mcp_server.threading, "Thread", FakeThread)
    monkeypatch.setattr(mcp_server, "_reserve_task_slot", lambda user_id: True)
    result = mcp_server._handle_search_leads({"user_id": "default", "query": "LED Germany"})
    assert "task-123" in result
    assert started


def test_smtp_uses_verified_tls_context(monkeypatch):
    captured = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout, context):
            captured["context"] = context

        def login(self, *args): pass
        def sendmail(self, *args): pass
        def quit(self): pass

    monkeypatch.setenv("REACHSURGE_ENABLE_SEND_EMAIL", "1")
    monkeypatch.setenv("REACHSURGE_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("REACHSURGE_SMTP_PORT", "465")
    monkeypatch.setenv("REACHSURGE_SMTP_USER", "sender@example.com")
    monkeypatch.setenv("REACHSURGE_SMTP_PASSWORD", "app-password")
    monkeypatch.setattr(mcp_server, "validate_public_host", lambda *args: None)
    monkeypatch.setattr(mcp_server, "get_user_config", lambda *args: {"daily_send_limit": 30})
    monkeypatch.setattr(mcp_server, "reserve_outreach_send", lambda *args: ("record", 1))
    monkeypatch.setattr(mcp_server, "finish_outreach_send", lambda *args, **kwargs: None)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)

    result = mcp_server._handle_send_email({
        "user_id": "default", "to_email": "to@example.com", "subject": "s",
        "body": "b", "confirm_send": True,
    })
    assert "已发送" in result
    assert captured["context"].check_hostname is True
    assert captured["context"].verify_mode == ssl.CERT_REQUIRED


def test_failed_send_finalizes_reserved_record(monkeypatch):
    finalized = []

    class FailingSMTP:
        def __init__(self, *args, **kwargs):
            raise smtplib.SMTPException("authentication failed")

    monkeypatch.setenv("REACHSURGE_ENABLE_SEND_EMAIL", "1")
    monkeypatch.setenv("REACHSURGE_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("REACHSURGE_SMTP_PORT", "465")
    monkeypatch.setenv("REACHSURGE_SMTP_USER", "sender@example.com")
    monkeypatch.setenv("REACHSURGE_SMTP_PASSWORD", "app-password")
    monkeypatch.setattr(mcp_server, "validate_public_host", lambda *args: None)
    monkeypatch.setattr(mcp_server, "get_user_config", lambda *args: {})
    monkeypatch.setattr(mcp_server, "reserve_outreach_send", lambda *args: ("record", 1))
    monkeypatch.setattr(mcp_server, "finish_outreach_send", lambda *args: finalized.append(args))
    monkeypatch.setattr(smtplib, "SMTP_SSL", FailingSMTP)

    result = mcp_server._handle_send_email({
        "user_id": "default", "to_email": "to@example.com", "subject": "s",
        "body": "b", "confirm_send": True,
    })
    assert "发送失败" in result
    assert finalized and finalized[0][:3] == ("default", "record", "failed")


def test_imap_uses_verified_tls_context(monkeypatch, tmp_path):
    captured = {}

    class FakeIMAP:
        def __init__(self, host, port, ssl_context):
            captured["context"] = ssl_context

        def login(self, *args): pass
        def select(self, *args): pass
        def uid(self, command, *args):
            return ("OK", [b""]) if command == "search" else ("NO", [])
        def logout(self): pass

    monkeypatch.setenv("REACHSURGE_ENABLE_CHECK_INBOX", "1")
    monkeypatch.setenv("REACHSURGE_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("REACHSURGE_IMAP_PORT", "993")
    monkeypatch.setenv("REACHSURGE_IMAP_USER", "reader@example.com")
    monkeypatch.setenv("REACHSURGE_IMAP_PASSWORD", "app-password")
    monkeypatch.setattr(mcp_server, "validate_public_host", lambda *args: None)
    monkeypatch.setattr(mcp_server, "get_user_config", lambda *args: {})
    monkeypatch.setattr(mcp_server, "DATA_DIR", tmp_path)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)

    result = mcp_server._handle_check_inbox({"user_id": "default", "limit": 1})
    assert "没有新回复" in result
    assert captured["context"].check_hostname is True
    assert captured["context"].verify_mode == ssl.CERT_REQUIRED


def test_inbox_skips_oversized_message_before_fetching_body(monkeypatch, tmp_path):
    calls = []

    class FakeIMAP:
        def __init__(self, *args, **kwargs): pass
        def login(self, *args): pass
        def select(self, *args): pass
        def uid(self, command, *args):
            calls.append((command, args))
            if command == "search":
                return "OK", [b"42"]
            if args[-1] == "(RFC822.SIZE)":
                return "OK", [b"42 (UID 42 RFC822.SIZE 9999999)"]
            raise AssertionError("oversized RFC822 body must not be fetched")
        def logout(self): pass

    monkeypatch.setenv("REACHSURGE_ENABLE_CHECK_INBOX", "1")
    monkeypatch.setenv("REACHSURGE_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("REACHSURGE_IMAP_USER", "reader@example.com")
    monkeypatch.setenv("REACHSURGE_IMAP_PASSWORD", "app-password")
    monkeypatch.setenv("REACHSURGE_MAX_EMAIL_BYTES", "1024")
    monkeypatch.setattr(mcp_server, "validate_public_host", lambda *args: None)
    monkeypatch.setattr(mcp_server, "DATA_DIR", tmp_path)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)

    result = mcp_server._handle_check_inbox({"user_id": "default", "limit": 1})
    assert "已跳过 1 封" in result
    assert not any(args and args[-1] == "(RFC822)" for _, args in calls)

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from sources import gosom


def _enable_gosom(monkeypatch, tmp_path):
    monkeypatch.delenv("GOSOM_BIN", raising=False)
    monkeypatch.setattr(gosom, "ENABLED", True)
    monkeypatch.setattr(gosom, "BIN", sys.executable)
    monkeypatch.setattr(gosom.tempfile, "tempdir", str(tmp_path))


def test_gosom_removes_temporary_files_after_empty_result(tmp_path, monkeypatch):
    _enable_gosom(monkeypatch, tmp_path)
    monkeypatch.setattr(gosom.subprocess, "run", lambda *args, **kwargs: None)
    assert gosom.search("LED", "Germany", 1) == []
    assert list(tmp_path.glob("gosom_*")) == []


def test_gosom_removes_temporary_files_after_failure(tmp_path, monkeypatch):
    _enable_gosom(monkeypatch, tmp_path)

    def fail(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(gosom.subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="boom"):
        gosom.search("LED", "Germany", 1)
    assert list(tmp_path.glob("gosom_*")) == []


def test_missing_optional_binary_is_a_clean_downgrade(tmp_path, monkeypatch):
    missing = tmp_path / "google_maps_scraper"
    called = False

    def should_not_run(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.delenv("GOSOM_BIN", raising=False)
    monkeypatch.setattr(gosom, "ENABLED", True)
    monkeypatch.setattr(gosom, "BIN", str(missing))
    monkeypatch.setattr(gosom.subprocess, "run", should_not_run)

    available, reason = gosom.availability()
    assert available is False
    assert "不影响其他来源" in reason
    assert gosom.search("LED", "Germany", 1) == []
    assert called is False


def test_blank_env_uses_default_candidate(tmp_path, monkeypatch):
    binary = tmp_path / "google_maps_scraper"
    binary.write_text("placeholder")
    binary.chmod(0o700)
    monkeypatch.setenv("GOSOM_BIN", "   ")
    monkeypatch.setattr(gosom, "BIN", str(binary))

    assert gosom._resolve_binary() == binary.resolve()
    assert gosom.is_available() is True


def test_env_change_after_import_is_respected(tmp_path, monkeypatch):
    binary = tmp_path / "含 空格" / "google_maps_scraper"
    binary.parent.mkdir()
    binary.write_text("placeholder")
    binary.chmod(0o700)
    monkeypatch.setenv("GOSOM_BIN", str(binary))

    assert gosom._resolve_binary() == binary.resolve()
    assert gosom.is_available() is True


@pytest.mark.skipif(sys.platform == "win32", reason="Windows does not use POSIX execute bits")
def test_non_executable_binary_is_a_clean_downgrade(tmp_path, monkeypatch):
    binary = tmp_path / "google_maps_scraper"
    binary.write_text("placeholder")
    binary.chmod(0o600)
    monkeypatch.delenv("GOSOM_BIN", raising=False)
    monkeypatch.setattr(gosom, "ENABLED", True)
    monkeypatch.setattr(gosom, "BIN", str(binary))

    available, reason = gosom.availability()
    assert available is False
    assert "执行权限" in reason


def test_registry_skips_missing_gosom_without_recording_an_error(tmp_path, monkeypatch):
    import registry

    monkeypatch.delenv("GOSOM_BIN", raising=False)
    monkeypatch.setattr(gosom, "ENABLED", True)
    monkeypatch.setattr(gosom, "BIN", str(tmp_path / "missing"))
    monkeypatch.setattr(
        registry,
        "SOURCE_REGISTRY",
        {
            "gosom_maps": {
                "module": gosom,
                "enabled": gosom.is_available,
                "weight": 1.2,
                "kind": "archive",
            }
        },
    )

    assert registry.enabled_sources() == []
    assert registry.orchestrate("LED", "Germany", sources=["gosom_maps"]) == []
    assert registry.last_errors() == []


def test_registry_keeps_other_sources_when_gosom_is_missing(tmp_path, monkeypatch):
    import registry
    from sources.base import LeadCandidate

    class WorkingSource:
        @staticmethod
        def search(**kwargs):
            return [LeadCandidate(company_name="Working Company", score=80)]

    monkeypatch.delenv("GOSOM_BIN", raising=False)
    monkeypatch.setattr(gosom, "ENABLED", True)
    monkeypatch.setattr(gosom, "BIN", str(tmp_path / "missing"))
    monkeypatch.setattr(
        registry,
        "SOURCE_REGISTRY",
        {
            "gosom_maps": {
                "module": gosom,
                "enabled": gosom.is_available,
                "weight": 1.2,
                "kind": "archive",
            },
            "working": {
                "module": WorkingSource,
                "enabled": True,
                "weight": 1.0,
                "kind": "archive",
            },
        },
    )

    leads = registry.orchestrate("LED", "Germany")
    assert [lead.company_name for lead in leads] == ["Working Company"]
    assert registry.last_errors() == []


def test_explicit_missing_binary_is_reported_as_configuration_error(tmp_path, monkeypatch):
    import registry

    monkeypatch.setenv("GOSOM_BIN", str(tmp_path / "missing"))
    monkeypatch.setattr(gosom, "ENABLED", True)
    monkeypatch.setattr(
        registry,
        "SOURCE_REGISTRY",
        {
            "gosom_maps": {
                "module": gosom,
                "enabled": gosom.is_available,
                "configuration_error": gosom.configuration_error,
                "weight": 1.2,
                "kind": "archive",
            }
        },
    )

    with pytest.raises(RuntimeError, match="配置无效"):
        gosom.search("LED", "Germany", 1)
    assert registry.orchestrate("LED", "Germany") == []
    assert "配置错误" in registry.last_errors()[0]


def test_explicit_relative_binary_is_rejected(monkeypatch):
    monkeypatch.setenv("GOSOM_BIN", "bin/google_maps_scraper")
    monkeypatch.setattr(gosom, "ENABLED", True)

    available, reason = gosom.availability()
    assert available is False
    assert "绝对路径" in reason


def test_nonzero_exit_is_reported(tmp_path, monkeypatch):
    _enable_gosom(monkeypatch, tmp_path)
    monkeypatch.setattr(
        gosom.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=23),
    )

    with pytest.raises(RuntimeError, match="退出码 23"):
        gosom.search("LED", "Germany", 1)


def test_timeout_is_reported(tmp_path, monkeypatch):
    _enable_gosom(monkeypatch, tmp_path)

    def timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(gosom.subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="运行超时"):
        gosom.search("LED", "Germany", 1)


def test_utf8_query_and_jsonl_result(tmp_path, monkeypatch):
    _enable_gosom(monkeypatch, tmp_path)

    def produce_result(cmd, **kwargs):
        query_path = cmd[cmd.index("-input") + 1]
        result_path = cmd[cmd.index("-results") + 1]
        with open(query_path, encoding="utf-8") as handle:
            assert "中文 LED" in handle.read()
        row = {
            "title": "慕尼黑 LED 经销商",
            "website": "https://example.com",
            "complete_address": {"country": "DE"},
            "categories": ["LED Händler"],
        }
        with open(result_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(gosom.subprocess, "run", produce_result)
    leads = gosom.search("中文 LED", "德国", 1)
    assert [lead.company_name for lead in leads] == ["慕尼黑 LED 经销商"]
    assert leads[0].country == "Germany"


def test_subprocess_does_not_receive_secrets_or_proxy_credentials(tmp_path, monkeypatch):
    _enable_gosom(monkeypatch, tmp_path)
    proxy_secret = "proxy-password-very-secret"
    api_secret = "api-key-very-secret"
    mail_secret = "mail-password-very-secret"
    captured = {}

    monkeypatch.setenv("LEADGEN_PROXY", f"http://user:{proxy_secret}@proxy.example:8080")
    monkeypatch.setenv("DEEPSEEK_API_KEY", api_secret)
    monkeypatch.setenv("REACHSURGE_SMTP_PASSWORD", mail_secret)

    def capture(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        captured["cwd"] = kwargs["cwd"]
        proxy_index = cmd.index("-proxies-file") + 1
        proxy_path = cmd[proxy_index]
        captured["proxy_path"] = proxy_path
        with open(proxy_path, encoding="utf-8") as handle:
            captured["proxy_contents"] = handle.read()
        if sys.platform != "win32":
            captured["proxy_mode"] = oct(os.stat(proxy_path).st_mode & 0o777)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(gosom.subprocess, "run", capture)
    assert gosom.search("中文 LED", "德国", 1) == []

    command_text = " ".join(captured["cmd"])
    env_text = repr(captured["env"])
    assert "-proxies-file" in captured["cmd"]
    assert proxy_secret not in command_text
    assert proxy_secret not in env_text
    assert api_secret not in env_text
    assert mail_secret not in env_text
    assert captured["env"]["DISABLE_TELEMETRY"] == "1"
    assert captured["cwd"].startswith(str(tmp_path))
    assert proxy_secret in captured["proxy_contents"]
    if sys.platform != "win32":
        assert captured["proxy_mode"] == "0o600"
    assert not os.path.exists(captured["proxy_path"])


def test_registry_redacts_provider_secret_from_adapter_error(monkeypatch):
    import registry

    secret = "provider-secret-that-must-not-leak"

    class FailingSource:
        @staticmethod
        def search(**kwargs):
            raise RuntimeError(f"request failed with token={secret}")

    monkeypatch.setenv("HUNTER_API_KEY", secret)
    monkeypatch.setattr(
        registry,
        "SOURCE_REGISTRY",
        {
            "failing": {
                "module": FailingSource,
                "enabled": True,
                "weight": 1.0,
                "kind": "archive",
            }
        },
    )

    assert registry.orchestrate("LED") == []
    error = registry.last_errors()[0]
    assert secret not in error
    assert "***REDACTED***" in error

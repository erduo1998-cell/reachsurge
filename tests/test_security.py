import ssl

import pytest

from security import (
    mcp_user_id,
    mask_url_credentials,
    normalize_user_id,
    redact_mapping,
    redact_text,
    storage_token,
    validate_public_host,
    validate_public_http_url,
)


def test_user_id_rejects_path_and_control_characters():
    for value in ("a/b", "a\\b", "..", "bad\x00id", "bad\nid"):
        with pytest.raises(ValueError):
            normalize_user_id(value)


def test_storage_tokens_do_not_collapse_unicode_ids():
    assert storage_token("耳总") != storage_token("耳朵")
    assert storage_token("trial") == "trial"


def test_mcp_namespace_is_fixed_by_default(monkeypatch):
    monkeypatch.setenv("REACHSURGE_ALLOW_USER_NAMESPACES", "0")
    assert mcp_user_id("attacker-selected") == mcp_user_id(None)


def test_redaction_hides_nested_and_url_credentials(monkeypatch):
    monkeypatch.setenv("EXAMPLE_API_KEY", "secret-value-123")
    value = redact_mapping({"smtp_password": "mail-secret", "nested": {"api_key": "key-secret"}})
    assert value["smtp_password"] == "***REDACTED***"
    assert value["nested"]["api_key"] == "***REDACTED***"
    text = redact_text("proxy=http://alice:hunter2@example.com API_KEY=secret-value-123")
    assert "hunter2" not in text
    assert "secret-value-123" not in text
    assert "***REDACTED***" in text


def test_proxy_status_masks_password():
    masked = mask_url_credentials(
        "https://proxy-user:proxy-pass@proxy.example:8080/connect?token=query-secret#fragment"
    )
    assert masked == "https://proxy.example:8080"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/admin",
    "http://169.254.169.254/latest/meta-data",
    "http://localhost:8000",
    "file:///etc/passwd",
])
def test_ssrf_guard_blocks_local_and_non_http(url):
    with pytest.raises(ValueError):
        validate_public_http_url(url)


def test_mail_guard_blocks_localhost():
    with pytest.raises(ValueError):
        validate_public_host("localhost", 25)


def test_default_tls_context_verifies_certificates():
    context = ssl.create_default_context()
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED

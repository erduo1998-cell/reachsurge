import json
import os
from pathlib import Path

import pytest

import onboarding


@pytest.fixture
def isolated_onboarding(tmp_path, monkeypatch):
    monkeypatch.setattr(onboarding, "BASE_DATA_DIR", tmp_path / "data")
    monkeypatch.setenv("LEADGEN_ENV_FILE", str(tmp_path / "missing.env"))
    for name in (
        "DEEPSEEK_API_KEY", "SERPAPI_API_KEYS", "HUNTER_API_KEY",
        "TAVILY_API_KEYS", "CAPSOLVER_API_KEY",
        "REACHSURGE_SMTP_PASSWORD", "REACHSURGE_IMAP_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_new_user_gets_pending_state_without_secrets(isolated_onboarding, monkeypatch):
    monkeypatch.setattr(onboarding, "get_user_profile", lambda user_id: None)
    secret = "sentinel-super-secret"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    env_file = isolated_onboarding / "keys.env"
    env_file.write_text("DEEPSEEK_API_KEY=hidden\n", encoding="utf-8")
    if os.name != "nt":
        env_file.chmod(0o600)
    monkeypatch.setenv("LEADGEN_ENV_FILE", str(env_file))

    status = onboarding.setup_status("default")
    serialized = json.dumps(status, ensure_ascii=False)
    assert status["phase"] == "needs_profile"
    assert status["capabilities"]["deepseek"]["configured"] is True
    assert secret not in serialized
    pending, complete = onboarding._state_paths("default")
    assert pending.exists()
    assert not complete.exists()
    assert secret not in pending.read_text(encoding="utf-8")
    if os.name != "nt":
        assert pending.stat().st_mode & 0o077 == 0


def test_zero_key_setup_completes_and_removes_pending(isolated_onboarding, monkeypatch):
    profile = {
        "industry": "LED",
        "product_description": "Commercial fixtures",
        "target_markets": ["Germany"],
    }
    monkeypatch.setattr(onboarding, "get_user_profile", lambda user_id: profile)

    before = onboarding.setup_status("default")
    assert before["phase"] == "ready_to_complete"
    pending, complete = onboarding._state_paths("default")
    assert pending.exists()

    after = onboarding.complete_setup("default")
    assert after["onboarding_complete"] is True
    assert not pending.exists()
    assert complete.exists()
    marker = json.loads(complete.read_text(encoding="utf-8"))
    assert set(marker) == {
        "schema_version", "completed_at", "app_version", "profile_namespace"
    }
    if os.name != "nt":
        assert complete.stat().st_mode & 0o077 == 0


def test_completed_setup_recovers_leftover_pending_file(isolated_onboarding, monkeypatch):
    profile = {
        "industry": "LED", "product_description": "Fixtures",
        "target_markets": ["Germany"],
    }
    monkeypatch.setattr(onboarding, "get_user_profile", lambda user_id: profile)
    onboarding.complete_setup("default")
    pending, _ = onboarding._state_paths("default")
    onboarding._atomic_private_json(pending, onboarding._pending_payload("default"))
    assert pending.exists()

    status = onboarding.setup_status("default")
    assert status["phase"] == "complete"
    assert status["temporary_state_file"]["exists"] is False
    assert not pending.exists()


def test_corrupt_marker_fails_closed(isolated_onboarding, monkeypatch):
    profile = {
        "industry": "LED", "product_description": "Fixtures",
        "target_markets": ["Germany"],
    }
    monkeypatch.setattr(onboarding, "get_user_profile", lambda user_id: profile)
    _, complete = onboarding._state_paths("default")
    complete.parent.mkdir(parents=True)
    complete.write_text("not json", encoding="utf-8")

    assert onboarding.is_setup_complete("default") is False
    assert onboarding.setup_status("default")["phase"] == "needs_repair"


def test_marker_from_another_namespace_fails_closed(isolated_onboarding, monkeypatch):
    profile = {
        "industry": "LED", "product_description": "Fixtures",
        "target_markets": ["Germany"],
    }
    monkeypatch.setattr(onboarding, "get_user_profile", lambda user_id: profile)
    _, complete = onboarding._state_paths("default")
    onboarding._atomic_private_json(complete, {
        "schema_version": onboarding.SCHEMA_VERSION,
        "completed_at": "2026-01-01T00:00:00+00:00",
        "app_version": onboarding.APP_VERSION,
        "profile_namespace": "someone-else",
    })

    assert onboarding.is_setup_complete("default") is False
    assert onboarding.setup_status("default")["phase"] == "needs_repair"


def test_profile_loss_after_completion_fails_closed(isolated_onboarding, monkeypatch):
    profile = {
        "industry": "LED", "product_description": "Fixtures",
        "target_markets": ["Germany"],
    }
    monkeypatch.setattr(onboarding, "get_user_profile", lambda user_id: profile)
    onboarding.complete_setup("default")
    monkeypatch.setattr(onboarding, "get_user_profile", lambda user_id: None)
    assert onboarding.is_setup_complete("default") is False
    assert onboarding.setup_status("default")["phase"] == "needs_repair"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission check")
def test_insecure_env_with_secret_blocks_completion(isolated_onboarding, monkeypatch):
    profile = {
        "industry": "LED", "product_description": "Fixtures",
        "target_markets": ["Germany"],
    }
    monkeypatch.setattr(onboarding, "get_user_profile", lambda user_id: profile)
    env_file = isolated_onboarding / "keys.env"
    env_file.write_text("DEEPSEEK_API_KEY=hidden\n", encoding="utf-8")
    env_file.chmod(0o644)
    monkeypatch.setenv("LEADGEN_ENV_FILE", str(env_file))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sentinel")

    status = onboarding.setup_status("default")
    assert status["phase"] == "needs_security_fix"
    with pytest.raises(ValueError, match="chmod 600"):
        onboarding.complete_setup("default")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission check")
def test_adding_secret_in_insecure_env_reopens_safety_gate(isolated_onboarding, monkeypatch):
    profile = {
        "industry": "LED", "product_description": "Fixtures",
        "target_markets": ["Germany"],
    }
    monkeypatch.setattr(onboarding, "get_user_profile", lambda user_id: profile)
    onboarding.complete_setup("default")
    assert onboarding.is_setup_complete("default") is True

    env_file = isolated_onboarding / "keys.env"
    env_file.write_text("DEEPSEEK_API_KEY=hidden\n", encoding="utf-8")
    env_file.chmod(0o644)
    monkeypatch.setenv("LEADGEN_ENV_FILE", str(env_file))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sentinel")

    assert onboarding.is_setup_complete("default") is False
    assert onboarding.setup_status("default")["phase"] == "needs_security_fix"

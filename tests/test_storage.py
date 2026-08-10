import os

import pytest

import storage.db as db
import storage.rag as rag


def test_database_and_knowledge_paths_are_bounded(tmp_path, monkeypatch):
    db_root = tmp_path / "sqlite"
    kb_root = tmp_path / "knowledge"
    db_root.mkdir()
    kb_root.mkdir()
    monkeypatch.setattr(db, "DATA_DIR", db_root)
    monkeypatch.setattr(rag, "KNOWLEDGE_DIR", kb_root)

    db.init_db("alpha")
    rag.add_knowledge("alpha", ["hello"])

    assert db._get_db_path("alpha").parent == db_root
    assert list(kb_root.joinpath("alpha").glob("*.json"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits do not apply on Windows")
def test_database_file_is_private_on_posix(tmp_path, monkeypatch):
    db_root = tmp_path / "sqlite"
    db_root.mkdir()
    monkeypatch.setattr(db, "DATA_DIR", db_root)
    db.init_db("private")
    mode = db._get_db_path("private").stat().st_mode & 0o777
    assert mode & 0o077 == 0


def test_onboarding_profile_read_never_decrypts_mail_secrets(tmp_path, monkeypatch):
    db_root = tmp_path / "sqlite"
    db_root.mkdir()
    monkeypatch.setattr(db, "DATA_DIR", db_root)
    monkeypatch.setattr(db, "KEY_DIR", tmp_path)
    monkeypatch.setattr(db, "_fernet_cache", None)
    db.upsert_user_config({
        "user_id": "profile-only",
        "name": "Demo",
        "industry": "LED",
        "target_markets": ["Germany"],
        "product_description": "Commercial fixtures",
        "smtp_password": "must-not-be-read",
        "imap_password": "must-not-be-read",
    })

    def fail_if_decrypted(*args, **kwargs):
        raise AssertionError("onboarding attempted to decrypt a mail secret")

    monkeypatch.setattr(db, "_decrypt_field", fail_if_decrypted)
    profile = db.get_user_profile("profile-only")
    assert profile["target_markets"] == ["Germany"]
    assert "smtp_password" not in profile
    assert "imap_password" not in profile

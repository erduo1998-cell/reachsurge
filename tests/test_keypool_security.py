import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import keypool
import storage.db as db


def test_legacy_plaintext_proxy_is_migrated_and_status_is_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(keypool, "KEYPOOL_DB", tmp_path / "keypool.db")
    monkeypatch.setattr(db, "KEY_DIR", tmp_path)
    monkeypatch.setattr(db, "_fernet_cache", None)
    keypool.init_keypool()

    raw = "https://proxy-user:proxy-pass@proxy.example:8443/connect?token=query-secret"
    with sqlite3.connect(keypool.KEYPOOL_DB) as conn:
        conn.execute(
            "INSERT INTO proxy_pool (proxy_id, label, proxy_url) VALUES (?, ?, ?)",
            ("legacy", "legacy", raw),
        )
        conn.commit()

    keypool.init_keypool()
    with sqlite3.connect(keypool.KEYPOOL_DB) as conn:
        stored = conn.execute(
            "SELECT proxy_url FROM proxy_pool WHERE proxy_id='legacy'"
        ).fetchone()[0]
    assert stored.startswith(db._FERNET_CIPHER_PREFIX)
    assert db._decrypt_field(stored, "proxy_url") == raw

    status = keypool.ProxyPool().status()[0]["proxy_url"]
    assert status == "https://proxy.example:8443"


def test_concurrent_first_start_uses_one_fernet_key(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["LEADGEN_DATA_DIR"] = str(tmp_path)
    env.pop("LEADGEN_FERNET_KEY", None)
    env["PYTHONPATH"] = str(project_root)
    encrypt = "from storage.db import _encrypt_field; print(_encrypt_field('shared-value'))"
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", encrypt],
            cwd=tmp_path,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(8)
    ]
    ciphertexts = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stderr
        ciphertexts.append(stdout.strip())

    decrypt = (
        "import sys; from storage.db import _decrypt_field; "
        "print('\\n'.join(_decrypt_field(v, 'test') for v in sys.argv[1:]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", decrypt, *ciphertexts],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    assert result.stdout.splitlines() == ["shared-value"] * len(ciphertexts)


def test_daily_send_reservation_is_atomic_across_processes(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["LEADGEN_DATA_DIR"] = str(tmp_path)
    env["PYTHONPATH"] = str(project_root)
    reserve = (
        "from storage.db import reserve_outreach_send; "
        "print(bool(reserve_outreach_send('shared', '', 'subject', 'body', 1)[0]))"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", reserve],
            cwd=tmp_path,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(8)
    ]
    outcomes = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stderr
        outcomes.append(stdout.strip())
    assert outcomes.count("True") == 1
    assert outcomes.count("False") == 7

import sys

import pytest

from sources import gosom


def _enable_gosom(monkeypatch, tmp_path):
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

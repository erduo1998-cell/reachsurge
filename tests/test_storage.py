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


def test_database_file_is_private_on_posix(tmp_path, monkeypatch):
    db_root = tmp_path / "sqlite"
    db_root.mkdir()
    monkeypatch.setattr(db, "DATA_DIR", db_root)
    db.init_db("private")
    mode = db._get_db_path("private").stat().st_mode & 0o777
    assert mode & 0o077 == 0

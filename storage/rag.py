"""知识库存储 —— 文件 + 关键词检索（零依赖）。

后期可升级为 ChromaDB + bge-m3 向量检索。
"""
import os
import json
import uuid
from pathlib import Path

from platformdirs import user_data_path
from security import storage_token

_env_data_dir = os.environ.get("LEADGEN_DATA_DIR", "").strip()
_legacy_project_data = Path(__file__).parent.parent / "data"
_legacy_project_has_data = (
    (_legacy_project_data / "leadgen_fernet.key").exists()
    or (_legacy_project_data / "sqlite" / "keypool.db").exists()
    or any((_legacy_project_data / "sqlite").glob("user_*.db"))
    or ((_legacy_project_data / "knowledge").exists() and any((_legacy_project_data / "knowledge").iterdir()))
)
_base_data_dir = (
    Path(_env_data_dir)
    if _env_data_dir
    else (_legacy_project_data if _legacy_project_has_data else user_data_path("ReachSurge", "erduo"))
)
KNOWLEDGE_DIR = _base_data_dir / "knowledge"
KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
try:
    KNOWLEDGE_DIR.chmod(0o700)
except OSError:
    pass


def _user_dir(user_id: str) -> Path:
    d = KNOWLEDGE_DIR / storage_token(user_id)
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d


def _doc_path(user_id: str, doc_id: str) -> Path:
    return _user_dir(user_id) / f"{doc_id}.json"


def add_knowledge(
    user_id: str,
    documents: list[str],
    metadatas: list[dict] = None,
    ids: list[str] = None,
) -> list[str]:
    """向用户知识库添加文档。"""
    if ids is None:
        ids = [uuid.uuid4().hex[:12] for _ in documents]
    if metadatas is None:
        metadatas = [{} for _ in documents]

    for doc_id, text, meta in zip(ids, documents, metadatas):
        record = {
            "id": doc_id,
            "content": text,
            "metadata": meta,
        }
        path = _doc_path(user_id, doc_id)
        path.write_text(
            json.dumps(record, ensure_ascii=False), encoding="utf-8"
        )
        try:
            path.chmod(0o600)
        except OSError:
            pass

    return ids


def search_knowledge(user_id: str, query: str, n_results: int = 3) -> list[str]:
    """检索用户知识库（关键词匹配）。"""
    user_dir = _user_dir(user_id)
    if not user_dir.exists():
        return []

    keywords = _tokenize(query)
    if not keywords:
        return []

    scored = []
    for f in user_dir.glob("*.json"):
        try:
            record = json.loads(f.read_text(encoding="utf-8"))
            content = record.get("content", "")
            score = sum(content.lower().count(kw.lower()) for kw in keywords)
            if score > 0:
                scored.append((score, content))
        except (json.JSONDecodeError, KeyError):
            continue

    scored.sort(key=lambda x: x[0], reverse=True)
    return [content for _, content in scored[:n_results]]


def get_knowledge_count(user_id: str) -> int:
    """获取用户知识库文档数量。"""
    user_dir = _user_dir(user_id)
    if not user_dir.exists():
        return 0
    return len(list(user_dir.glob("*.json")))


def _tokenize(text: str) -> list[str]:
    """简单分词：英文按空格切分，中文按字符 bigram。"""
    import re
    tokens = []

    parts = re.split(r'[^\w一-鿿]+', text)
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if re.match(r'^[一-鿿]+$', p):
            tokens.append(p)
            for i in range(len(p) - 1):
                tokens.append(p[i:i + 2])
        elif len(p) >= 2:
            tokens.append(p)

    return tokens

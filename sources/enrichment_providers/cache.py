"""SQLite 富集结果缓存 (复用 storage.db 连接, 每租户 db 内新表)。

作用: 同一 domain → 同一邮箱, 命中即跳过整个瀑布 —— 省 Hunter 配额
(Free 50 search/月) + 省慢 SMTP 探测。首次跑会把既有 guessed/no_mx
域名重新过一遍 Hunter (SELECT 含它们), 之后缓存命中, 重跑近乎免费。

表 email_enrichment_cache 幂等建 (CREATE IF NOT EXISTS, 照 db.py 的 ALTER 模式)。
缓存键 = domain (每租户 db 内唯一)。命中即返回, 不区分强弱状态 —— 一旦富集过就不再重跑
(强结果 verified/scraped 当然跳; 弱结果 guessed 也跳, 因 Hunter 已试过配额不该再烧)。

缓存对失败优雅: 建表/读写任何异常都不影响瀑布本身 (降级为无缓存)。
"""
from typing import Optional

from .base import EnrichResult

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS email_enrichment_cache (
    domain TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    email TEXT,
    email_status TEXT,
    confidence REAL DEFAULT 0,
    evidence TEXT DEFAULT '',
    source TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);
"""


class EnrichmentCache:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self._table_ready = False

    def _ensure_table(self, conn) -> None:
        if self._table_ready:
            return
        try:
            conn.execute(_TABLE_SQL)
            conn.commit()
            self._table_ready = True
        except Exception:
            # 建表失败 -> 降级无缓存, 不阻断富集
            self._table_ready = True  # 不再重试, 避免每条都试

    def get(self, domain: str) -> Optional[EnrichResult]:
        """命中返回 EnrichResult, 未命中/出错返回 None。"""
        try:
            from storage.db import _get_conn
            conn = _get_conn(self.user_id)
            try:
                self._ensure_table(conn)
                row = conn.execute(
                    "SELECT email, email_status, confidence, evidence, source "
                    "FROM email_enrichment_cache WHERE domain = ?",
                    (domain,),
                ).fetchone()
            finally:
                conn.close()
        except Exception:
            return None
        if not row:
            return None
        return EnrichResult(
            email=row["email"] or "",
            status=row["email_status"] or "unknown",
            confidence=float(row["confidence"] or 0.0),
            evidence=row["evidence"] or "",
            source=row["source"] or "",
        )

    def put(self, domain: str, result: EnrichResult) -> None:
        """写入缓存 (INSERT OR REPLACE)。任何异常静默忽略。"""
        try:
            from storage.db import _get_conn
            conn = _get_conn(self.user_id)
            try:
                self._ensure_table(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO email_enrichment_cache "
                    "(domain, user_id, email, email_status, confidence, evidence, source) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (domain, self.user_id, result.email, result.status,
                     result.confidence, result.evidence, result.source),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass

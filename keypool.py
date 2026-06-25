"""号池层 (阶段0 竖切) — 共享 API key + 代理池, 跨租户。

设计要点:
- 全局 keypool.db (data/sqlite/keypool.db), 不走 per-user 库 —— 号池是跨租户共享资源
  (当前单租户, 表结构预留多租户)。
- 复用 storage.db 的 Fernet 加密 api_key (_encrypt_field/_decrypt_field)。
- 同步实现 (对齐现有 httpx/sqlite/handler 全同步风格): with pool.acquire('overpass') as k
- 3 张表: api_key_pool / proxy_pool / usage_log (quotas 内化进 api_key_pool, 避免过度设计)。
- 合规红线: resell_ok 标志 (共享池转售只准入 true; Overpass 等公共端点 resell_ok=1)。

阶段0 诚实范围:
- KeyPool 完整 (acquire/consume/mark_exhausted/reset_if_due/add/list/status)。
- ProxyPool 骨架: 单代理 (本地 Clash 7897); 机场 Clash 单本地端口, external-controller 为空,
  无法做到「每 key 独立 IP」——真实多 IP 待住宅代理。Overpass (A 类) 经它出墙。
"""
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from storage.db import _encrypt_field, _decrypt_field, DATA_DIR

KEYPOOL_DB = DATA_DIR / "keypool.db"
DEFAULT_TENANT = "ear"  # 默认租户; 产品化时多租户仍贯穿此字段


# ── 连接 & 建表 ──

def _conn() -> sqlite3.Connection:
    KEYPOOL_DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(KEYPOOL_DB))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_keypool():
    """幂等建表。"""
    c = _conn()
    try:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS api_key_pool (
                key_id          TEXT PRIMARY KEY,
                provider        TEXT NOT NULL,           -- overpass/hunter/apollo/tavily/...
                tenant_id       TEXT NOT NULL DEFAULT 'ear',
                label           TEXT DEFAULT '',         -- 可读标签
                api_key         TEXT DEFAULT '',         -- Fernet 加密; 公共端点可为空
                endpoint_url    TEXT DEFAULT '',         -- overpass 多端点 / 自定义 API base
                status          TEXT DEFAULT 'active',   -- active/exhausted/disabled
                quota_total     INTEGER DEFAULT 0,       -- 周期配额上限; 0=不限(公共端点)
                quota_used      INTEGER DEFAULT 0,
                quota_window    TEXT DEFAULT '',         -- 'monthly'/'daily'/''
                window_reset_at TEXT DEFAULT '',         -- ISO8601 下次重置时间
                last_error_code TEXT DEFAULT '',         -- 429/402/...
                last_error_at   TEXT DEFAULT '',
                last_used_at    TEXT DEFAULT '',
                resell_ok       INTEGER DEFAULT 0,       -- 合规: 能否进共享池转售
                register_region TEXT DEFAULT '',         -- 注册地区 (IP 绑定参考)
                proxy_id        TEXT DEFAULT '',         -- 绑定代理 (换号必换 IP)
                created_at      TEXT DEFAULT (datetime('now')),
                updated_at      TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_key_provider ON api_key_pool(provider, status);

            CREATE TABLE IF NOT EXISTS proxy_pool (
                proxy_id     TEXT PRIMARY KEY,
                label        TEXT DEFAULT '',
                proxy_url    TEXT NOT NULL,              -- socks5://127.0.0.1:7897 等
                region       TEXT DEFAULT '',            -- HK/JP/DE... (Clash 机场多为亚太)
                status       TEXT DEFAULT 'active',      -- active/banned/dead
                source       TEXT DEFAULT 'clash',       -- clash/residential/datacenter
                last_check_at TEXT DEFAULT '',
                last_ban_at   TEXT DEFAULT '',
                created_at    TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS usage_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        TEXT DEFAULT (datetime('now')),
                tenant_id TEXT DEFAULT 'ear',
                provider  TEXT NOT NULL,
                key_id    TEXT DEFAULT '',
                proxy_id  TEXT DEFAULT '',
                cost      INTEGER DEFAULT 1,
                result    TEXT DEFAULT 'ok',             -- ok/429/402/error
                detail    TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_log(ts);
        """)
        c.commit()
    finally:
        c.close()


class NoAvailableKey(Exception):
    """某 provider 无可用 key (全部 exhausted/disabled 或未投喂)。"""


# ── KeyPool ──

class KeyContext:
    """acquire 返回的上下文。with 块退出自动 consume; 调用方遇 429/402 调 mark_failed。"""

    def __init__(self, pool: "KeyPool", row: sqlite3.Row):
        self._pool = pool
        self._row = row
        self.key_id: str = row["key_id"]
        self.provider: str = row["provider"]
        self.api_key: str = _decrypt_field(row["api_key"], "api_key") if row["api_key"] else ""
        self.endpoint_url: str = row["endpoint_url"] or ""
        self.proxy_id: str = row["proxy_id"] or ""
        self.resell_ok: bool = bool(row["resell_ok"])
        self._settled = False  # 是否已结算 (consume 或 mark_failed)

    def mark_failed(self, code: str, detail: str = ""):
        """调用方遇限流/配额错误调此。429/402/quota → 标 exhausted; 其他只记日志不禁用。"""
        if self._settled:
            return
        code_norm = str(code).strip()
        if code_norm in ("429", "402", "quota", "rate_limited"):
            self._pool._mark_exhausted(self.key_id, code_norm, detail)
            self._pool._log_usage(self._pool.tenant_id, self.provider,
                                  self.key_id, self.proxy_id, 0, code_norm, detail)
        else:
            self._pool._log_usage(self._pool.tenant_id, self.provider,
                                  self.key_id, self.proxy_id, 0, "error", f"{code_norm}: {detail}")
        self._settled = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self._settled:
            self._pool._consume(self.key_id)
            self._pool._log_usage(self._pool.tenant_id, self.provider,
                                  self.key_id, self.proxy_id, 1, "ok", "")
        return False


class KeyPool:
    """provider → key 列表 + 配额守卫 + 自动轮换。投喂新号 add() 热生效。"""

    def __init__(self, tenant_id: str = DEFAULT_TENANT):
        self.tenant_id = tenant_id
        init_keypool()

    # ── 投喂 / 管理 ──

    def add(self, provider: str, api_key: str = "", endpoint_url: str = "",
            label: str = "", quota_total: int = 0, quota_window: str = "",
            resell_ok: bool = False, register_region: str = "", proxy_id: str = "",
            tenant_id: Optional[str] = None) -> str:
        """投喂一个 key/端点, 热生效。返回 key_id。

        公共端点 (overpass): api_key 留空, endpoint_url 填端点, resell_ok=True, quota_total=0(不限)。
        """
        tid = tenant_id or self.tenant_id
        key_id = uuid.uuid4().hex[:16]
        enc = _encrypt_field(api_key) if api_key else ""
        c = _conn()
        try:
            c.execute(
                "INSERT INTO api_key_pool "
                "(key_id, provider, tenant_id, label, api_key, endpoint_url, status, "
                " quota_total, quota_used, quota_window, resell_ok, register_region, proxy_id) "
                "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, 0, ?, ?, ?, ?)",
                (key_id, provider, tid, label, enc, endpoint_url,
                 int(quota_total), quota_window, int(bool(resell_ok)),
                 register_region, proxy_id),
            )
            c.commit()
        finally:
            c.close()
        return key_id

    def list_keys(self, provider: Optional[str] = None) -> list[dict]:
        c = _conn()
        try:
            if provider:
                rows = c.execute(
                    "SELECT * FROM api_key_pool WHERE provider=? ORDER BY created_at",
                    (provider,)).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM api_key_pool ORDER BY provider, created_at").fetchall()
        finally:
            c.close()
        out = []
        for r in rows:
            d = dict(r)
            d["api_key"] = "***" if d.get("api_key") else ""  # 不回显明文
            out.append(d)
        return out

    def status(self) -> dict:
        """各 provider 的 key 数 / active / 配额汇总。"""
        c = _conn()
        try:
            rows = c.execute(
                "SELECT provider, status, COUNT(*) AS n, "
                " SUM(quota_used) AS used, SUM(quota_total) AS total "
                " FROM api_key_pool GROUP BY provider, status"
            ).fetchall()
        finally:
            c.close()
        agg: dict = {}
        for r in rows:
            p = r["provider"]
            agg.setdefault(p, {"active": 0, "exhausted": 0, "disabled": 0, "used": 0, "total": 0})
            agg[p][r["status"]] = agg[p].get(r["status"], 0) + r["n"]
            agg[p]["used"] += r["used"] or 0
            agg[p]["total"] += r["total"] or 0
        return agg

    # ── 取用 ──

    def _select(self, provider: str) -> Optional[sqlite3.Row]:
        """选一个可用 key: active + 配额未尽; 优先最少使用 (轮询)。"""
        self._reset_if_due_provider(provider)
        c = _conn()
        try:
            rows = c.execute(
                "SELECT * FROM api_key_pool WHERE provider=? AND status='active' "
                " AND (quota_total=0 OR quota_used < quota_total) "
                " ORDER BY quota_used ASC, last_used_at ASC LIMIT 1",
                (provider,)).fetchall()
        finally:
            c.close()
        return rows[0] if rows else None

    def acquire(self, provider: str) -> KeyContext:
        row = self._select(provider)
        if row is None:
            raise NoAvailableKey(f"provider={provider} 无可用 key (未投喂或全部耗尽)")
        # 标记 last_used_at (软占用, 真正 consume 在 ctx 退出)
        c = _conn()
        try:
            c.execute(
                "UPDATE api_key_pool SET last_used_at=datetime('now'), updated_at=datetime('now') "
                "WHERE key_id=?", (row["key_id"],))
            c.commit()
        finally:
            c.close()
        return KeyContext(self, row)

    @contextmanager
    def borrow(self, provider: str):
        """便捷: with pool.borrow('overpass') as k: (k.api_key/k.endpoint_url/...)。"""
        ctx = self.acquire(provider)
        try:
            yield ctx
        finally:
            ctx.__exit__(None, None, None)

    # ── 内部结算 ──

    def _consume(self, key_id: str, cost: int = 1):
        c = _conn()
        try:
            c.execute(
                "UPDATE api_key_pool SET quota_used=quota_used+?, "
                " last_used_at=datetime('now'), updated_at=datetime('now') WHERE key_id=?",
                (cost, key_id))
            c.commit()
        finally:
            c.close()

    def _mark_exhausted(self, key_id: str, code: str, detail: str):
        c = _conn()
        try:
            c.execute(
                "UPDATE api_key_pool SET status='exhausted', "
                " last_error_code=?, last_error_at=datetime('now'), updated_at=datetime('now') "
                " WHERE key_id=?",
                (code[:16], key_id))
            c.commit()
        finally:
            c.close()

    def _log_usage(self, tenant_id: str, provider: str, key_id: str,
                   proxy_id: str, cost: int, result: str, detail: str):
        try:
            c = _conn()
            c.execute(
                "INSERT INTO usage_log (tenant_id, provider, key_id, proxy_id, cost, result, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, provider, key_id, proxy_id, cost, result[:16], detail[:200]))
            c.commit()
            c.close()
        except Exception:
            pass  # 日志失败不影响主流程

    def _reset_if_due_provider(self, provider: str):
        """周期配额重置: window_reset_at 已过 → status 回 active, quota_used 清零。"""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
        c = _conn()
        try:
            due = c.execute(
                "SELECT key_id FROM api_key_pool WHERE provider=? AND status='exhausted' "
                " AND window_reset_at!='' AND window_reset_at <= ?",
                (provider, now)).fetchall()
            for r in due:
                c.execute(
                    "UPDATE api_key_pool SET status='active', quota_used=0, "
                    " last_error_code='', updated_at=datetime('now') WHERE key_id=?",
                    (r["key_id"],))
            c.commit()
        finally:
            c.close()


# ── ProxyPool (阶段0: 单代理骨架) ──

class ProxyPool:
    """代理池。阶段0: 单代理 (本地 Clash 7897), 多 IP 接口预留。

    现实约束: Clash Verge 机场订阅为单本地混合端口 (7897), external-controller 为空,
    无法做到「每 key 独立 IP」——切节点需 controller API 且全局影响。
    真实多 IP 池待住宅代理 (每代理独立 IP:port) 接入后在此扩展。
    Overpass (A 类) 经 get_active() 出墙即可。
    """

    def __init__(self):
        init_keypool()

    def add(self, label: str, proxy_url: str, region: str = "",
            source: str = "clash") -> str:
        pid = uuid.uuid4().hex[:16]
        c = _conn()
        try:
            c.execute(
                "INSERT INTO proxy_pool (proxy_id, label, proxy_url, region, status, source) "
                "VALUES (?, ?, ?, ?, 'active', ?)",
                (pid, label, proxy_url, region, source))
            c.commit()
        finally:
            c.close()
        return pid

    def get_active(self) -> Optional[str]:
        """返回当前可用代理 URL, 无则 None (直连)。优先 proxy_pool 表; 回退 Clash 环境变量。"""
        c = _conn()
        try:
            row = c.execute(
                "SELECT proxy_url FROM proxy_pool WHERE status='active' "
                "ORDER BY last_ban_at ASC, created_at ASC LIMIT 1").fetchone()
        finally:
            c.close()
        if row:
            return row["proxy_url"]
        # 回退: 标准代理环境变量 (https_proxy/HTTPS_PROXY/http_proxy/HTTP_PROXY/all_proxy)
        for k in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "all_proxy"):
            v = os.environ.get(k, "").strip()
            if v:
                return v
        return None

    def status(self) -> list[dict]:
        c = _conn()
        try:
            rows = c.execute("SELECT * FROM proxy_pool ORDER BY created_at").fetchall()
        finally:
            c.close()
        return [dict(r) for r in rows]

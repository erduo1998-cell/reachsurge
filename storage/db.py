"""SQLite 数据层 —— 每用户独立 db 文件，零锁争用。

复用自 foreign-trade-leadgen/src/storage/db.py，路径调整为 reachsurge。
"""
import os
import sys
import sqlite3
import json
import time
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from platformdirs import user_data_path
from security import normalize_user_id, storage_token

_env_data_dir = os.environ.get("LEADGEN_DATA_DIR", "").strip()
_legacy_project_data = Path(__file__).parent.parent / "data"
_legacy_project_has_data = (
    (_legacy_project_data / "leadgen_fernet.key").exists()
    or (_legacy_project_data / "sqlite" / "keypool.db").exists()
    or any((_legacy_project_data / "sqlite").glob("user_*.db"))
)
BASE_DATA_DIR = (
    Path(_env_data_dir)
    if _env_data_dir
    else (_legacy_project_data if _legacy_project_has_data else user_data_path("ReachSurge", "erduo"))
)
# Compatibility: older versions treated LEADGEN_DATA_DIR itself as the SQLite
# directory.  Keep using it when existing DBs prove that layout is in use.
_legacy_direct_layout = bool(_env_data_dir) and (
    (BASE_DATA_DIR / "keypool.db").exists() or any(BASE_DATA_DIR.glob("user_*.db"))
)
DATA_DIR = BASE_DATA_DIR if _legacy_direct_layout else BASE_DATA_DIR / "sqlite"
DATA_DIR.mkdir(parents=True, exist_ok=True)
try:
    DATA_DIR.chmod(0o700)
except OSError:
    pass

# Fernet 密钥目录：与 DATA_DIR 同源(复用 LEADGEN_DATA_DIR)，env 模式直接用 env 值，
# 否则落到项目根 data/ 目录。密钥文件 leadgen_fernet.key 与 sqlite 子目录并列。
KEY_DIR = BASE_DATA_DIR

# Fernet 密文固定前缀，用于判定值是否已被加密
_FERNET_CIPHER_PREFIX = "gAAAAA"

# 进程级缓存：Fernet 实例（首次使用时惰性初始化）
_fernet_cache: Optional[Fernet] = None


def _get_fernet() -> Fernet:
    """获取（并缓存）进程级 Fernet 实例。

    密钥来源优先级：
      1. 环境变量 LEADGEN_FERNET_KEY（urlsafe base64 Fernet key）
      2. 文件 <KEY_DIR>/leadgen_fernet.key（KEY_DIR 复用 LEADGEN_DATA_DIR，默认项目根 data/）：
         不存在则生成并写入（0600），存在则读取。
    """
    global _fernet_cache
    if _fernet_cache is not None:
        return _fernet_cache

    key_str = os.environ.get("LEADGEN_FERNET_KEY", "").strip()
    if key_str:
        _fernet_cache = Fernet(key_str.encode("utf-8"))
        return _fernet_cache

    key_file = KEY_DIR / "leadgen_fernet.key"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        key_file.parent.chmod(0o700)
    except OSError:
        pass
    try:
        # O_EXCL elects exactly one writer across MCP client processes sharing
        # this data directory. Losers read the winner's key instead of caching
        # a different key that would make newly written ciphertext unreadable.
        fd = os.open(str(key_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        fd = None

    if fd is not None:
        key_bytes = Fernet.generate_key()
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(key_bytes)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            try:
                key_file.unlink()
            except OSError:
                pass
            raise
    else:
        # The winner may have created the file but not finished its tiny write.
        key_bytes = b""
        for _ in range(50):
            try:
                candidate = key_file.read_bytes().strip()
                Fernet(candidate)
                key_bytes = candidate
                break
            except (OSError, ValueError):
                time.sleep(0.02)
        if not key_bytes:
            raise RuntimeError(f"加密密钥文件无效或尚未写完：{key_file}")
    try:
        key_file.chmod(0o600)
    except OSError:
        pass
    _fernet_cache = Fernet(key_bytes)
    return _fernet_cache


def _encrypt_field(value):
    """加密单个字段。空字符串保持空；非空字符串加密为 Fernet 密文。"""
    if value is None or value == "":
        return value if value is not None else ""
    if not isinstance(value, str):
        value = str(value)
    return _get_fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def _decrypt_field(value, field_name: str):
    """解密单个字段。

    - None / 空字符串 → 原样返回
    - 以 gAAAAA 开头 → 尝试 Fernet 解密；解密失败打 warning，原样返回
    - 其它（历史明文）→ 原样返回并打 warning
    """
    if value is None or value == "":
        return value
    if not isinstance(value, str):
        return value
    if not value.startswith(_FERNET_CIPHER_PREFIX):
        # 历史明文：不破坏现有流程，仅告警
        print(f"[db.py WARNING] {field_name} 存在未加密明文，原样返回", file=sys.stderr)
        return value
    try:
        return _get_fernet().decrypt(value.encode("utf-8")).decode("utf-8")
    except (InvalidToken, Exception) as e:
        print(f"[db.py WARNING] {field_name} 解密失败 ({e!r})，原样返回", file=sys.stderr)
        return value


def _get_db_path(user_id: str) -> Path:
    """每个本地命名空间一个 SQLite 文件（user_id 不是认证身份）。"""
    return DATA_DIR / f"user_{storage_token(user_id)}.db"


def _get_conn(user_id: str) -> sqlite3.Connection:
    db_path = _get_db_path(user_id)
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        db_path.chmod(0o600)
    except OSError:
        pass
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError as exc:
        # Another first-start process may be changing this persistent setting.
        # The connection remains usable; BEGIN IMMEDIATE below still enforces
        # the send reservation's atomicity.
        if "locked" not in str(exc).lower():
            raise
    conn.row_factory = sqlite3.Row
    return conn


def init_db(user_id: str):
    """为用户创建数据库表（幂等）。"""
    user_id = normalize_user_id(user_id)
    conn = _get_conn(user_id)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS user_config (
            user_id TEXT PRIMARY KEY,
            feishu_open_id TEXT NOT NULL,
            name TEXT DEFAULT '',
            industry TEXT DEFAULT '',
            target_markets TEXT DEFAULT '[]',
            product_description TEXT DEFAULT '',
            smtp_host TEXT DEFAULT '',
            smtp_port INTEGER DEFAULT 587,
            smtp_user TEXT DEFAULT '',
            smtp_password TEXT DEFAULT '',
            imap_host TEXT DEFAULT '',
            imap_port INTEGER DEFAULT 993,
            imap_user TEXT DEFAULT '',
            imap_password TEXT DEFAULT '',
            daily_send_limit INTEGER DEFAULT 30,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS leads (
            lead_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            company_name TEXT NOT NULL,
            website TEXT DEFAULT '',
            country TEXT DEFAULT '',
            city TEXT DEFAULT '',
            contact_name TEXT DEFAULT '',
            contact_title TEXT DEFAULT '',
            email TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            linkedin_url TEXT DEFAULT '',
            buyer_type TEXT DEFAULT '',  -- europages: distributor/wholesaler/manufacturer/importer
            email_status TEXT DEFAULT 'unknown',
            email_confidence REAL DEFAULT 0.0,
            source TEXT DEFAULT '',
            search_query TEXT DEFAULT '',
            score INTEGER DEFAULT 0,
            status TEXT DEFAULT 'new',
            discovered_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS outreach_records (
            record_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            lead_id TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            template_used TEXT DEFAULT '',
            status TEXT DEFAULT 'queued',
            sent_at TEXT,
            opened_at TEXT,
            error_message TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS inquiries (
            inquiry_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            lead_id TEXT,
            outreach_record_id TEXT,
            from_address TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            received_at TEXT DEFAULT (datetime('now')),
            intent TEXT DEFAULT '',
            confidence REAL DEFAULT 0.0,
            auto_replied INTEGER DEFAULT 0,
            reply_body TEXT DEFAULT '',
            needs_human INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            task_type TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            limit_int INTEGER,
            total INTEGER DEFAULT 0,
            processed INTEGER DEFAULT 0,
            result_summary TEXT DEFAULT '',
            error TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
    """)
    # 幂等迁移: 老库 leads 表缺列则补
    _cols = [r[1] for r in conn.execute("PRAGMA table_info(leads)").fetchall()]
    migrations = {
        "buyer_type": "ALTER TABLE leads ADD COLUMN buyer_type TEXT DEFAULT ''",
        "signal_level": "ALTER TABLE leads ADD COLUMN signal_level TEXT DEFAULT ''",
        "company_intel": "ALTER TABLE leads ADD COLUMN company_intel TEXT DEFAULT ''",
    }
    for column, statement in migrations.items():
        if column in _cols:
            continue
        try:
            conn.execute(statement)
        except sqlite3.OperationalError as exc:
            # A parallel first-start process may have added it after our
            # table_info snapshot. Treat only that exact migration race as OK.
            if "duplicate column name" not in str(exc).lower():
                raise
    conn.commit()
    conn.close()


def upsert_user_config(config: dict) -> dict:
    """插入或更新用户配置。

    smtp_password / imap_password 在写入前透明加密（空值保持空）。
    """
    init_db(config["user_id"])
    conn = _get_conn(config["user_id"])

    if isinstance(config.get("target_markets"), list):
        config = {**config, "target_markets": json.dumps(config["target_markets"], ensure_ascii=False)}

    fields = [
        "user_id", "feishu_open_id", "name", "industry", "target_markets",
        "product_description", "smtp_host", "smtp_port", "smtp_user",
        "smtp_password", "imap_host", "imap_port", "imap_user",
        "imap_password", "daily_send_limit", "is_active"
    ]
    values = {k: config.get(k, "") for k in fields}

    # 写入前加密敏感字段（空串保持空，不加密）
    values["smtp_password"] = _encrypt_field(values["smtp_password"])
    values["imap_password"] = _encrypt_field(values["imap_password"])

    values["updated_at"] = "datetime('now')"

    placeholders = ", ".join(fields)
    updates = ", ".join(f"{k} = excluded.{k}" for k in fields)
    sql = f"INSERT INTO user_config ({placeholders}) VALUES ({', '.join('?' for _ in fields)}) ON CONFLICT(user_id) DO UPDATE SET {updates}"

    conn.execute(sql, [values[k] for k in fields])
    conn.commit()
    conn.close()
    return get_user_config(config["user_id"])


def get_user_config(user_id: str) -> Optional[dict]:
    """读取用户配置。读出时透明解密 smtp_password / imap_password。"""
    db_path = _get_db_path(user_id)
    if not db_path.exists():
        return None
    conn = _get_conn(user_id)
    row = conn.execute("SELECT * FROM user_config WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    try:
        d["target_markets"] = json.loads(d.get("target_markets", "[]"))
    except (json.JSONDecodeError, TypeError):
        d["target_markets"] = []
    # 读出后透明解密
    d["smtp_password"] = _decrypt_field(d.get("smtp_password"), "smtp_password")
    d["imap_password"] = _decrypt_field(d.get("imap_password"), "imap_password")
    return d


def user_exists(user_id: str) -> bool:
    """检查用户是否已录入过信息。"""
    db_path = _get_db_path(user_id)
    if not db_path.exists():
        return False
    conn = _get_conn(user_id)
    row = conn.execute("SELECT 1 FROM user_config WHERE user_id = ? AND product_description != ''", (user_id,)).fetchone()
    conn.close()
    return row is not None


# ── Leads ──

def insert_lead(lead: dict):
    """插入一条线索。"""
    init_db(lead["user_id"])
    conn = _get_conn(lead["user_id"])

    fields = [
        "lead_id", "user_id", "company_name", "website", "country", "city",
        "contact_name", "contact_title", "email", "phone", "linkedin_url",
        "buyer_type", "source", "search_query", "score", "status"
    ]
    values = {k: lead.get(k, "") for k in fields}
    values["discovered_at"] = "datetime('now')"
    values["updated_at"] = "datetime('now')"

    placeholders = ", ".join(fields + ["discovered_at", "updated_at"])
    sql = f"INSERT OR REPLACE INTO leads ({placeholders}) VALUES ({', '.join('?' for _ in fields)}, datetime('now'), datetime('now'))"

    conn.execute(sql, [values[k] for k in fields])
    conn.commit()
    conn.close()



def lead_exists(user_id: str, company_name: str, source: str) -> bool:
    """检查同用户同源同公司是否已入库（防重复触发累积）。

    归一化: company_name 去首尾空白 + lowercase（与 registry.orchestrate 内部去重一致）。
    跨源同公司允许共存（europages 有 buyer_type，gosom 有 email，互补）。
    """
    if not company_name:
        return False
    db_path = _get_db_path(user_id)
    if not db_path.exists():
        return False
    conn = _get_conn(user_id)
    row = conn.execute(
        "SELECT 1 FROM leads WHERE user_id = ? AND source = ? "
        "AND LOWER(TRIM(company_name)) = LOWER(TRIM(?)) LIMIT 1",
        (user_id, source, company_name),
    ).fetchone()
    conn.close()
    return row is not None


def list_leads(user_id: str, status: str = None, limit: int = 20) -> list[dict]:
    """列出用户线索。"""
    db_path = _get_db_path(user_id)
    if not db_path.exists():
        return []
    conn = _get_conn(user_id)

    if status:
        rows = conn.execute(
            "SELECT * FROM leads WHERE user_id = ? AND status = ? ORDER BY discovered_at DESC LIMIT ?",
            (user_id, status, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM leads WHERE user_id = ? ORDER BY discovered_at DESC LIMIT ?",
            (user_id, limit)
        ).fetchall()

    conn.close()
    return [dict(r) for r in rows]


def update_lead_status(user_id: str, lead_id: str, status: str, email_status: str = None):
    """更新线索状态。"""
    conn = _get_conn(user_id)
    if email_status:
        conn.execute(
            "UPDATE leads SET status = ?, email_status = ?, updated_at = datetime('now') WHERE lead_id = ? AND user_id = ?",
            (status, email_status, lead_id, user_id)
        )
    else:
        conn.execute(
            "UPDATE leads SET status = ?, updated_at = datetime('now') WHERE lead_id = ? AND user_id = ?",
            (status, lead_id, user_id)
        )
    conn.commit()
    conn.close()


def get_lead(user_id: str, lead_id: str) -> Optional[dict]:
    """读取单条线索全字段 (含 company_intel)。跨用户返回 None。"""
    db_path = _get_db_path(user_id)
    if not db_path.exists():
        return None
    conn = _get_conn(user_id)
    try:
        row = conn.execute(
            "SELECT * FROM leads WHERE lead_id = ? AND user_id = ?",
            (lead_id, user_id),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def update_lead_intel(user_id: str, lead_id: str, intel_json: str, signal_level: str):
    """写入公司情报 (enrich_company_profile 产出): signal_level 单列 + company_intel JSON。

    幂等覆盖 (情报可重跑刷新), 不动 email/status/buyer_type 等其他字段。
    """
    conn = _get_conn(user_id)
    try:
        conn.execute(
            "UPDATE leads SET signal_level = ?, company_intel = ?, "
            "updated_at = datetime('now') WHERE lead_id = ? AND user_id = ?",
            (signal_level or "", intel_json or "", lead_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()


# ── Outreach & Inquiries (发信/收信记录, 邮箱闭环) ──

def insert_outreach_record(user_id: str, lead_id: str, subject: str, body: str,
                           template_used: str = "", status: str = "sent",
                           error_message: str = "") -> str:
    """记录一封开发信的发送结果。返回 record_id。

    status: queued / sent / failed / opened。lead_id 为空串表示手动发信(非线索)。
    sent_at 仅在调用时记录(成功/失败均记, 失败时 error_message 填原因)。
    """
    import uuid as _uuid
    init_db(user_id)
    record_id = _uuid.uuid4().hex[:16]
    conn = _get_conn(user_id)
    try:
        conn.execute(
            "INSERT INTO outreach_records "
            "(record_id, user_id, lead_id, subject, body, template_used, status, sent_at, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)",
            (record_id, user_id, lead_id or "", subject, body,
             template_used, status, error_message),
        )
        conn.commit()
    finally:
        conn.close()
    return record_id


def reserve_outreach_send(user_id: str, lead_id: str, subject: str, body: str,
                          daily_limit: int) -> tuple[Optional[str], int]:
    """Atomically reserve one daily send slot across all local MCP processes.

    Returns ``(record_id, used_slots)``. A missing record_id means the hard
    limit was already reached. A crashed sender remains ``sending`` for the
    current day, intentionally failing closed instead of exceeding the limit.
    """
    import uuid as _uuid
    init_db(user_id)
    record_id = _uuid.uuid4().hex[:16]
    conn = _get_conn(user_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM outreach_records "
            "WHERE user_id = ? AND status IN ('sending', 'sent') "
            "AND date(sent_at, 'localtime') = date('now', 'localtime')",
            (user_id,),
        ).fetchone()
        used = int(row["n"]) if row else 0
        if used >= daily_limit:
            conn.rollback()
            return None, used
        conn.execute(
            "INSERT INTO outreach_records "
            "(record_id, user_id, lead_id, subject, body, status, sent_at) "
            "VALUES (?, ?, ?, ?, ?, 'sending', datetime('now'))",
            (record_id, user_id, lead_id or "", subject, body),
        )
        conn.commit()
        return record_id, used + 1
    finally:
        conn.close()


def finish_outreach_send(user_id: str, record_id: str, status: str,
                         error_message: str = ""):
    """Finish a previously reserved send as ``sent`` or ``failed``."""
    if status not in {"sent", "failed"}:
        raise ValueError("发送记录状态只能是 sent 或 failed")
    conn = _get_conn(user_id)
    try:
        conn.execute(
            "UPDATE outreach_records SET status=?, error_message=? WHERE record_id=? AND user_id=?",
            (status, error_message, record_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def count_sent_today(user_id: str) -> int:
    """Count successful sends for the local calendar day (SQLite localtime)."""
    db_path = _get_db_path(user_id)
    if not db_path.exists():
        return 0
    conn = _get_conn(user_id)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM outreach_records "
            "WHERE user_id = ? AND status = 'sent' "
            "AND date(sent_at, 'localtime') = date('now', 'localtime')",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()
    return int(row["n"]) if row else 0


def insert_inquiry(user_id: str, from_address: str, subject: str, body: str,
                   lead_id: str = "", outreach_record_id: str = "",
                   intent: str = "", confidence: float = 0.0,
                   needs_human: int = 1) -> str:
    """记录一封收到的邮件(客户回复)。返回 inquiry_id。

    去重由调用方(check_inbox)按 IMAP UID 维护 seen 集合, 此函数只负责落库。
    """
    import uuid as _uuid
    init_db(user_id)
    inquiry_id = _uuid.uuid4().hex[:16]
    conn = _get_conn(user_id)
    try:
        conn.execute(
            "INSERT INTO inquiries "
            "(inquiry_id, user_id, lead_id, outreach_record_id, from_address, subject, body, "
            "intent, confidence, needs_human) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (inquiry_id, user_id, lead_id or "", outreach_record_id or "",
             from_address, subject, body, intent, confidence, needs_human),
        )
        conn.commit()
    finally:
        conn.close()
    return inquiry_id


def list_inquiries(user_id: str, only_unhandled: bool = True, limit: int = 20) -> list[dict]:
    """列出收到的邮件(默认只看 needs_human=1 的待处理回复)。"""
    db_path = _get_db_path(user_id)
    if not db_path.exists():
        return []
    conn = _get_conn(user_id)
    try:
        if only_unhandled:
            rows = conn.execute(
                "SELECT * FROM inquiries WHERE user_id = ? AND needs_human = 1 "
                "ORDER BY received_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM inquiries WHERE user_id = ? "
                "ORDER BY received_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# ── Tasks (异步任务追踪, 方案B) ──

def create_task(user_id: str, task_type: str, limit: int = None, total: int = 0) -> str:
    """创建一条 pending 任务, 返回 task_id。user_id 强制绑定。"""
    import uuid as _uuid
    init_db(user_id)
    task_id = _uuid.uuid4().hex
    conn = _get_conn(user_id)
    try:
        conn.execute(
            "INSERT INTO tasks (id, user_id, task_type, status, limit_int, total, processed) "
            "VALUES (?, ?, ?, 'pending', ?, ?, 0)",
            (task_id, user_id, task_type, limit, total),
        )
        conn.commit()
    finally:
        conn.close()
    return task_id


def set_task_running(task_id: str, user_id: str):
    conn = _get_conn(user_id)
    try:
        conn.execute(
            "UPDATE tasks SET status = 'running', updated_at = datetime('now') "
            "WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_task_progress(task_id: str, user_id: str, processed: int, total: int):
    """短事务, 立即 commit, 降低与 leads 写锁冲突。"""
    conn = _get_conn(user_id)
    try:
        conn.execute(
            "UPDATE tasks SET processed = ?, total = ?, updated_at = datetime('now') "
            "WHERE id = ? AND user_id = ?",
            (processed, total, task_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def complete_task(task_id: str, user_id: str, result_summary: str):
    conn = _get_conn(user_id)
    try:
        conn.execute(
            "UPDATE tasks SET status = 'done', result_summary = ?, "
            "processed = total, updated_at = datetime('now') "
            "WHERE id = ? AND user_id = ?",
            (result_summary, task_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def fail_task(task_id: str, user_id: str, error: str):
    conn = _get_conn(user_id)
    try:
        conn.execute(
            "UPDATE tasks SET status = 'failed', error = ?, updated_at = datetime('now') "
            "WHERE id = ? AND user_id = ?",
            (error, task_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_task(task_id: str, user_id: str):
    """读取任务详情。user_id 强制隔离: 跨用户查询返回 None。"""
    db_path = _get_db_path(user_id)
    if not db_path.exists():
        return None
    conn = _get_conn(user_id)
    try:
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def count_pending_leads(user_id: str) -> int:
    """预估待补全邮箱的线索数(enrich_emails 的候选集合大小), 用于任务 total。

    查询条件必须与 email_enrich.enrich_emails 的 SELECT 完全一致, 但只取 COUNT。
    绝不修改任何行。
    """
    db_path = _get_db_path(user_id)
    if not db_path.exists():
        return 0
    conn = _get_conn(user_id)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM leads "
            "WHERE user_id = ? "
            "AND website IS NOT NULL AND website != '' "
            "AND (email IS NULL OR email = '' OR email_status IN ('guessed','no_mx')) "
            "AND (email_status IS NULL OR email_status NOT IN ('existing','verified','catchall'))",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()
    return int(row["n"]) if row else 0

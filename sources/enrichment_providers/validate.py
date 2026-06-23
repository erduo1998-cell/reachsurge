"""验证门: 丢弃明显无效的 provider 发现邮箱。

照搬 enrichment-kit 的语义门, 治「占位/垃圾邮箱灌库」病:
  - 域名不匹配 (邮箱域 ≠ lead 域, e.g. Hunter 返回 linkedin 残留) → 丢
  - 系统/示例/建站平台域名 (example.com / wixpress.com ...) → 丢
  - 非业务 localpart (noreply / webmaster / abuse ...) → 丢

注意: role 邮箱 (info@/sales@) 不丢 —— B2B 场景下它们是合法联系入口,
只是置信较低 (由 guess provider 兜底)。本门只拦「明显无效」, 不拦 role。

常量在此独立定义 (照搬), 不 import email_enrich (避免循环依赖、不动其内部)。
"""
from typing import Optional

# 示例/系统/建站/分析平台域名 —— 出现在邮箱里的几乎都不是真实业务邮箱
JUNK_DOMAINS = (
    'example.com', 'example.org', 'test.com', 'email.com', 'youremail.com',
    'sentry.io', 'sentry.com', 'wixpress.com', 'googleapis.com', 'w3.org',
    'schema.org', 'gravatar.com', 'wordpress.com', 'github.com',
)

# 非业务联系性质的 localpart —— 抓到即丢
NON_BUSINESS_LOCALPARTS = frozenset((
    'noreply', 'no-reply', 'donotreply', 'do-not-reply', 'webmaster',
    'postmaster', 'abuse', 'admin', 'administrator', 'root',
))


def is_valid_enrichment_email(email: Optional[str], domain: str) -> bool:
    """provider 发现的邮箱是否可信。email 需小写。

    判定:
      1. 域名必须 == lead 的 domain (跨域邮箱多是残留/聚合, 丢)
      2. localpart 非系统角色 (noreply/webmaster ...)
      3. 不含垃圾域名片段
    """
    if not email:
        return False
    e = email.lower().strip()
    if '@' not in e:
        return False
    local, _, host = e.rpartition('@')
    # 1. 域名匹配 (host 可能带子域, 取末两段比较太严格; 用 == domain + 后缀兜底)
    if host != domain and not host.endswith('.' + domain):
        return False
    # 2. 非业务 localpart
    if local in NON_BUSINESS_LOCALPARTS:
        return False
    # 3. 垃圾域名
    if any(b in e for b in JUNK_DOMAINS):
        return False
    return True

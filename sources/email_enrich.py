"""邮箱补全模块: 对 email 为空的线索做域名 MX + SMTP RCPT 探测补全。

Provenance: 移植自已验证的 /tmp/enrich_emails.py (Session 27, 58 家 Ear 库实测)。
SMTP/DNS 逻辑保持 verbatim —— 编码了 3 个关键 pitfall, 不可改动:

  1. SMTP 必须连 MX host 的 IPv4, 不能连 domain 的 A 记录
     (A 记录通常是 web server, port 25 关闭 → Connection refused)。
  2. WSL 无 IPv6 路由, 必须强制 AF_INET (否则 getaddrinfo 偏好 AAAA →
     'Network is unreachable')。_resolve_ipv4 强制 AF_INET。
  3. 大型反垃圾服务商 MX (outlook/google/mimecast/proofpoint) 统一拒绝
     CN-IP 探测 —— 直接跳过 verify 走 info@ 猜测, 不浪费时间。

契约 (CRITICAL):
  - 只补全 email 为空 (NULL 或 '') 的行; 绝不覆盖已有邮箱或已验证邮箱。
  - SELECT 排除 email_status IN ('existing','verified'), UPDATE 带
    `email IS NULL OR email=''` 双保险。
  - 幂等: 重复运行不重复写、不覆盖已填充结果。

返回: {"total": int, "updated": int, "counts": {status: count, ...}}
status ∈ {verified, scraped, guessed, catchall, no_mx, no_domain}

scraped = 网站 /kontakt /impressum 等页面公开的真实邮箱(公司自公开，可信度高于 guessed，不经 SMTP)
"""
import sys
import re
import time
import smtplib
import socket
import ipaddress
import urllib.request
import urllib.error
import os
from urllib.parse import urlparse, urljoin

# 支持从任意工作目录直接运行/导入本文件。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from security import validate_public_http_url

import dns.resolver
import dns.exception

# ── 模块常量 (verbatim from /tmp/enrich_emails.py) ──

FROM_ADDR = 'test@probe.invalid'
SMTP_TIMEOUT = 12
MX_NAMESERVERS = ['8.8.8.8', '1.1.1.1']
# priority: info most likely for German B2B
PREFIXES = ['info', 'vertrieb', 'kontakt', 'einkauf', 'sales', 'office', 'mail']
CATCHALL_PROBE_LOCAL = 'xqznonexist99887766'

# MX hosts hosted by big anti-spam providers that uniformly reject CN-IP SMTP probes.
# These can't be reliably verified via RCPT -> guessed (info@).
BIG_PROVIDER_MX_MARKERS = (
    'protection.outlook.com', '.outlook.com',
    'google.com', '.googlemail.com',   # Google Workspace: all RCPT -> 454 relay denied
    'mimecast.com',                    # often blocks unknown IPs
    'proofpoint.com',                  # pphosted -> often 554
)

# ── 网站深抓常量 (借鉴 Scout enrichment.py，自写非复制) ──
EMAIL_RE = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'
# 明显非真实/系统/示例邮箱，丢弃
EMAIL_BLACKLIST = ('example.com', 'test.com', 'email.com', 'youremail.com',
                   'sentry.io', 'wixpress.com', 'googleapis.com', 'w3.org',
                   'schema.org', 'gravatar.com', 'wordpress.com', 'sentry.com')
# 非"业务联系"性质的 localpart，丢弃（防止抓到 webmaster/noreply）
NON_BUSINESS_LOCALPARTS = frozenset((
    'noreply', 'no-reply', 'donotreply', 'do-not-reply', 'webmaster',
    'postmaster', 'abuse', 'admin', 'administrator', 'root',
))
# website 指向这些域名的对找邮箱无用（社交/短链/建站平台），跳过深抓
USELESS_WEB_DOMAINS = ('youtube.com', 'youtu.be', 'instagram.com', 'tiktok.com',
                       'twitter.com', 'x.com', 'facebook.com', 'linktr.ee',
                       'stan.store', 'beacons.ai', 'bit.ly', 'spotify.com',
                       'linkedin.com', 'pinterest.com', 'wix.com', 'squarespace.com')
# 联系页路径按目标市场语言动态选择(通用智能体: 任意国家都适配)。
# 根页 '' 始终第一; 加市场对应语言路径; 英语 /contact /about 兜底(多数公司有英文版)。
_CONTACT_PATHS_BY_COUNTRY = {
    'DE': ('', '/kontakt', '/impressum', '/contact', '/about'),
    'AT': ('', '/kontakt', '/impressum', '/contact', '/about'),
    'CH': ('', '/kontakt', '/impressum', '/contact', '/about'),
    'BR': ('', '/contato', '/fale-conosco', '/fale conosco', '/contact', '/about'),
    'PT': ('', '/contacto', '/contactos', '/contact', '/about'),
    'ES': ('', '/contacto', '/contact', '/about'),
    'MX': ('', '/contacto', '/contacto-us', '/contact', '/about'),
    'AR': ('', '/contacto', '/contact', '/about'),
    'FR': ('', '/contact', '/nous-contacter', '/about'),
    'IT': ('', '/contatti', '/contatto', '/contact', '/about'),
    'NL': ('', '/contact', '/contacteer-ons', '/about'),
    'JP': ('', '/contact', '/access', '/about'),
    'CN': ('', '/contact', '/lianxi', '/about'),
}
# 默认(市场未知/英语): 国际通用路径
_CONTACT_PATHS_DEFAULT = ('', '/contact', '/contact-us', '/about', '/about-us')
# country 字段国名 -> ISO 映射(兜住 country 写法不统一 Germany/DE 的 bug)
_COUNTRY_NAME_TO_ISO = {
    'GERMANY': 'DE', 'DEUTSCHLAND': 'DE', 'AUSTRIA': 'AT', 'SWITZERLAND': 'CH',
    'BRAZIL': 'BR', 'BRASIL': 'BR', 'PORTUGAL': 'PT', 'SPAIN': 'ES', 'MEXICO': 'MX',
    'ARGENTINA': 'AR', 'FRANCE': 'FR', 'ITALY': 'IT', 'NETHERLANDS': 'NL',
    'JAPAN': 'JP', 'CHINA': 'CN',
}
# 链接发现: 根页 href 命中这些多语言关键词的内部链接也抓(抓公司自定义/非标准联系页)
_CONTACT_LINK_KEYWORDS = ('contact', 'kontakt', 'impressum', 'contato', 'contacto',
                          'contatti', 'fale', 'reach', 'get-in-touch', 'getintouch',
                          'nous', 'sobre', 'sprech', 'anfrage', 'inquiry', 'enquire')
SCRAPE_TIMEOUT = 6   # 单页超时(秒)
SCRAPE_USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                     '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')
MAX_SCRAPE_BYTES = 2 * 1024 * 1024


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


def extract_domain(website):
    if not website:
        return None
    s = website.strip()
    # add scheme if missing so urlparse finds netloc
    if '://' not in s:
        s = 'http://' + s
    netloc = urlparse(s).netloc
    if not netloc:
        # treat raw string as domain
        netloc = s.split('/')[0]
    netloc = netloc.split('@')[-1]  # strip any user@
    netloc = netloc.split(':')[0]   # strip port
    netloc = netloc.lower()
    if netloc.startswith('www.'):
        netloc = netloc[4:]
    return netloc if netloc else None


def make_resolver():
    r = dns.resolver.Resolver(configure=False)
    r.nameservers = MX_NAMESERVERS
    r.timeout = 8
    r.lifetime = 8
    return r


_resolver = make_resolver()


def get_mx(domain):
    """Return (mx_host, evidence) or (None, reason). Retry once on transient."""
    last = None
    for attempt in range(3):
        try:
            answers = _resolver.resolve(domain, 'MX')
            best = sorted(answers, key=lambda a: a.preference)[0]
            host = str(best.exchange).rstrip('.').lower()
            return host, f"mx:{host}(pref={best.preference})"
        except dns.resolver.NoAnswer:
            return None, "no_mx:NoAnswer"
        except dns.resolver.NXDOMAIN:
            return None, "no_mx:NXDOMAIN"
        except dns.resolver.NoNameservers as e:
            last = f"no_mx:NoNameservers:{e}"
            time.sleep(1)
            continue
        except dns.exception.Timeout as e:
            last = "no_mx:DNS_timeout"
            time.sleep(1)
            continue
        except Exception as e:
            last = f"no_mx:{type(e).__name__}:{e}"
            time.sleep(1)
            continue
    return None, last or "no_mx:unknown"


def _resolve_ipv4(host):
    """Resolve first IPv4 (A record) for host. WSL has no IPv6 route, so we must
    force AF_INET or getaddrinfo prefers AAAA -> 'Network is unreachable'."""
    try:
        infos = socket.getaddrinfo(host, 25, socket.AF_INET, socket.SOCK_STREAM)
        for fam, typ, proto, canon, sa in infos:
            if ipaddress.ip_address(sa[0]).is_global:
                return sa[0]  # IPv4 string
    except socket.gaierror:
        return None
    return None


def smtp_code(domain, mx_host, localpart, timeout=SMTP_TIMEOUT):
    """Try RCPT TO for localpart@domain by connecting to mx_host's IPv4.
    CRITICAL: must connect to MX host (mail server), not domain's A record
    (which is usually the web server, port 25 closed -> Connection refused).
    WSL has no IPv6 route, so we force AF_INET.
    Retries once on transient CONN_DISCONN/CONN_TIMEOUT (some servers flap)."""
    addr = f"{localpart}@{domain}"
    ip = _resolve_ipv4(mx_host)
    if not ip:
        return None, f"CONN_NO_IPV4:{mx_host}:resolve_failed", True

    last_result = None
    for attempt in range(2):
        try:
            with smtplib.SMTP(timeout=timeout) as s:
                s.connect(ip, 25)
                s.ehlo_or_helo_if_needed()
                s.mail(FROM_ADDR)
                code, msg = s.rcpt(addr)
                try:
                    s.quit()
                except Exception:
                    pass
                text = msg.decode('utf-8', 'replace') if isinstance(msg, (bytes, bytearray)) else str(msg)
                return code, text, False
        except smtplib.SMTPConnectError as e:
            return None, f"CONN_REFUSED:{e}", True  # port closed, no point retrying
        except smtplib.SMTPServerDisconnected as e:
            last_result = (None, f"CONN_DISCONN:{e}", True)
            if attempt == 0:
                time.sleep(2); continue
            return last_result
        except socket.timeout:
            last_result = (None, "CONN_TIMEOUT", True)
            if attempt == 0:
                time.sleep(2); continue
            return last_result
        except smtplib.SMTPResponseException as e:
            err_txt = e.smtp_error.decode('utf-8', 'replace') if isinstance(e.smtp_error, (bytes, bytearray)) else str(e.smtp_error)
            return e.smtp_code, f"SMTP_RE:{e.smtp_code}:{err_txt}", (e.smtp_code >= 500)
        except OSError as e:
            last_result = (None, f"CONN_OSErr:{type(e).__name__}:{e}", True)
            if attempt == 0:
                time.sleep(2); continue
            return last_result
        except Exception as e:
            return None, f"CONN_ERR:{type(e).__name__}:{e}", True
    return last_result


def is_big_provider_mx(mx_host):
    return any(m in mx_host for m in BIG_PROVIDER_MX_MARKERS)


def classify(domain, mx_host):
    """Return (status, email, evidence). status ∈ {verified, guessed, catchall}.

    Status strings are the final DB values (not the original script's
    guessed_unverified / catchall_domain).
    """
    # 1. big-provider hosted MX -> CN IP rejected uniformly, skip verify
    if is_big_provider_mx(mx_host):
        provider = 'outlook' if 'outlook.com' in mx_host else ('google' if ('google.com' in mx_host or 'googlemail.com' in mx_host) else 'bigprovider')
        return 'guessed', f"info@{domain}", f"{provider}_mx:{mx_host}|CN-IP-blocked"

    # 2. catch-all probe (only meaningful for self-hosted SMTP)
    code, text, cerr = smtp_code(domain, mx_host, CATCHALL_PROBE_LOCAL)
    if cerr:
        # connection error on probe -> can't verify at all, fallback
        return 'guessed', f"info@{domain}", f"catchall_probe_connerr:{text}"
    if code in (250, 251):
        # catch-all domain: nonexistent probe accepted
        return 'catchall', f"info@{domain}", f"catchall:true(probe={code})"
    # probe 550 -> domain NOT catch-all, proceed to prefix verify (good)
    # probe 4xx (relay/greylist) on self-hosted -> ambiguous, but still try real prefixes
    #   (some servers reject probe-like nonsense but accept real addresses)
    # probe other 2xx/5xx -> also try prefix verify, trust per-prefix result

    # 3. prefix verify
    last_resp = None
    for pfx in PREFIXES:
        code2, text2, cerr2 = smtp_code(domain, mx_host, pfx)
        if cerr2:
            # connection broke mid-way; can't continue reliably, fallback
            return 'guessed', f"info@{domain}", f"prefix_connerr@{pfx}:{text2}"
        last_resp = f"{pfx}@->{code2}"
        if code2 in (250, 251):
            return 'verified', f"{pfx}@{domain}", f"{pfx}@->{code2}|probe_was_{code}"
        # 550/4xx/etc -> not accepted, try next
    # no prefix accepted
    ev = f"all_rejected:last={last_resp}|probe_was_{code}" if last_resp else f"no_prefixes_tried|probe_was_{code}"
    return 'guessed', f"info@{domain}", ev


def _website_is_useful(website):
    """social/short-link/building-platform domains 对找邮箱无用。"""
    if not website:
        return False
    w = website.lower()
    return not any(d in w for d in USELESS_WEB_DOMAINS)


def _fetch_url(url, timeout=SCRAPE_TIMEOUT):
    """Fetch public HTML with redirect and response-size guards."""
    class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            validate_public_http_url(newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    try:
        validate_public_http_url(url)
        req = urllib.request.Request(url, headers={'User-Agent': SCRAPE_USER_AGENT})
        opener = urllib.request.build_opener(_SafeRedirectHandler())
        with opener.open(req, timeout=timeout) as resp:
            if getattr(resp, 'status', 200) != 200 and getattr(resp, 'code', 200) != 200:
                return None
            declared = int(resp.headers.get('Content-Length', '0') or 0)
            if declared > MAX_SCRAPE_BYTES:
                return None
            data = resp.read(MAX_SCRAPE_BYTES + 1)
            if len(data) > MAX_SCRAPE_BYTES:
                return None
            # 多数欧洲站点 utf-8；容错降级
            try:
                return data.decode('utf-8', errors='replace')
            except Exception:
                return data.decode('latin-1', errors='replace')
    except Exception:
        return None


def _is_valid_scraped(email_lower, domain):
    """深抓邮箱过滤：必须属于 lead 自己的 domain；排除系统/示例邮箱。"""
    if not email_lower.endswith('@' + domain):
        return False
    local = email_lower.split('@', 1)[0]
    if local in NON_BUSINESS_LOCALPARTS:
        return False
    if any(b in email_lower for b in EMAIL_BLACKLIST):
        return False
    return True


def _contact_paths_for(country):
    """按目标市场 country 返回联系页路径列表(根页 '' 始终第一)。
    兼容 ISO-2(DE/BR...) 和国名(Germany/Brazil...); 未知市场用默认国际路径。"""
    if not country:
        return _CONTACT_PATHS_DEFAULT
    c = country.strip().upper()
    if c in _CONTACT_PATHS_BY_COUNTRY:
        return _CONTACT_PATHS_BY_COUNTRY[c]
    if c in _COUNTRY_NAME_TO_ISO:
        return _CONTACT_PATHS_BY_COUNTRY[_COUNTRY_NAME_TO_ISO[c]]
    return _CONTACT_PATHS_DEFAULT


def _discover_contact_links(html, base_url, max_links=5):
    """从根页 HTML 提取联系类内部链接(href 命中多语言 contact 关键词)。
    只收相对路径(同站)拼成绝对 URL; 去重, 最多 max_links 个。
    通用性关键: 不靠猜固定路径, 公司自定义的 /get-in-touch /reach-us /fale-conosco 也能发现。"""
    if not html:
        return []
    found = []
    seen = set()
    for href in re.findall(r'href=["\']([^"\']+)["\']', html, re.IGNORECASE):
        h = href.strip().split('#')[0].split('?')[0]
        if not h:
            continue
        if h.startswith(('mailto:', 'tel:', 'javascript:', 'data:', 'http://', 'https://')):
            continue  # 只收相对路径(同站), 跳过外链/锚点/伪协议
        low = h.lower()
        if not any(k in low for k in _CONTACT_LINK_KEYWORDS):
            continue
        full = urljoin(base_url, h)
        if full not in seen:
            seen.add(full)
            found.append(full)
        if len(found) >= max_links:
            break
    return found


def _deep_scrape_emails(website, domain, country=None):
    """抓 website 的联系页(kontakt/impressum/contato/contact 等, 按 country 选语言),
    提取属于该 domain 的真实邮箱。返回去重小写列表; 一旦抓到 role 邮箱(info/sales/...)即提前停止。

    通用性两机制:
      ① 按 country 动态选联系页路径(德国 /kontakt /impressum, 巴西 /contato ...)
      ② 链接发现: 从根页 href 找命中的联系类链接, 抓公司自定义/非标准联系页
    根页 HTML 抓一次后复用, 不二次请求。"""
    base = website.strip()
    if not base:
        return []
    if not base.startswith('http'):
        base = 'https://' + base
    base = base.rstrip('/')

    # 1. 抓根页(一次), 复用 + 做链接发现
    root_html = _fetch_url(base)
    discovered = _discover_contact_links(root_html, base)

    # 2. 组装待抓 URL: country 语言路径(根页优先) + 链接发现页
    pages = []
    for p in _contact_paths_for(country):
        url = base + p
        if url not in pages:
            pages.append(url)
    for d in discovered:
        if d not in pages:
            pages.append(d)

    found = []
    got_role = False
    for url in pages:
        html = root_html if url == base else _fetch_url(url)  # 根页复用, 不二次抓
        if not html:
            continue
        for raw in re.findall(EMAIL_RE, html):
            el = raw.lower()
            if not _is_valid_scraped(el, domain):
                continue
            if el not in found:
                found.append(el)
                if el.split('@', 1)[0] in PREFIXES:
                    got_role = True
        if got_role:
            break   # 已抓到 role 邮箱(info/sales/...), 不必再翻页
    return found


def _pick_best_scraped(emails_lower, domain):
    """从深抓到的邮箱里挑最佳：优先 PREFIXES 顺序的 role 邮箱(info 最优)，否则取 localpart 最短的。"""
    if not emails_lower:
        return None
    for pfx in PREFIXES:
        cand = pfx + '@' + domain
        if cand in emails_lower:
            return cand
    return sorted(emails_lower, key=lambda e: (len(e.split('@', 1)[0]), e))[0]


# ── Driver (改调 enrichment 瀑布, proven 函数与 SELECT/UPDATE 契约不变) ──

def enrich_emails(user_id, limit=None):
    """批量补全线索邮箱(瀑布: scrape→hunter→smtp_probe→guess + SQLite 缓存)。

    对每个候选 domain 调 enrichment_providers.waterfall.enrich_domain:
      先查缓存(命中跳整条瀑布, 省 Hunter 配额 + 省 SMTP);
      未命中按 provider 顺序首命中即停, 结果写缓存。
    SELECT 候选 / UPDATE 写回的 SQL 与 idempotency 守卫与原版逐字一致 ——
    只补 email 为空或 guessed/no_mx 的行, 绝不覆盖 verified/existing/catchall。

    返回: {"total": int, "updated": int, "counts": {status: count, ...}}
    """
    from storage.db import _get_conn, init_db
    from sources.enrichment_providers.cache import EnrichmentCache
    from sources.enrichment_providers.waterfall import enrich_domain

    init_db(user_id)
    cache = EnrichmentCache(user_id)

    # 1. 读候选行 (慢 SMTP/Hunter 循环前关闭连接, 避免 cursor-reused bug)
    read_conn = _get_conn(user_id)
    try:
        sql = (
            "SELECT lead_id, company_name, website, email, country FROM leads "
            "WHERE user_id = ? "
            "AND website IS NOT NULL AND website != '' "
            "AND (email IS NULL OR email = '' OR email_status IN ('guessed','no_mx')) "
            "AND (email_status IS NULL OR email_status NOT IN ('existing','verified','catchall'))"
        )
        params = [user_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = read_conn.execute(sql, params).fetchall()
    finally:
        read_conn.close()

    total = len(rows)
    counts = {"verified": 0, "scraped": 0, "guessed": 0,
              "catchall": 0, "no_mx": 0, "no_domain": 0}
    updates = []  # (lead_id, email, status, contact_name, contact_title) 待写

    _log(f"[enrich_emails] start user={user_id} target={total}")

    for lead_id, company, website, email, country in rows:
        domain = extract_domain(website)
        if not domain:
            # 只对"原本邮箱为空"的才记 no_domain；guessed 的原本就有 domain 不会走到这
            if not email:
                counts["no_domain"] += 1
                updates.append((lead_id, "", "no_domain", "", ""))
            continue

        result = enrich_domain(domain, website, country, cache)
        if result is None:
            continue   # 瀑布无果(极少, guess 是终端兜底)
        counts[result.status] = counts.get(result.status, 0) + 1
        updates.append((lead_id, result.email, result.status, result.contact_name, result.contact_title))

    # 2. 单连接批量写 (idempotency: 允许覆盖空 或 guessed/no_mx；绝不碰 verified/existing/catchall)
    updated = 0
    write_conn = _get_conn(user_id)
    try:
        for lead_id, em, status, cname, ctitle in updates:
            write_conn.execute(
                "UPDATE leads SET email = ?, email_status = ?, updated_at = datetime('now') "
                "WHERE lead_id = ? AND user_id = ? "
                "AND (email IS NULL OR email = '' OR email_status IN ('guessed','no_mx'))",
                (em, status, lead_id, user_id),
            )
            # 联系人只填空, 不覆盖已有非空 (避免覆盖用户手填/其他来源的人名; 避免同 domain cache 复用错挂)
            if cname:
                write_conn.execute(
                    "UPDATE leads SET contact_name = ? "
                    "WHERE lead_id = ? AND user_id = ? "
                    "AND (contact_name IS NULL OR contact_name = '')",
                    (cname, lead_id, user_id),
                )
            if ctitle:
                write_conn.execute(
                    "UPDATE leads SET contact_title = ? "
                    "WHERE lead_id = ? AND user_id = ? "
                    "AND (contact_title IS NULL OR contact_title = '')",
                    (ctitle, lead_id, user_id),
                )
            updated += 1
        write_conn.commit()
    finally:
        write_conn.close()

    _log(f"[enrich_emails] done total={total} updated={updated} counts={counts}")

    return {
        "total": total,
        "updated": updated,
        "counts": counts,
    }

"""邮箱验证 —— 格式检查 + MX 记录查询 + SMTP 握手。

不实际发送邮件，只做 SMTP 握手验证（RCPT TO 后 QUIT）。
"""
import re
import smtplib
import socket
import ssl
import dns.resolver

from security import validate_public_host

# 常见 disposable email 域名
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "tempmail.com", "10minutemail.com",
    "yopmail.com", "throwaway.email", "sharklasers.com", "trashmail.com",
    "temp-mail.org", "maildrop.cc", "getnada.com", "dispostable.com",
}


def verify_email_smtp(email: str, timeout: int = 10) -> str:
    """验证邮箱有效性：格式 → MX → SMTP 握手。

    Returns:
        格式化的验证结果字符串。
    """
    # 1. 格式检查
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        return f"❌ {email} — 格式无效"

    domain = email.split("@")[1].lower()

    # 2. Disposable 检查
    if domain in DISPOSABLE_DOMAINS:
        return f"⚠️ {email} — 一次性邮箱（{domain}），不建议使用"

    # 3. MX 记录查询
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5)
        mx_records = sorted(
            [(r.preference, str(r.exchange).rstrip(".")) for r in answers],
            key=lambda x: x[0],
        )
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
        return f"❌ {email} — 域名 {domain} 无 MX 记录（无法接收邮件）"
    except dns.resolver.LifetimeTimeout:
        return f"⚠️ {email} — DNS 查询超时，无法验证"
    except Exception as e:
        return f"⚠️ {email} — DNS 查询异常: {e}"

    if not mx_records:
        return f"❌ {email} — 域名 {domain} 无 MX 记录"

    # 4. SMTP 握手验证
    mx_host = mx_records[0][1]
    try:
        validate_public_host(mx_host, 25)
        smtp = smtplib.SMTP(mx_host, timeout=timeout)
        smtp.ehlo_or_helo_if_needed()
        # 有些服务器需要 STARTTLS
        try:
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo_or_helo_if_needed()
        except smtplib.SMTPException:
            pass

        # MAIL FROM + RCPT TO 验证，不实际发信
        smtp.mail("verify@example.com")
        code, message = smtp.rcpt(email)
        smtp.quit()

        if code == 250:
            return f"✅ {email} — 有效（MX: {mx_host}，SMTP 验证通过）"
        elif code == 550:
            return f"❌ {email} — 邮箱不存在（{message.decode() if isinstance(message, bytes) else message}）"
        elif 500 <= code < 600:
            return f"⚠️ {email} — 被拒: {code} {message.decode() if isinstance(message, bytes) else message}"
        else:
            return f"⚠️ {email} — SMTP 返回 {code}，可能存在但未确认"

    except smtplib.SMTPServerDisconnected:
        return f"⚠️ {email} — SMTP 服务器断开（{mx_host}），可能是灰名单保护"
    except smtplib.SMTPConnectError:
        return f"⚠️ {email} — 无法连接 {mx_host}，可能是防火墙拦截"
    except socket.timeout:
        return f"⚠️ {email} — 连接 {mx_host} 超时"
    except Exception as e:
        return f"⚠️ {email} — SMTP 验证异常: {type(e).__name__}"

"""社交 profile 抓取源。借鉴 kiryano/Scout 思路自写,不依赖 Scout 包(绝不 pip install)。

抓取思路:
- TikTok: requests 抓 https://www.tiktok.com/@{username},正则提
  <script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">...</script> 的 SSR JSON,
  取 __DEFAULT_SCOPE__.webapp.user-detail.userInfo。桌面 UA,稳定无需重试。
- Instagram: requests + 移动 UA + Accept: text/html 头,正则提
  biography/full_name/follower_count 等。**完整性判断=提取到 follower_count 才算成功**,
  否则降级页重试(默认 5 次)。禁用 "description":"..." 假阳性 fallback(实测会吐 'intern site in general')。

查询类工具,不进 SOURCE_REGISTRY,不入库。代理走 LEADGEN_PROXY/HTTPS_PROXY 环境变量,无则直连,
节点须美国(TikTok 对 HK 节点 302,/hk/about)。
"""
import re
import os
import json
import time
import random

import requests

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

# 借鉴 Scout instagram.py MOBILE_USER_AGENTS (Read 后照抄 4 个)
MOBILE_UAS = [
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36',
    'Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36',
]

# 借鉴 Scout tiktok.py desktop UA
TIKTOK_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'

UNIVERSAL_DATA_RE = re.compile(
    r'<script\s+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
    re.DOTALL,
)


def _get_proxy():
    """代理 URL (纯环境变量驱动): LEADGEN_PROXY > HTTPS_PROXY > HTTP_PROXY。

    无任何代理 env 返回 None; 取到则补 http:// 前缀并返回 requests 的 proxies dict。
    """
    url = (os.environ.get('LEADGEN_PROXY')
           or os.environ.get('HTTPS_PROXY')
           or os.environ.get('HTTP_PROXY'))
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    if not url.startswith('http'):
        url = f'http://{url}'
    return {'http': url, 'https': url}


def _extract_email(text):
    """从文本提取首个 email,小写。借鉴 Scout utils.extract_email。"""
    if not text:
        return None
    m = EMAIL_RE.search(text)
    return m.group(0).lower() if m else None


def scrape_tiktok(username, proxies):
    """抓 TikTok 公开 profile。借鉴 Scout tiktok.scrape_tiktok_profile。

    取 <script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"> 的 SSR JSON,
    __DEFAULT_SCOPE__.webapp.user-detail.userInfo:
      user: uniqueId/nickname/signature(bio)/verified/bioLink.link
      stats: followerCount/followingCount/heartCount/videoCount
    从 signature+bioLink 提 email/url。TikTok 稳定无需重试。返回 dict。
    """
    url = f'https://www.tiktok.com/@{username}'
    headers = {
        'User-Agent': TIKTOK_UA,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-Dest': 'document',
        'Sec-Ch-Ua-Mobile': '?0',
        'Sec-Ch-Ua-Platform': '"Windows"',
    }
    try:
        r = requests.get(url, headers=headers, proxies=proxies, timeout=20)
    except Exception as e:
        return {
            'platform': 'tiktok', 'username': username,
            'error': f'request_failed: {type(e).__name__}: {e}',
            'status_code': None,
        }
    if r.status_code == 404:
        return {'platform': 'tiktok', 'username': username, 'error': 'not_found', 'status_code': 404}
    if r.status_code != 200:
        return {
            'platform': 'tiktok', 'username': username,
            'error': f'http_{r.status_code}', 'status_code': r.status_code,
        }
    # /hk/about 跳转=节点被 TikTok 地理墙
    if '/hk/' in (r.url or ''):
        return {
            'platform': 'tiktok', 'username': username,
            'error': 'region_blocked (redirected to /hk/about, switch proxy to US node)',
            'status_code': 200,
        }

    m = UNIVERSAL_DATA_RE.search(r.text)
    if not m:
        return {
            'platform': 'tiktok', 'username': username,
            'error': 'universal_data_not_found (page structure changed)',
            'status_code': 200,
        }
    try:
        data = json.loads(m.group(1))
        user_detail = data['__DEFAULT_SCOPE__']['webapp.user-detail']
        user_info = user_detail['userInfo']
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        return {
            'platform': 'tiktok', 'username': username,
            'error': f'json_structure: {type(e).__name__}: {e}',
            'status_code': 200,
        }

    user = user_info.get('user', {})
    stats = user_info.get('stats', {})
    bio = user.get('signature', '') or ''
    bio_link = (user.get('bioLink') or {}).get('link', '') or ''
    website = bio_link
    email = _extract_email(bio) or _extract_email(bio_link)

    return {
        'platform': 'tiktok',
        'username': user.get('uniqueId', username),
        'full_name': user.get('nickname', ''),
        'bio': bio,
        'verified': bool(user.get('verified', False)),
        'followers': stats.get('followerCount', 0),
        'following': stats.get('followingCount', 0),
        'likes': stats.get('heartCount', 0),
        'videos': stats.get('videoCount', 0),
        'website': website,
        'email': email,
        'profile_url': url,
        'status_code': 200,
    }


def _parse_abbreviated_number(s):
    """借鉴 Scout utils.parse_abbreviated_number: '11.5K'/'2.3M'/'1.2B' -> int。"""
    if s is None:
        return None
    s = str(s).strip().replace(',', '')
    if not s:
        return None
    multipliers = {'K': 1_000, 'M': 1_000_000, 'B': 1_000_000_000}
    for suffix, mult in multipliers.items():
        if s.upper().endswith(suffix):
            try:
                return int(float(s[:-1]) * mult)
            except (ValueError, IndexError):
                return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _extract_ig(html, username):
    """从 IG HTML 提 profile。借鉴 Scout instagram._extract_profile_from_html regex 集。

    ⚠️ 完整性判断=follower_count 提取到才算成功(否则降级页假阳性)。
    ⚠️ 禁用 "description":"..." fallback(降级页吐 'intern site in general' 类假阳性)。
    """
    results = {}

    for pat in (r'"username":"([^"]+)"',):
        m = re.search(pat, html)
        if m and m.group(1).lower() == username.lower():
            results['username'] = m.group(1)
            break

    for pat in (r'"full_name":"([^"]*)"', r'"name":"([^"]*)"'):
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            results['full_name'] = m.group(1).strip()
            break

    # ⚠️ 只取 biography,不取 description(降级页假阳性)
    m = re.search(r'"biography":"([^"]*)"', html)
    if m:
        try:
            decoded = m.group(1).encode('utf-8').decode('unicode_escape')
            results['biography'] = decoded.encode('utf-16', 'surrogatepass').decode('utf-16')
        except (UnicodeDecodeError, UnicodeEncodeError):
            results['biography'] = m.group(1).replace('\\u', '')

    for pat in (r'"follower_count":(\d+)', r'"edge_followed_by":\{"count":(\d+)\}'):
        m = re.search(pat, html)
        if m:
            results['follower_count'] = int(m.group(1))
            break

    for pat in (r'"following_count":(\d+)', r'"edge_follow":\{"count":(\d+)\}'):
        m = re.search(pat, html)
        if m:
            results['following_count'] = int(m.group(1))
            break

    # og:meta 兜底(scout 也用),但仍是完整性信号(非假阳性源)
    meta_pat = re.compile(
        r'content="([\d.,]+[KMB]?)\s*Followers?,\s*([\d.,]+[KMB]?)\s*Following,\s*([\d.,]+[KMB]?)\s*Posts?',
        re.IGNORECASE,
    )
    mm = meta_pat.search(html)
    if mm:
        results.setdefault('follower_count', _parse_abbreviated_number(mm.group(1)))
        results.setdefault('following_count', _parse_abbreviated_number(mm.group(2)))
        results.setdefault('media_count', _parse_abbreviated_number(mm.group(3)))

    for pat in (r'"is_verified":(true|false)', r'"verified":(true|false)'):
        m = re.search(pat, html)
        if m:
            results['is_verified'] = m.group(1) == 'true'
            break

    for pat in (r'"external_url":"([^"]+)"', r'"website":"([^"]+)"'):
        m = re.search(pat, html)
        if m:
            try:
                decoded = m.group(1).replace('\\/', '/').encode('utf-8').decode('unicode_escape')
                results['external_url'] = decoded.encode('utf-16', 'surrogatepass').decode('utf-16')
            except (UnicodeDecodeError, UnicodeEncodeError):
                results['external_url'] = m.group(1).replace('\\/', '/')
            break

    # ⚠️ 完整性: follower_count 提不到=降级页,不算成功
    if 'follower_count' not in results or results.get('follower_count') is None:
        return None
    return results


def scrape_instagram(username, proxies, max_retries=5):
    """抓 IG 公开 profile(免认证)。借鉴 Scout instagram.scrape_profile_no_login。

    headers 必须含 Accept: text/html,...,q=0.9,*/*;q=0.8 + Accept-Language: en-US,en;q=0.9
    (少 Accept 头会降级), User-Agent 随机 MOBILE_UAS。循环 max_retries:
      html=requests.get(.../instagram.com/{username}/,...,timeout=20).text
      data=_extract_ig(html,username); if data and data.get('follower_count'): return data
      time.sleep(1.5)
    返回最后一次(可能仍降级,标 degraded=True, follower_count=None)。
    """
    url = f'https://www.instagram.com/{username}/'
    headers_base = {
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }

    last_status = None
    last_error = None

    for attempt in range(max_retries):
        headers = dict(headers_base)
        headers['User-Agent'] = random.choice(MOBILE_UAS)
        try:
            r = requests.get(url, headers=headers, proxies=proxies, timeout=20)
        except Exception as e:
            last_error = f'request_failed: {type(e).__name__}: {e}'
            if attempt < max_retries - 1:
                time.sleep(1.5)
                continue
            break

        last_status = r.status_code
        last_error = None
        if r.status_code == 404:
            return {'platform': 'instagram', 'username': username, 'error': 'not_found', 'status_code': 404}
        if r.status_code == 429:
            last_error = 'rate_limited_429'
            if attempt < max_retries - 1:
                time.sleep(2.0)
                continue
            break
        if r.status_code != 200:
            last_error = f'http_{r.status_code}'
            if attempt < max_retries - 1:
                time.sleep(1.5)
                continue
            break

        html = r.text
        # 登录墙/降级页判断
        if '/accounts/login' in (r.url or '') or (
            'login' in html[:5000].lower() and 'password' in html[:5000].lower()
        ):
            last_error = 'login_wall'
            if attempt < max_retries - 1:
                time.sleep(1.5)
                continue
            break

        data = _extract_ig(html, username)
        if data:
            bio = data.get('biography', '') or ''
            website = data.get('external_url', '') or ''
            email = _extract_email(bio) or _extract_email(website)
            return {
                'platform': 'instagram',
                'username': data.get('username', username),
                'full_name': data.get('full_name', ''),
                'bio': bio,
                'verified': bool(data.get('is_verified', False)),
                'followers': data.get('follower_count', 0),
                'following': data.get('following_count', 0),
                'posts': data.get('media_count', 0),
                'website': website,
                'email': email,
                'profile_url': url,
                'status_code': 200,
                'degraded': False,
            }
        # 降级页,重试
        if attempt < max_retries - 1:
            time.sleep(1.5)
            continue

    # 全部重试失败,返回降级标志
    return {
        'platform': 'instagram',
        'username': username,
        'bio': None,
        'followers': None,
        'following': None,
        'website': None,
        'email': None,
        'profile_url': url,
        'status_code': last_status,
        'degraded': True,
        'error': last_error or 'extraction_failed_after_retries',
    }


def social_profile_lookup(platform, username):
    """统一入口: platform in {'tiktok','instagram'}。

    返回 dict(成功: platform/username/followers/following/bio/verified/website/email;
    失败/降级: 诚实标注 error/degraded)。
    """
    platform = (platform or '').strip().lower()
    uname = (username or '').strip().lstrip('@')
    if not uname:
        return {'error': 'missing username'}
    proxies = _get_proxy()
    if platform == 'tiktok':
        return scrape_tiktok(uname, proxies)
    if platform == 'instagram':
        return scrape_instagram(uname, proxies)
    return {'error': f'unsupported platform: {platform} (supported: tiktok, instagram)'}

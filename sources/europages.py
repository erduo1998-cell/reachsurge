"""档案源适配器: Europages (欧洲最大 B2B 平台)。

补 gosom 抓不到的欧洲 LED 进口商/分销商 —— 关键价值是 supplierTypes
(production/distribution/wholesaler/...) 映射 buyer_type, 用于区分
"经销商门店"(gosom 抓的) vs "真实买家角色"(europages 的 supplierTypes)。

数据源 (实测锁定):
  数据在搜索页 SSR 的 __NUXT_DATA__ payload (Nuxt3 devalue 序列化的扁平
  引用数组), 不是 JSON API 也不是 __NUXT__ (后者是空 config)。路径:
  state.$ssearchCompaniesResponse.companies[] —— 每家含 name/email/
  phoneNumber/homepage/countryCode/city/slug/supplierTypes/description。
  ⚠️ email 字段列表页恒空 (emailExisting=true 但 email=""), 邮箱在详情页。

WAF 过法 (验证 Situation 2, dump 确认):
  Europages 全站前置 AWS WAF。主页是 silent challenge (chromium 1秒自动过),
  但调 API 才触发 puzzle (405 + gokuProps)。链路:
    chromium silent 过主页 → context.request 调 SEARCH_API 抓 405 puzzle 的
    gokuProps(key/iv/context)+challenge.js → 喂 capsolver AntiAwsWafTaskProxyLess
    Situation 2 (awsKey/awsIv/awsContext/awsChallengeJS) → 0秒拿有效 aws-waf-token
    → requests 带 token GET 搜索页 (200) → 解析 __NUXT_DATA__。
  ⚠️ Situation 1 (只传 websiteURL) 失败: capsolver 对 europages 直接 200 不 challenge。
  ⚠️ puzzle gokuProps 实时会过期, chromium 抓 → capsolver 解 必须同一流程内。
  ⚠️ 需 CAPSOLVER_API_KEY 环境变量或 ~/.capsolver_key 文件。
"""
import hashlib
import json
import os
import re
import time

from .base import LeadCandidate
from .gosom import _iso_for, _en_for_iso

# 启动时检测 playwright 可用性，不可用则静默降级（不影响其他源）
try:
    from playwright.sync_api import sync_playwright  # noqa: F401
    ENABLED = True
except ImportError:
    ENABLED = False

_HOME = "https://www.europages.co.uk/"
# 这个 JSON API 已下线 (带 token 调返回 404), 但未带 token 时返回 405+puzzle,
# 专门用来触发 puzzle 抓 gokuProps (见 get_waf_cookies)。
_SEARCH_API = _HOME + "search-frontend/alibaba-api/online.company.search"
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")

# europages supplierTypes → buyer_type (dump 实测值)
_SUPPLIER_TYPE_MAP = {
    "production": "manufacturer",
    "distribution": "distributor",
    "wholesaler": "wholesaler",
    "service": "service",
    "customer_specific_manufacturing": "manufacturer",
    "importer": "importer",
}

_CAPSOLVER_CREATE = "https://api.capsolver.com/createTask"
_CAPSOLVER_RESULT = "https://api.capsolver.com/getTaskResult"


def _gateway_proxy() -> str:
    """代理 URL (纯环境变量驱动): LEADGEN_PROXY > HTTPS_PROXY > HTTP_PROXY。

    返回 http://host:port 形式; 无任何代理 env 则返回空串 (requests 直连)。
    """
    env_p = (os.environ.get("LEADGEN_PROXY")
             or os.environ.get("HTTPS_PROXY")
             or os.environ.get("HTTP_PROXY")
             or "")
    env_p = env_p.strip()
    if not env_p:
        return ""
    if not env_p.startswith("http"):
        env_p = f"http://{env_p}"
    return env_p


def _proxies():
    proxy_url = _gateway_proxy()
    return {"http": proxy_url, "https": proxy_url} if proxy_url else None


def _capsolver_key() -> str:
    """优先环境变量, 否则读 ~/.capsolver_key (MCP 客户端无需配环境变量)。"""
    k = os.environ.get("CAPSOLVER_API_KEY", "").strip()
    if k:
        return k
    try:
        return open(os.path.expanduser("~/.capsolver_key")).read().strip()
    except Exception:
        return ""


def _parse_nuxt(arr):
    """解析 Nuxt3 __NUXT_DATA__ payload (devalue 扁平引用数组) → 原生对象。

    格式: 顶层 arr[0]=["ShallowReactive",1] 标记根; arr[1] 是根对象, 其字段值
    若是 int 则是对数组其他位置的引用; 列表元素同理。字符串/数字/bool 内联。
    """
    seen = {}

    def decode(idx):
        if idx in seen:
            return seen[idx]
        v = arr[idx]
        if isinstance(v, list):
            if len(v) == 2 and v[0] in ("ShallowReactive", "Reactive",
                                        "ShallowRef", "Ref"):
                seen[idx] = None
                seen[idx] = decode(v[1])
                return seen[idx]
            out = []
            seen[idx] = out
            for el in v:
                out.append(decode(el) if isinstance(el, int)
                           and 0 <= el < len(arr) else el)
            return out
        if isinstance(v, dict):
            out = {}
            seen[idx] = out
            for k, vv in v.items():
                out[k] = (decode(vv) if isinstance(vv, int)
                          and 0 <= vv < len(arr) and vv != idx else vv)
            return out
        seen[idx] = v
        return v

    try:
        return decode(1)
    except Exception:
        return {}


def _map_buyer_type(supplier_types) -> str:
    """europages supplierTypes (list) → 单一 buyer_type。优先真实买家角色。"""
    if not supplier_types:
        return ""
    types = supplier_types if isinstance(supplier_types, list) else [supplier_types]
    mapped = [_SUPPLIER_TYPE_MAP.get(str(t), str(t)) for t in types]
    for role in ("distributor", "wholesaler", "importer", "manufacturer"):
        if role in mapped:
            return role
    return mapped[0] if mapped else ""


def _extract_companies(html: str) -> list:
    """从搜索页 HTML 提取 __NUXT_DATA__ payload → companies 列表。"""
    m = re.search(
        r'<script[^>]*id=["\']__NUXT_DATA__["\'][^>]*>(.*?)</script>',
        html, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(1))
    except Exception:
        return []
    root = _parse_nuxt(arr)
    return (root.get("state", {})
            .get("$ssearchCompaniesResponse", {})
            .get("companies", []) or [])


def get_waf_cookies(timeout_sec: int = 120) -> dict:
    """chromium 抓 API 405 puzzle 的 gokuProps → capsolver Situation 2 解题
    → 有效 aws-waf-token。返回 {"aws-waf-token": <token>}。

    Situation 2 必须 (实测): chromium 先抓 puzzle 参数喂 capsolver, 不能只传
    websiteURL (capsolver 对 europages 直接 200 不 challenge)。puzzle 参数实时
    会过期, chromium 抓 → capsolver 解 在同一流程内完成。
    """
    from playwright.sync_api import sync_playwright
    import requests

    api_key = _capsolver_key()
    if not api_key:
        raise RuntimeError(
            "缺 capsolver key (设环境变量 CAPSOLVER_API_KEY 或写 ~/.capsolver_key)"
        )

    proxies = _proxies()
    proxy_url = _gateway_proxy() or None

    # Step1: chromium silent 过主页 + 调 API 抓 405 puzzle gokuProps
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
            proxy={"server": proxy_url} if proxy_url else None)
        context = browser.new_context(
            user_agent=_UA, viewport={"width": 1366, "height": 900})
        page = context.new_page()
        for _ in range(6):
            try:
                resp = page.goto(_HOME, wait_until="domcontentloaded",
                                 timeout=25000)
                if resp and resp.status < 500:
                    break
            except Exception:
                time.sleep(2)
        for _ in range(15):
            if any(c['name'] == 'aws-waf-token' for c in context.cookies()):
                break
            try:
                page.wait_for_timeout(1000)
            except Exception:
                break
        r = context.request.get(_SEARCH_API,
            params={"query": "x", "country": "DE", "lang": "en"},
            headers={"Referer": _HOME, "Accept": "application/json"},
            timeout=40000)
        body = r.text()
        browser.close()

    # Step2: 解析 puzzle gokuProps + challenge.js
    gm = re.search(r'gokuProps\s*=\s*(\{)', body)
    if not gm:
        raise RuntimeError(f"没抓到 puzzle gokuProps (API status={r.status})")
    goku, _ = json.JSONDecoder().raw_decode(body[gm.end()-1:])
    if not goku.get("key"):
        raise RuntimeError(f"gokuProps 无 key: {list(goku.keys())}")
    cm = re.search(r'(https?://[^\s"\'<>]*?challenge\.js[^\s"\'<>]*)', body)
    chal_js = cm.group(1) if cm else ""

    # Step3: capsolver Situation 2 (puzzle 参数实时, 立即解)
    payload = {"clientKey": api_key, "task": {
        "type": "AntiAwsWafTaskProxyLess", "websiteURL": _HOME,
        "awsKey": goku["key"], "awsIv": goku.get("iv", ""),
        "awsContext": goku.get("context", ""), "awsChallengeJS": chal_js,
    }}
    rj = requests.post(_CAPSOLVER_CREATE, json=payload,
                       proxies=proxies, timeout=30).json()
    if rj.get("errorId") or not rj.get("taskId"):
        raise RuntimeError(
            f"capsolver createTask 失败: {rj.get('errorCode')} - {rj.get('errorDescription')}")
    task_id = rj["taskId"]

    deadline = time.time() + timeout_sec
    last = None
    while time.time() < deadline:
        time.sleep(2)
        last = requests.post(_CAPSOLVER_RESULT,
            json={"clientKey": api_key, "taskId": task_id},
            proxies=proxies, timeout=30).json()
        if last.get("status") == "ready":
            cookie = (last.get("solution") or {}).get("cookie")
            if cookie:
                return {"aws-waf-token": cookie}
            raise RuntimeError(f"capsolver ready 但无 cookie: {last}")
        if last.get("errorId") or last.get("status") == "failed":
            raise RuntimeError(
                f"capsolver 解题失败: {last.get('errorCode')} - {last.get('errorDescription')}")
    raise RuntimeError(f"capsolver 轮询超时 {timeout_sec}s, last={last}")


def search(query: str, country: str = "", max_results: int = 20) -> list:
    """过 WAF 拿 token → GET 搜索页 → 解析 __NUXT_DATA__ companies → LeadCandidate。

    country: ISO 2 字母 (DE/FR/GB...). europages 的 ?country 参数不生效 (返回全球),
    所以解析后按 countryCode 过滤。为凑够德国公司, 自动翻页 (最多 5 页, token 复用)。
    """
    if not ENABLED:
        return []

    import requests

    cookies = get_waf_cookies()
    slug = re.sub(r'[^a-z0-9]+', '-', (query or "").lower()).strip('-') or "led"
    # 用户 country(英文名/中文/ISO2) → ISO2; europages 返回的 countryCode 也是 ISO2
    cc_filter = (_iso_for(country) or "").upper()

    headers = {"User-Agent": _UA, "Referer": _HOME,
               "Accept": "text/html,application/xhtml+xml"}
    out = []
    seen = set()

    for page in range(1, 6):
        params = {"country": cc_filter} if cc_filter else {}
        if page > 1:
            params["page"] = str(page)
        url = f"{_HOME}companies/{slug}.html"
        resp = requests.get(url, params=params, headers=headers,
                            cookies=cookies, proxies=_proxies(), timeout=60)
        html = resp.text
        if resp.status_code != 200 or "Human Verification" in html \
                or "captcha-container" in html:
            break

        companies = _extract_companies(html)
        if not companies:
            break

        added = 0
        for c in companies:
            name = (c.get("name") or "").strip()
            if not name:
                continue
            key = name.lower()
            if key in seen:
                continue
            if cc_filter and (c.get("countryCode") or "").upper() != cc_filter:
                continue
            seen.add(key)
            homepage = (c.get("homepage") or "").strip()
            phone = (c.get("phoneNumber") or "").strip()
            buyer_type = _map_buyer_type(c.get("supplierTypes"))

            lead_id = "ep_" + hashlib.md5(
                f"{name}{homepage}".encode()).hexdigest()[:10]

            score = 72.0
            if buyer_type in ("importer", "distributor", "wholesaler"):
                score += 12
            elif buyer_type == "manufacturer":
                score += 4
            if homepage:
                score += 6
            if phone:
                score += 4

            out.append(LeadCandidate(
                company_name=name[:160],
                website=homepage,
                phone=phone,
                country=(_en_for_iso((c.get("countryCode") or cc_filter or "")) or country),
                city=(c.get("city") or ""),
                source="europages",
                search_query=query,
                score=min(score, 100),
                buyer_type=buyer_type,
                detail=(c.get("description") or "")[:200],
            ))
            added += 1
            if len(out) >= max_results:
                return out
        if added == 0 and page > 1:
            break
    return out


if __name__ == "__main__":
    import sys
    t0 = time.time()
    try:
        cookies = get_waf_cookies(timeout_sec=120)
        print(f"✅ WAF token: {len(cookies.get('aws-waf-token',''))} 字符 "
              f"({time.time()-t0:.1f}s)")
    except Exception as e:
        print(f"❌ WAF 失败 ({time.time()-t0:.1f}s): {type(e).__name__}: {e}")
        sys.exit(1)

    q = sys.argv[1] if len(sys.argv) > 1 else "led"
    cc = sys.argv[2] if len(sys.argv) > 2 else "DE"
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 15
    t1 = time.time()
    leads = search(q, cc, n)
    print(f"\n=== search('{q}','{cc}',{n}) → {len(leads)} 条 ({time.time()-t1:.1f}s) ===")
    for lc in leads:
        print(f"  [{lc.buyer_type or '-':12}] {lc.company_name[:40]:42} "
              f"{lc.country} {lc.website[:40]}")

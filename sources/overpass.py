"""Overpass (OSM) 公司发现源 — 阶段0 号池竖切的首个发现 adapter。

为何 Overpass 是阶段0 旗舰:
- A 类公共端点: 免 key 免注册, 不怕封 IP (公共限流靠号池轮换端点化解)。
- 原生邮箱字段 (email / contact:email): 一举补「公司发现 + 邮箱」两短板。
- 经销商类目最准: OSM shop/office/craft tag 比搜索引擎黄页更结构化。
- 不依赖 Clash IP 质量 (A 类), 代理仅用于 WSL 出墙。

号池应用 (验证 KeyPool 在发现层):
- 4 个公共端点 = KeyPool 里 provider='overpass' 的成员 (免 key, endpoint_url 承载)。
- 首次运行 bootstrap: 若号池无 overpass 端点, 自动投喂 4 个默认公共端点。
- acquire 轮询; 单端点 429 → mark_failed → 自动切下一个; 全耗尽返回 []。

返回 LeadCandidate (复用 sources.base), 过滤有 email 或 website 的商户。
"""
import logging
from typing import Optional

from sources.base import LeadCandidate
from keypool import KeyPool, NoAvailableKey

logger = logging.getLogger("leadgen-overpass")

# 默认公共端点 (bootstrap 投喂; resell_ok=True: OSM 公开数据可转售)。
# 顺序 = 实测对中国直连友好度。号池轮换会自动跳过过载/拒绝的端点。
_DEFAULT_ENDPOINTS = [
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",  # 俄罗斯镜像, 对中国直连友好 (实测稳, 返回邮箱)
    "https://overpass.osm.ch/api/interpreter",                   # 瑞士镜像, 快 (area 数据略少)
    "https://overpass-api.de/api/interpreter",                   # 官方主站 (常过载 timeout)
    "https://overpass.private.coffee/api/interpreter",           # 社区镜像
    "https://overpass.kumi-systems.ch/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter",         # 对部分出口 IP 403
]

# 国家名 → ISO 3166-1 alpha-2 (OSM area 查询用)
_COUNTRY_ISO = {
    "德国": "DE", "美国": "US", "英国": "GB", "法国": "FR", "意大利": "IT",
    "西班牙": "ES", "荷兰": "NL", "比利时": "BE", "瑞士": "CH", "奥地利": "AT",
    "波兰": "PL", "瑞典": "SE", "丹麦": "DK", "芬兰": "FI", "葡萄牙": "PT",
    "日本": "JP", "韩国": "KR", "中国": "CN", "加拿大": "CA", "澳大利亚": "AU",
    "印度": "IN", "巴西": "BR", "俄罗斯": "RU", "墨西哥": "MX", "土耳其": "TR",
    "阿联酋": "AE", "越南": "VN", "泰国": "TH", "印尼": "ID", "马来西亚": "MY",
    "新加坡": "SG", "台湾": "TW",
    "germany": "DE", "usa": "US", "us": "US", "uk": "GB", "united kingdom": "GB",
    "france": "FR", "italy": "IT", "spain": "ES", "netherlands": "NL",
    "japan": "JP", "korea": "KR", "canada": "CA", "australia": "AU",
    "india": "IN", "brazil": "BR", "de": "DE", "gb": "GB", "fr": "FR",
}

# query 关键词 → OSM tag 过滤 (品类发现)
_CATEGORY_TAGS = [
    (["led", "light", "lamp", "灯", "照明", "灯具", "leuchten", "beleuchtung"],
     '["shop"~"lighting|electrical|electronics",i]'),
    (["electronic", "电子", "elektronik"],
     '["shop"~"electronics|electrical",i]'),
    (["wholesale", "distributor", "批发", "经销商", "grosshandel"],
     '["office"="wholesale"]'),
    (["electric", "electrical", "电工", "电气", "elektriker"],
     '["craft"~"electrician",i]'),
]
_FALLBACK_TAGS = '["shop"]'


def _resolve_country(country: str) -> Optional[str]:
    if not country:
        return None
    c = country.strip().lower()
    if c in _COUNTRY_ISO:
        return _COUNTRY_ISO[c]
    if len(country.strip()) == 2 and country.strip().isalpha():
        return country.strip().upper()
    return None


def _build_ql(query: str, country_iso: Optional[str], max_results: int) -> str:
    """构造 Overpass QL。聚焦商业 POI。"""
    q = (query or "").lower()
    tag_expr = _FALLBACK_TAGS
    for keywords, expr in _CATEGORY_TAGS:
        if any(kw in q for kw in keywords):
            tag_expr = expr
            break
    if country_iso:
        area = f'area["ISO3166-1"="{country_iso}"]->.a;'
        filt = f"(nwr{tag_expr}(area.a););"
    else:
        filt = f"(nwr{tag_expr};);"
    return f"[out:json][timeout:60];{area}{filt}out center tags {max_results * 5};"


def _bootstrap_endpoints(pool: KeyPool):
    """首次运行: 号池无 overpass 端点则投喂默认公共端点。"""
    if pool.list_keys("overpass"):
        return
    for ep in _DEFAULT_ENDPOINTS:
        label = ep.split("//")[1].split("/")[0]
        pool.add("overpass", endpoint_url=ep, label=label,
                 resell_ok=True, quota_total=0)


_OSM_UA = "leadgen-keypool/0.1 (foreign-trade leadgen research; OSM Overpass; +https://osm.org/)"


def _post(endpoint: str, ql: str, proxy: Optional[str], timeout: int = 50):
    """POST 一次 Overpass。返回 (status, json_or_None)。429/504 → (429, None) 换端点。

    必须带描述性 User-Agent: Overpass/OSM 公共端点礼仪策略会拒绝默认 python-httpx UA (406/403)。
    """
    import httpx
    headers = {"User-Agent": _OSM_UA}
    with httpx.Client(timeout=timeout, proxy=proxy, headers=headers) as c:
        r = c.post(endpoint, data={"data": ql})
    if r.status_code in (429, 504):
        return 429, None
    if r.status_code != 200:
        return r.status_code, None
    try:
        return 200, r.json()
    except Exception:
        return 200, None


def _norm_website(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return "https://" + url


def _parse(data: dict, query: str, country: str, max_results: int) -> list:
    """Overpass elements → LeadCandidate。只收有 email 或 website 的商户。"""
    out = []
    seen = set()
    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        name = tags.get("name") or tags.get("name:en") or tags.get("brand") or ""
        if not name:
            continue
        email = (tags.get("email") or tags.get("contact:email") or "").strip()
        website = (tags.get("website") or tags.get("contact:website")
                   or tags.get("url") or "").strip()
        if not email and not website:
            continue  # overpass 价值在联系方式, 无则跳过
        key = name.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        phone = (tags.get("phone") or tags.get("contact:phone") or "").strip()
        city = tags.get("addr:city") or tags.get("addr:town") or ""

        score = 50.0
        if email:
            score += 25
        if website:
            score += 15
        if tags.get("shop") in ("lighting", "electrical", "electronics"):
            score += 10
        score = max(0.0, min(100.0, score))

        bt = ""
        if tags.get("office") == "wholesale" or tags.get("wholesale"):
            bt = "wholesaler"
        elif tags.get("shop"):
            bt = "distributor"

        out.append(LeadCandidate(
            company_name=name.strip(),
            website=_norm_website(website),
            country=country,
            city=city,
            email=email.lower(),
            phone=phone,
            buyer_type=bt,
            source="osm_overpass",
            search_query=query,
            score=score,
        ))
        if len(out) >= max_results:
            break
    return out


def search(query: str, country: str = "", max_results: int = 20) -> list:
    """Overpass 查商业 POI → LeadCandidate。走号池轮询公共端点, Clash 代理出墙。"""
    pool = KeyPool()
    _bootstrap_endpoints(pool)
    # Overpass 未被墙, 直连可达; 走 Clash 机场 IP 反被部分端点拒绝 (fr 403 / mail.ru 超时)。
    # A 类公共端点不需隐藏 IP, 直连最稳。代理能力保留在 ProxyPool, 此处不用。
    proxy = None

    iso = _resolve_country(country)
    ql = _build_ql(query, iso, max_results)

    n_keys = len(pool.list_keys("overpass"))
    data = None
    for _ in range(max(n_keys, 1)):
        try:
            ctx = pool.acquire("overpass")
        except NoAvailableKey:
            logger.warning("overpass: 所有公共端点已耗尽 (429), 本轮无果")
            break
        with ctx as k:
            try:
                status, j = _post(k.endpoint_url, ql, proxy)
            except Exception as e:
                logger.warning(f"overpass {k.endpoint_url} 网络异常: {e!r} → 换端点")
                continue  # __exit__ consume ok (网络抖动不罚端点)
            if status == 429:
                k.mark_failed("429", "rate limited")
                continue  # 标 exhausted, 换下一个
            if j is not None:
                data = j
                break
            # 非 200 非 429 (5xx 等): __exit__ consume ok, 换端点

    if data is None:
        return []
    return _parse(data, query, country, max_results)

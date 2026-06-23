"""档案源适配器: SerpApi google_maps (SerpApi Search API, engine=google_maps)。

为何 SerpApi google_maps 是经销商源补充:
- google_maps 字段碾压 gosom (phone/website/type/type_ids/place_id/gps/rating/reviews)
- type_ids 天然 buyer_type (wholesaler/manufacturer/store/dealer) 不需 LLM 判
- 2 key 免费档叠加产能 (号池轮换)
- 不需 playwright/chromium (相比 gosom 更轻)

号池应用:
- provider='serpapi' 成员 = 免费 key 列表; acquire 轮换最少使用优先
- 401/403 (key 失效) 或 429 (限流) → mark_failed → 自动切下一个 key
- quota_total=100 monthly (免费档); 每调必扣额度

⚠️ google_maps **每调必扣额度** → 必须加 SQLite 缓存层 (serpapi_query_cache 表):
  缓存键 = sha256(query+ll), 命中不调 API。缓存放 profile 级 keypool.db 同目录。

⚠️ google_maps **type 参数坑**: 带 type=search 退化单点查询 (只返1条该 place);
  发现源必须不带 type 走列表模式返 ~10 条 local_results。

鉴权: auth=api_key query param。endpoint:
  https://serpapi.com/search?engine=google_maps&q=...&ll=@lat,lng,Zz&api_key=KEY

返回 LeadCandidate (source='serpapi_maps'):
  - buyer_type 从 type_ids 映射 (不调 LLM)
  - email="" (google_maps 无邮箱, 下游 email_enrich 补)
  - score = rating*权重 + reviews 50-70 区间
"""
import hashlib
import logging
import os
import sqlite3
from typing import Optional

from .base import LeadCandidate
from keypool import KeyPool, NoAvailableKey, KEYPOOL_DB

logger = logging.getLogger("leadgen-serpapi-maps")

_ENDPOINT = "https://serpapi.com/search"

# 主要贸易城市 → (lat, lng, zoom) 给 ll=@lat,lng,Zz 参数
# zoom=15.5 实测适合城市级经销商搜索 (不过窄不过宽)
_CITY_COORDS = {
    # 德国
    "berlin": (52.5200, 13.4050, 15.5),
    "munich": (48.1351, 11.5820, 15.5),
    "münchen": (48.1351, 11.5820, 15.5),
    "hamburg": (53.5511, 9.9937, 15.5),
    "frankfurt": (50.1109, 8.6821, 15.5),
    "frankfurt am main": (50.1109, 8.6821, 15.5),
    "cologne": (50.9375, 6.9603, 15.5),
    "köln": (50.9375, 6.9603, 15.5),
    "stuttgart": (48.7758, 9.1829, 15.5),
    "düsseldorf": (51.2277, 6.7735, 15.5),
    "duesseldorf": (51.2277, 6.7735, 15.5),
    # 美国
    "new york": (40.7128, -74.0060, 15.5),
    "nyc": (40.7128, -74.0060, 15.5),
    "los angeles": (34.0522, -118.2437, 15.5),
    "la": (34.0522, -118.2437, 15.5),
    "chicago": (41.8781, -87.6298, 15.5),
    "houston": (29.7604, -95.3698, 15.5),
    # 英国
    "london": (51.5074, -0.1278, 15.5),
    # 法国
    "paris": (48.8566, 2.3522, 15.5),
    # 意大利
    "milan": (45.4642, 9.1900, 15.5),
    "milano": (45.4642, 9.1900, 15.5),
    "roma": (41.9028, 12.4964, 15.5),
    "rome": (41.9028, 12.4964, 15.5),
    # 其他
    "amsterdam": (52.3676, 4.9041, 15.5),
    "madrid": (40.4168, -3.7038, 15.5),
    "barcelona": (41.3851, 2.1734, 15.5),
    "vienna": (48.2082, 16.3738, 15.5),
    "wien": (48.2082, 16.3738, 15.5),
    "zurich": (47.3769, 8.5417, 15.5),
    "tokyo": (35.6762, 139.6503, 15.5),
    "osaka": (34.6937, 135.5023, 15.5),
    "seoul": (37.5665, 126.9780, 15.5),
    "shanghai": (31.2304, 121.4737, 15.5),
    "beijing": (39.9042, 116.4074, 15.5),
    "shenzhen": (22.5431, 114.0579, 15.5),
    "singapore": (1.3521, 103.8198, 15.5),
    "sydney": (-33.8688, 151.2093, 15.5),
    "melbourne": (-37.8136, 144.9631, 15.5),
    "toronto": (43.6532, -79.3832, 15.5),
    "dubai": (25.2048, 55.2708, 15.5),
}

# 国家级默认坐标 (城市未指定/未命中时用国家级中心)
_COUNTRY_COORDS = {
    "germany": (51.1657, 10.4515, 11.0),
    "deutschland": (51.1657, 10.4515, 11.0),
    "usa": (39.8283, -98.5795, 5.0),
    "united states": (39.8283, -98.5795, 5.0),
    "uk": (55.3781, -3.4360, 7.0),
    "united kingdom": (55.3781, -3.4360, 7.0),
    "france": (46.6035, 1.8883, 7.0),
    "italy": (41.8719, 12.5674, 7.0),
    "spain": (40.4637, -3.7492, 7.0),
    "netherlands": (52.1326, 5.2913, 8.0),
    "japan": (36.2048, 138.2529, 6.0),
    "china": (35.8617, 104.1954, 5.0),
}

# SerpApi type_ids → buyer_type 映射 (google_maps 的 type_ids 天然带角色)
# 实测 google_maps type_ids 常见值: Wholesaler/Manufacturer/Corporate office/
# Lighting manufacturer/Lighting wholesaler/Electronics store/...
_BUYER_TYPE_MAP = [
    # (关键词片段列表, buyer_type)  顺序=优先级 (wholesaler 最准, 先判)
    (("wholesaler", "wholesale", "grosshandel", "großhandel", "grossist"), "wholesaler"),
    (("manufacturer", "hersteller", "fabricant", "fabrikant", "maker"), "manufacturer"),
    (("store", "shop", "laden", "boutique", "retail"), "distributor"),
    (("dealer", "händler", "haendler", "distributor", "distributeur"), "distributor"),
    (("importer", "importeure", "importateur"), "importer"),
]


def _resolve_ll(city: str, country: str) -> str:
    """城市名(优先) / 国家名 → ll=@lat,lng,Zz 字符串。未命中用国家兜底，再不行不传ll。"""
    c = (city or "").strip().lower()
    if c and c in _CITY_COORDS:
        lat, lng, z = _CITY_COORDS[c]
        return f"@{lat},{lng},{z}z"
    cc = (country or "").strip().lower()
    if cc and cc in _COUNTRY_COORDS:
        lat, lng, z = _COUNTRY_COORDS[cc]
        return f"@{lat},{lng},{z}z"
    return ""  # 不传 ll 让 serpapi 用 q 文本地理


def _map_buyer_type(type_ids: list, type_str: str = "") -> str:
    """从 google_maps type_ids/type 字段映射 buyer_type。不调 LLM。

    type_ids 是 google 的类型标签数组 (如 ["lighting_wholesaler", "manufacturer"]);
    type 是自由文本 (如 "Lighting wholesaler"). 合并扫描关键词命中。
    返回 wholesaler/manufacturer/distributor/importer 或 "" (unknown 留空不调 LLM)。
    """
    blob = " ".join(str(t or "").lower() for t in (type_ids or []))
    if type_str:
        blob += " " + (type_str or "").lower()
    if not blob.strip():
        return ""
    for keywords, bt in _BUYER_TYPE_MAP:
        for kw in keywords:
            if kw in blob:
                return bt
    return ""


# ---------------------------------------------------------------------------
# SQLite 缓存层 (google_maps 每调必扣额度, 缓存是刚需)
# ---------------------------------------------------------------------------
_CACHE_DB = None  # 延迟求值 (KEYPOOL_DB 在 storage.db import 时定)


def _cache_db_path() -> str:
    """缓存放 profile 级 keypool.db 同目录的 serpapi_cache.db。"""
    global _CACHE_DB
    if _CACHE_DB is None:
        _CACHE_DB = str(KEYPOOL_DB.parent / "serpapi_cache.db")
    return _CACHE_DB


def _cache_key(query: str, ll: str, extra: str = "") -> str:
    raw = f"{(query or '').strip().lower()}|{(ll or '').strip().lower()}|{extra}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _cache_get(key: str) -> Optional[dict]:
    """命中返回缓存的 results JSON dict，未命中/出错返回 None。"""
    try:
        c = sqlite3.connect(_cache_db_path())
        try:
            c.execute(
                "CREATE TABLE IF NOT EXISTS serpapi_query_cache ("
                "  cache_key TEXT PRIMARY KEY,"
                "  query TEXT, ll TEXT,"
                "  result_json TEXT,"
                "  created_at TEXT DEFAULT (datetime('now'))"
                ")"
            )
            row = c.execute(
                "SELECT result_json FROM serpapi_query_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
        finally:
            c.close()
    except Exception as e:
        logger.debug("serpapi cache get 失败(降级无缓存): %r", e)
        return None
    if not row:
        return None
    try:
        import json
        return json.loads(row[0])
    except Exception:
        return None


def _cache_put(key: str, query: str, ll: str, data: dict) -> None:
    """写入缓存 (INSERT OR REPLACE)。任何异常静默忽略。"""
    try:
        import json
        c = sqlite3.connect(_cache_db_path())
        try:
            c.execute(
                "CREATE TABLE IF NOT EXISTS serpapi_query_cache ("
                "  cache_key TEXT PRIMARY KEY,"
                "  query TEXT, ll TEXT,"
                "  result_json TEXT,"
                "  created_at TEXT DEFAULT (datetime('now'))"
                ")"
            )
            c.execute(
                "INSERT OR REPLACE INTO serpapi_query_cache "
                "(cache_key, query, ll, result_json) VALUES (?, ?, ?, ?)",
                (key, query, ll, json.dumps(data, ensure_ascii=False)),
            )
            c.commit()
        finally:
            c.close()
    except Exception as e:
        logger.debug("serpapi cache put 失败(降级无缓存): %r", e)


def _bootstrap_keys(pool: KeyPool):
    """号池无 serpapi key → 从 env SERPAPI_API_KEYS (逗号分隔) 投喂。库非空则跳过。"""
    if pool.list_keys("serpapi"):
        return
    keys = [k.strip() for k in os.environ.get("SERPAPI_API_KEYS", "").split(",") if k.strip()]
    for i, k in enumerate(keys):
        pool.add("serpapi", api_key=k, label=f"serpapi-free-{i+1}",
                 quota_total=100, quota_window="monthly", resell_ok=False)


def _read_proxy() -> Optional[str]:
    """照抄 hunter_discover: 读 https_proxy/HTTPS_PROXY/http_proxy env。"""
    for k in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        v = os.environ.get(k, "").strip()
        if v:
            return v
    return None


def _call(api_key: str, query: str, ll: str, max_results: int):
    """调 SerpApi /search engine=google_maps。返回 (status, json_or_None)。

    google_maps 列表模式: 不带 type 参数 (带 type=search 退化单点查询)。
    params: engine=google_maps / q / ll=@lat,lng,Zz / api_key
    """
    import httpx
    params = {
        "engine": "google_maps",
        "q": query,
        "api_key": api_key,
    }
    if ll:
        params["ll"] = ll
    proxy = _read_proxy()
    try:
        with httpx.Client(timeout=40, proxy=proxy) as c:
            r = c.get(_ENDPOINT, params=params)
    except Exception as e:
        logger.warning("serpapi_maps 网络异常: %r", e)
        return 0, None
    if r.status_code != 200:
        return r.status_code, None
    try:
        return 200, r.json()
    except Exception:
        return 200, None


def _norm_website(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return "https://" + url


def _extract_city_from_address(address: str) -> str:
    """从 address 末尾段尽力提取 city (google_maps address 格式常含邮编+城市)。"""
    if not address:
        return ""
    # 格式如 "Hauptstraße 1, 10115 Berlin, Germany" → 取倒数第二段
    parts = [p.strip() for p in address.split(",") if p.strip()]
    if len(parts) >= 2:
        # 末段是国家，倒数第二段是 "邮编 城市"
        candidate = parts[-2]
        # 去邮编
        import re
        m = re.match(r"^\s*\d+\s*(.+)$", candidate)
        if m:
            return m.group(1).strip()
        return candidate
    return ""


def _parse(data: dict, query: str, country: str, city: str, max_results: int) -> list:
    """google_maps local_results → LeadCandidate。

    每个 local_result 字段: title/phone/website/type/type_ids/place_id/gps/
    rating/reviews/address/country. 无邮箱。
    """
    local_results = data.get("local_results") or []
    # 有些响应把结果塞在 places 数组里
    if not local_results and isinstance(data.get("places"), list):
        local_results = data["places"]
    # 降级: q 不含地理词 + 只传 ll 时 google_maps 返 place_results (单点详情模式)
    if not local_results and isinstance(data.get("place_results"), dict):
        local_results = [data["place_results"]]

    out = []
    seen = set()
    for item in local_results:
        name = (item.get("title") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)

        phone = (item.get("phone") or "").strip()
        website = _norm_website(item.get("website") or "")
        type_ids = item.get("type_ids") or []
        type_str = (item.get("type") or "").strip()
        rating = item.get("rating")
        reviews = item.get("reviews") or 0
        address = (item.get("address") or "").strip()
        item_country = (item.get("country") or country or "").strip()

        # buyer_type 从 type_ids/type 映射 (不调 LLM)
        bt = _map_buyer_type(type_ids, type_str)

        # score = rating*权重 + reviews, 50-70 区间
        score = 50.0
        try:
            if rating:
                score += min(float(rating) * 2.0, 10.0)  # rating 5.0→+10
            if reviews:
                score += min(float(reviews) / 100.0, 10.0)  # 1000评论→+10
        except (TypeError, ValueError):
            pass
        if bt:
            score += 5.0  # 有明确 buyer_type 加分
        score = max(0.0, min(100.0, score))

        detail_parts = []
        if type_str:
            detail_parts.append(type_str)
        if rating:
            detail_parts.append(f"⭐{rating}")
        if reviews:
            detail_parts.append(f"({reviews}评)")

        out.append(LeadCandidate(
            company_name=name,
            website=website,
            country=item_country,
            city=city or _extract_city_from_address(address),
            email="",  # google_maps 无邮箱
            phone=phone,
            buyer_type=bt,
            source="serpapi_maps",
            search_query=query,
            score=score,
            detail=" ".join(detail_parts)[:120],
        ))
        if len(out) >= max_results:
            break
    return out


def search_maps(query: str, country: str = "", city: str = "", max_results: int = 10) -> list:
    """SerpApi google_maps 列表查询 → LeadCandidate。走号池轮换 key + SQLite 缓存。

    流程:
      1) 算 cache_key, 命中缓存直接 _parse 返回 (不调 API)
      2) 未命中 → 号池 acquire serpapi key 调 API
      3) 401/403/429 → mark_failed 换 key; 全耗尽返回 []
      4) 成功 → _cache_put 存缓存 → _parse 返回

    不带 type 参数 (列表模式返 ~10 条); ll=@lat,lng,Zz 从 city/country 映射。
    """
    ll = _resolve_ll(city, country)
    # ⚠️ q 必须自带地理词 (city/country), 否则只传 ll 不带地理 q 时
    # google_maps 会触发 place_results 单点模式而非 local_results 列表模式 (实测)
    extra_geo = " ".join(x for x in (city, country) if x).strip()
    full_q = f"{query} {extra_geo}".strip() if extra_geo else query
    ck = _cache_key(full_q, ll)

    # 1) 缓存命中?
    cached = _cache_get(ck)
    if cached is not None:
        logger.info("serpapi_maps 缓存命中 (query=%r ll=%r)", full_q, ll)
        return _parse(cached, query, country, city, max_results)

    # 2) 号池调 API
    pool = KeyPool()
    _bootstrap_keys(pool)

    n_keys = len(pool.list_keys("serpapi"))
    if n_keys == 0:
        logger.warning("serpapi_maps: 未投喂 key (库空且 SERPAPI_API_KEYS 未配), 不可用")
        return []

    data = None
    for _ in range(max(n_keys, 1)):
        try:
            ctx = pool.acquire("serpapi")
        except NoAvailableKey:
            logger.warning("serpapi_maps: 所有 key 已耗尽/失效")
            break
        with ctx as k:
            status, j = _call(k.api_key, full_q, ll, max_results)
            if status in (401, 403):
                k.mark_failed(str(status), "key invalid/forbidden")
                continue
            if status == 429:
                k.mark_failed("429", "rate limited")
                continue
            if j is not None:
                data = j
                break
            # 其他错误 (5xx/0 网络异常): __exit__ consume ok, 换 key

    if data is None:
        return []

    # 3) 存缓存
    _cache_put(ck, full_q, ll, data)

    return _parse(data, query, country, city, max_results)

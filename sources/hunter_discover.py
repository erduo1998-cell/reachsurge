"""档案源适配器: Hunter Discover (api.hunter.io/v2/discover)。

补 europages/gosom 抓不到的"公司清单发现" —— Discover 是 Hunter 的找公司端点,
按 headquarters_location(国家) + keywords 捞出在该地区做该业务的公司, 一次最多 100 家。
跟 hunter.py domain-search(已知域找邮箱) 是两回事:
  - domain-search (enrichment_providers/providers/hunter.py): 已知 domain → 找邮箱
  - discover      (本模块):                       已知国家+关键词 → 找公司清单(无邮箱)

结果每家仅 3 字段 (domain/organization/emails_count), 无 industry/邮箱/电话:
  - buyer_type 恒 "unknown" (结果无 industry 字段)
  - email      恒 ""         (无邮箱, 靠后续 email_enrich 瀑布补)
  - country    取传入的 country 参数 (结果无国家字段)
所以 Discover 是"发现 + 喂给 LLM 护城河过滤"的入口, 邮箱/画像交给下游补。

配额: Discover 走免费档, **不消耗 domain-search 的 50 credits/月**
(实测 3 次 POST, account searches/credits 零变化)。适合做主要发现源。
keywords 走 _extract_product_keywords (DeepSeek 提取英文产品词) + match=all
(实测 led lighting=100条全照明 90%+; 旧 match=any 混入医院/大学=36%)。
LLM 失败降级规则提取。仍要 enrich_company_profile 护城河过滤, 不裸入库 (铁律)。

鉴权/代理/httpx 照抄 enrichment_providers/providers/hunter.py 范式:
  - api_key 走 os.environ HUNTER_API_KEY
  - proxy 走 https_proxy/HTTPS_PROXY/http_proxy/HTTP_PROXY env
  - httpx.Client(timeout=, proxy=)
  - 非 200 + 401/403/429 → 记 warning, 返回 [] (本轮禁用, 不中断 orchestrate)

⚠️ Free plan **不能传 limit/offset**, 否则回 pagination_error。固定每次最多 100 家,
内存 out[:max_results] 截断。
"""
import logging
import os
import re
from typing import Optional

from .base import LeadCandidate

log = logging.getLogger(__name__)

_DISCOVER = "https://api.hunter.io/v2/discover"

# 常用国家 (中英文/常见写法) → ISO alpha-2。Discover 的 headquarters_location 只吃 ISO2。
_COUNTRY_ISO = {
    # 英文
    "united states": "US", "usa": "US", "us": "US", "america": "US",
    "united kingdom": "GB", "uk": "GB", "britain": "GB", "england": "GB",
    "germany": "DE", "france": "FR", "italy": "IT", "spain": "ES",
    "netherlands": "NL", "holland": "NL", "belgium": "BE",
    "switzerland": "CH", "austria": "AT", "sweden": "SE", "norway": "NO",
    "denmark": "DK", "finland": "FI", "poland": "PL", "portugal": "PT",
    "ireland": "IE", "czech republic": "CZ", "czechia": "CZ",
    "japan": "JP", "south korea": "KR", "korea": "KR",
    "china": "CN", "taiwan": "TW", "hong kong": "HK", "singapore": "SG",
    "india": "IN", "indonesia": "ID", "malaysia": "MY", "thailand": "TH",
    "vietnam": "VN", "philippines": "PH",
    "canada": "CA", "mx": "MX", "mexico": "MX", "brazil": "BR", "argentina": "AR",
    "australia": "AU", "new zealand": "NZ",
    "uae": "AE", "united arab emirates": "AE", "saudi arabia": "SA",
    "israel": "IL", "turkey": "TR", "egypt": "EG", "south africa": "ZA",
    "russia": "RU",
    # 中文
    "美国": "US", "英国": "GB", "德国": "DE", "法国": "FR", "意大利": "IT",
    "西班牙": "ES", "荷兰": "NL", "比利时": "BE", "瑞士": "CH", "奥地利": "AT",
    "瑞典": "SE", "挪威": "NO", "丹麦": "DK", "芬兰": "FI", "波兰": "PL",
    "葡萄牙": "PT", "爱尔兰": "IE", "捷克": "CZ",
    "日本": "JP", "韩国": "KR", "中国": "CN", "台湾": "TW", "香港": "HK",
    "新加坡": "SG", "印度": "IN", "印尼": "ID", "马来西亚": "MY", "泰国": "TH",
    "越南": "VN", "菲律宾": "PH",
    "加拿大": "CA", "墨西哥": "MX", "巴西": "BR", "阿根廷": "AR",
    "澳大利亚": "AU", "澳洲": "AU", "新西兰": "NZ",
    "阿联酋": "AE", "沙特": "SA", "沙特阿拉伯": "SA", "以色列": "IL",
    "土耳其": "TR", "埃及": "EG", "南非": "ZA", "俄罗斯": "RU",
}


def _to_iso2(country: str) -> str:
    """国家名(中/英/ISO2) → ISO alpha-2 大写。无法识别返回 "" (不传国家过滤)。"""
    c = (country or "").strip()
    if not c:
        return ""
    if len(c) == 2 and c.isalpha():
        return c.upper()
    return _COUNTRY_ISO.get(c.lower(), "")


def _read_proxy() -> Optional[str]:
    """照抄 hunter.py: 读 https_proxy/HTTPS_PROXY/http_proxy/HTTP_PROXY env。"""
    for k in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        v = os.environ.get(k, "").strip()
        if v:
            return v
    return None


_PROMPT_EXTRACT = """从下面的搜索查询里,提取出 1 到 2 个最适合用于英文 B2B 公司库搜索的英文产品/行业核心词(描述公司卖什么产品的词,不要场景词/地理词/买家角色词)。
- 只输出英文小写词,逗号分隔,最多2个
- 优先通用产品行业词(如 led, lighting, electronics, furniture, textile),避免太窄的型号词
- 输入是当地语言(中文/德文等)时,翻译成对应英文产品行业词
- 不要输出任何解释

查询: {q}
英文产品词:"""


def _extract_product_keywords(query: str) -> list:
    """调 DeepSeek 从 query 提取 1-2 个英文产品行业核心词, 喂 Hunter match=all。

    match=all 只在 keywords=产品词时精准 (实测 led lighting=100条全照明 90%+)。
    自然语言 query 混合语言+场景词, 规则提不出产品词 (前2误中场景词/全词返0),
    故用 LLM。thinking 模型 (deepseek-v4-flash) 先 reasoning (~100-300 token) 再
    输出答案, max_tokens 必须 >=800 否则 reasoning 吃光 token 致 content 空 (实测)。
    复用 company_intel 同款 env (DEEPSEEK_API_KEY/BASE_URL/MODEL) + _read_proxy 代理。
    返回小写英文词 list (<=2)。任何异常返回 [] (降级走 _fallback_keywords)。
    """
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key or not (query or "").strip():
        return []
    base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip().rstrip("/")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash").strip()
    body = {
        "model": model,
        "messages": [{"role": "user", "content": _PROMPT_EXTRACT.format(q=query)}],
        "temperature": 0.0,
        "max_tokens": 800,
    }
    try:
        import httpx
        with httpx.Client(timeout=30, proxy=_read_proxy()) as c:
            r = c.post(f"{base}/chat/completions",
                       headers={"Authorization": f"Bearer {key}",
                                "Content-Type": "application/json"},
                       json=body)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"] or ""
    except Exception as e:
        log.warning("hunter_discover LLM 提取产品词失败, 降级规则: %s: %s", type(e).__name__, e)
        return []
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9-]{1,30}", content)
    return [w.lower() for w in words if w.isascii()][:2]


def _fallback_keywords(query: str) -> list:
    """LLM 提取失败时降级: 取 query 里的 ASCII 英文词前2 (尽力而为)。
    场景词 query 可能跑偏, 但下游 LLM 护城河兜底, 不返纯垃圾。"""
    raw = [w for w in (query or "").replace(",", " ").replace('"', " ").split() if w]
    return [w.lower() for w in raw if w.isascii() and w.isalnum()][:2]


def search(query: str, country: str = "", max_results: int = 20) -> list:
    """调 Hunter Discover 捞公司清单 → LeadCandidate。

    POST https://api.hunter.io/v2/discover:
      - api_key 走 query param
      - 过滤走 JSON body:
          {"headquarters_location": {"include": [{"country": "DE"}]},
           "keywords":             {"include": ["led","lighting"], "match": "all"}}
        (keywords 由 _extract_product_keywords 从 query 提取英文产品词; match=all 只对产品词精准)
      - 绝不传 limit/offset (Free plan 回 pagination_error)
      - 响应 data[] 每家仅 domain/organization/emails_count{personal,generic,total}

    返回 list[LeadCandidate]:
      - buyer_type="unknown" (无 industry)
      - email="" (无邮箱, 下游 email_enrich 补)
      - country 取传入的 country (结果无国家)
      - score = 60 + min(emails_count.total // 1000, 20)  保证 ≥50
      - detail 存 "emails: personal=X,generic=Y,total=Z"

    异常/401/403/429 → 返回 [] (本轮禁用, 不中断 orchestrate)。
    """
    api_key = os.environ.get("HUNTER_API_KEY", "").strip()
    if not api_key:
        return []

    cc = _to_iso2(country)
    keywords = _extract_product_keywords(query)
    if not keywords:
        keywords = _fallback_keywords(query)  # LLM 失败降级: 英文词前2
    if not keywords:
        log.info("hunter_discover: query 无可提取产品词, 本轮跳过 (gosom 接)")
        return []

    body = {"keywords": {"include": keywords, "match": "all"}}
    if cc:
        body["headquarters_location"] = {"include": [{"country": cc}]}

    try:
        import httpx
        with httpx.Client(timeout=30, proxy=_read_proxy()) as c:
            r = c.post(_DISCOVER, params={"api_key": api_key}, json=body)
    except Exception as e:
        log.warning("hunter_discover 网络异常: %s: %s", type(e).__name__, e)
        return []

    if r.status_code != 200:
        log.warning("hunter_discover HTTP %d (本轮禁用): %s",
                    r.status_code, r.text[:200] if hasattr(r, "text") else "")
        return []

    try:
        data = r.json().get("data") or []
    except Exception as e:
        log.warning("hunter_discover 响应解析失败: %s: %s", type(e).__name__, e)
        return []

    out = []
    for item in data:
        domain = (item.get("domain") or "").strip()
        org = (item.get("organization") or "").strip()
        if not org and not domain:
            continue
        ec = item.get("emails_count") or {}
        personal = int(ec.get("personal") or 0)
        generic = int(ec.get("generic") or 0)
        total = int(ec.get("total") or 0)

        out.append(LeadCandidate(
            company_name=org[:160],
            website=domain,
            country=country,
            buyer_type="unknown",
            email="",
            source="hunter_discover",
            search_query=query,
            score=60.0 + min(total // 1000, 20),
            detail=f"emails: personal={personal},generic={generic},total={total}",
        ))
        if len(out) >= max_results:
            break
    return out

"""Tavily 搜索源 — 展会突破口 (discovery-line-design) 的抓取基建。

为何 Tavily 是展会源旗舰:
- include_domains 精准锁展会官网 (light-building.com 等), 不被 SEO 农场污染
- AI answer 字段已 LLM-ready, 实测直接从官网吐展商名 (Light+Building 1927 家 LED 展商)
- dev key 2000/月免费, A 类不怕封 IP (Tavily 在合规 resell_ok 准入名单)
- 多 key 叠加产能 (号池): 2 key = 4000/月

号池应用:
- provider='tavily' 成员 = dev key 列表; acquire 轮询, 最少使用优先
- 401/403 (key 失效) 或 429 (限流) → mark_failed → 自动切下一个
- quota_total=2000 monthly; window_reset_at 到期 KeyPool._reset_if_due 自动重置

返回 LeadCandidate (source='tavily_exhibition', buyer_type='exhibitor'),
公司名从 answer + results 用 DeepSeek LLM 提取 (失败/无 key 启发式降级)。
"""
import os
import re
import json
import logging
from typing import Optional

from sources.base import LeadCandidate
from keypool import KeyPool, NoAvailableKey

logger = logging.getLogger("leadgen-tavily")

_API = "https://api.tavily.com"

# 知名展会 → 官网域名 (include_domains 用)。query 含展会名时锁对应官网。
_FAIR_DOMAINS = [
    ("light+building", ["light-building.com"]),
    ("light building", ["light-building.com"]),
    ("light-building", ["light-building.com"]),
    ("frankfurt lighting", ["light-building.com"]),
    ("hong kong lighting", ["hklightingfair.com", "hktdc.com"]),
    ("香港灯", ["hklightingfair.com", "hktdc.com"]),
    ("guangzhou lighting", ["lighting.gymf.com.cn"]),
    ("广州照明", ["lighting.gymf.com.cn"]),
]

# query 里若直接出现域名, 优先用它
_DOMAIN_RE = re.compile(r'([a-z0-9-]+\.(?:com|de|cn|org|net|io)(?:\.[a-z]{2,3})?)', re.I)


def _bootstrap_keys(pool: KeyPool):
    """号池无 tavily key → 从 env TAVILY_API_KEYS (逗号分隔) 投喂。库非空则跳过。"""
    if pool.list_keys("tavily"):
        return
    keys = [k.strip() for k in os.environ.get("TAVILY_API_KEYS", "").split(",") if k.strip()]
    for i, k in enumerate(keys):
        pool.add("tavily", api_key=k, label=f"tavily-dev-{i+1}",
                 quota_total=2000, quota_window="monthly", resell_ok=True)


def _resolve_domains(query: str) -> list:
    """从 query 识别展会 → include_domains。无匹配返 [](全网搜)。"""
    q = (query or "").lower()
    for kw, domains in _FAIR_DOMAINS:
        if kw in q:
            return domains
    m = _DOMAIN_RE.search(query or "")
    if m:
        return [m.group(1).lower()]
    return []


# 中文/别名展会名 → 英文精准搜索词。
# 实测: 锁 include_domains 只拿官网主站、answer 泛泛(0 公司);
# 不锁域名 + 英文精准词, Tavily answer 直接点名十几个展商 (Bega/Zumtobel/Trilux...)。
_FAIR_QUERIES = {
    "light+building": "Light Building Frankfurt LED exhibitors",
    "light building": "Light Building Frankfurt LED exhibitors",
    "light-building": "Light Building Frankfurt LED exhibitors",
    "法兰克福灯": "Light Building Frankfurt LED exhibitors",
    "法兰克福led": "Light Building Frankfurt LED exhibitors",
    "frankfurt lighting": "Light Building Frankfurt LED exhibitors",
    "hong kong lighting": "Hong Kong International Lighting Fair exhibitors",
    "香港灯": "Hong Kong International Lighting Fair exhibitors",
    "guangzhou lighting": "Guangzhou International Lighting Exhibition exhibitors",
    "广州照明": "Guangzhou International Lighting Exhibition exhibitors",
}


def _build_search_query(query: str) -> str:
    """构建 Tavily 英文搜索词。命中展会映射→精准英文; 否则原 query+后缀。"""
    q = (query or "").lower()
    for kw, eng in _FAIR_QUERIES.items():
        if kw in q:
            return f"{eng} company names list"
    return f"{query} exhibitor list company names"


def _call(api_key: str, query: str, domains: list, max_results: int):
    """调 Tavily /search。返回 (status, json_or_None)。401/403/429 上层换 key。"""
    import httpx
    proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or None
    body = {
        "api_key": api_key,
        "query": query,
        "max_results": max_results,
        "search_depth": "advanced",
        "include_answer": True,
        "include_raw_content": False,
    }
    if domains:
        body["include_domains"] = domains
    try:
        with httpx.Client(timeout=40, proxy=proxy) as c:
            r = c.post(f"{_API}/search", json=body)
    except Exception as e:
        logger.warning("tavily 网络异常: %r", e)
        return 0, None
    if r.status_code != 200:
        return r.status_code, None
    try:
        return 200, r.json()
    except Exception:
        return 200, None


def _heuristic_extract(answer: str, results: list) -> list:
    """启发式降级: results title 清洗 + answer 'including X, Y' 正则提取。"""
    out = []
    seen = set()
    for r in results:
        t = (r.get("title") or "").strip()
        u = (r.get("url") or "").strip()
        if len(t) < 3:
            continue
        name = re.split(r'\s*[|｜\-–—:：]\s*', t)[0].strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append({"name": name, "website": u, "business": ""})
    if answer:
        m = re.search(r'(?:including|include|such as|notably|like)\s+(.+?)(?:\.|$)',
                      answer, re.I)
        if m:
            for part in re.split(r',|\sand\s', m.group(1)):
                p = part.strip().strip('.')
                if 2 < len(p) < 60 and p and p[0].isupper() and not p[0].isdigit():
                    if p.lower() not in seen:
                        seen.add(p.lower())
                        out.append({"name": p, "website": "", "business": ""})
    return out


def _llm_extract(answer: str, results: list, query: str) -> list:
    """DeepSeek 从 Tavily answer + results 提取展商公司列表。

    返回 [{name, website, business}]。LLM 失败/无 key → 启发式降级 (不阻断)。
    """
    chunks = []
    if answer:
        chunks.append(f"AI摘要: {answer[:600]}")
    for r in results[:8]:
        t = (r.get("title") or "").strip()
        u = (r.get("url") or "").strip()
        c = (r.get("content") or "").strip()[:300]
        if t:
            chunks.append(f"[{t}] {u}\n{c}")
    text = "\n".join(chunks)[:3000]
    if not text:
        return []

    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        return _heuristic_extract(answer, results)

    base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip().rstrip("/")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash").strip()
    proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or None
    prompt = f"""从下面的展会搜索内容里, 提取所有出现的**公司名**(展商/参展商/品牌方/厂商), 输出 JSON。

要求:
- 只提取真实公司名, 跳过展会主办方/场馆/日期/数字统计/无关词
- 每个 company 给 website(若内容里有 URL 推断归属, 否则空) 和 business(主营业务, 若能判断)
- 没有公司则返回空数组
- 不要 markdown 围栏, 直接 JSON

输出格式: {{"companies":[{{"name":"...","website":"...","business":"..."}}]}}

内容:
{text}"""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
        "max_tokens": 1200,
    }
    try:
        import httpx
        with httpx.Client(timeout=45, proxy=proxy) as c:
            r = c.post(f"{base}/chat/completions",
                       headers={"Authorization": f"Bearer {key}",
                                "Content-Type": "application/json"},
                       json=body)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"] or ""
    except Exception as e:
        logger.warning("tavily LLM 提取失败, 启发式降级: %r", e)
        return _heuristic_extract(answer, results)

    s = content.strip()
    if s.startswith("```"):
        s = s.lstrip("`").lstrip("json").lstrip().rstrip("`").strip()
    try:
        obj = json.loads(s)
        return obj.get("companies", []) or []
    except Exception:
        logger.warning("tavily LLM 提取解析失败, 启发式降级. 原始: %s", content[:200])
        return _heuristic_extract(answer, results)


def search_exhibition(query: str, max_results: int = 15) -> list:
    """展会源入口: Tavily 锁展会官网搜展商 → LLM 提取公司 → LeadCandidate。

    query 应含展会名 (如 'Light+Building 法兰克福LED展') 或品类+展会词。
    """
    pool = KeyPool()
    _bootstrap_keys(pool)
    # 不锁 include_domains: 锁官网只拿主站 answer 泛泛; 全网搜英文精准词 answer 直呼展商名
    domains = []
    sq = _build_search_query(query)

    n_keys = len(pool.list_keys("tavily"))
    if n_keys == 0:
        logger.warning("tavily: 未投喂 key (库空且 TAVILY_API_KEYS 未配), 展会源不可用")
        return []

    data = None
    for _ in range(max(n_keys, 1)):
        try:
            ctx = pool.acquire("tavily")
        except NoAvailableKey:
            logger.warning("tavily: 所有 key 已耗尽/失效")
            break
        with ctx as k:
            status, j = _call(k.api_key, sq, domains, max_results)
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

    answer = data.get("answer", "") or ""
    results = data.get("results", []) or []
    companies = _llm_extract(answer, results, query)

    out = []
    seen = set()
    for c in companies:
        name = (c.get("name") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(LeadCandidate(
            company_name=name,
            website=(c.get("website") or "").strip(),
            search_query=query,
            source="tavily_exhibition",
            buyer_type="exhibitor",
            score=58.0,
            detail=(c.get("business") or "")[:120],
        ))
        if len(out) >= max_results:
            break
    return out

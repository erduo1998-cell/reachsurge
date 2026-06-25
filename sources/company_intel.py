"""公司情报工具: 把一行公司名增密成"公司画像 + 合作可能性判断"。

解决的问题: gosom/europages 等源给的线索只有公司名/地址/电话, 信息没密度。
本工具深抓公司官网 (homepage/about/products/team 等, 智能链接发现不写死路径),
喂 DeepSeek 产出结构化情报:
  公司类型 / 业务范围 / 规模信号 / 合作可能性(从用户产品视角)+信号分级
  / 关键洞察 / 信息置信度

定位: 独立调研工具 (同 importyeti_lookup / social_profile_lookup), 不进
SOURCE_REGISTRY, 不做 discovery。agent 在 gosom 线索信息不够时调用本工具补深度信息,
结果 update 到 leads 表 (signal_level + company_intel 两列)。

v1 边界: 只抓官网做判断, 不联网搜索 (环境暂无搜索引擎 API: SerpAPI 未配, web_search
不可用)。官网是公司信息密度最高的源, 足够产出画像+合作可能性。
"中国采购痕迹"维度由 importyeti_lookup 覆盖, 本工具互补, 不重复。
联网补充留 v2 (prompt 已预留 web_context 注入位)。

DeepSeek 调用走 OpenAI 兼容 API:
  POST {DEEPSEEK_BASE_URL}/chat/completions, model=DEEPSEEK_MODEL, response_format=json_object。
  httpx 默认 trust_env, 自动走 http_proxy/https_proxy (Clash 7897)。
"""
import os
import re
import json
from urllib.parse import urlparse, urljoin

import httpx
import sys

# 直接跑本文件时确保 storage.* 可达 (gateway 内由 mcp_server 已 insert 项目根)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── DeepSeek 配置 (从 env 读, mcp_server.py 启动时 load_dotenv 已注入) ──
_DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
_DEEPSEEK_BASE = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip().rstrip("/")
_DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash").strip()

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_FETCH_TIMEOUT = 8.0          # 单页抓取超时
_MAX_PAGES = 6                # 最多抓多少个内页(不含首页)
_MAX_CHARS_PER_PAGE = 2500    # 单页正文裁剪上限
_MAX_TOTAL_CHARS = 7000       # 喂给 LLM 的总字符上限(控成本+聚焦)
_DEEPSEEK_TIMEOUT = 60.0

# 情报类内链关键词 (多语言, 用于从首页 href 发现 about/products/team 等页面)
_INTEL_LINK_KEYWORDS = (
    'about', 'about-us', 'who-we-are', 'company', 'unternehmen', 'ueber-uns',
    'ueber', 'über', 'profile', 'corporate', 'history', 'our-story',
    'product', 'produkt', 'produkte', 'portfolio', 'solution', 'solutions',
    'service', 'services', 'leistung', 'leistungen', 'catalog', 'catalogue',
    'team', 'management', 'staff', 'karrier', 'career',
    'manufactur', 'fabrik', 'werk', 'factory', 'quality', 'technolog',
    'reference', 'references', 'project', 'projekt', 'case-stud',
    'news', 'blog', 'partner', 'distribution', 'distributor',
)
# 首页之外的种子候选路径 (链接发现没覆盖时的补充)
_SEED_PATHS = (
    '/about', '/about-us', '/company', '/unternehmen', '/products',
    '/produkte', '/portfolio', '/solutions', '/team', '/impressum',
)
# 正文提取时丢弃的 HTML 标签 (噪声)
_BAD_TAGS = ('script', 'style', 'noscript', 'nav', 'header', 'footer',
             'aside', 'svg', 'form', 'button', 'iframe')

_VALID_LEVELS = ("high", "medium", "low", "none")


def _log(*a):
    print("[company_intel]", *a, flush=True)


# ── 抓取与正文提取 ──

def _normalize_origin(website: str) -> str:
    """website → scheme://netloc (origin), 作为抓首页与拼相对链接的 base。"""
    if not website:
        return ""
    s = website.strip()
    if not s.startswith("http"):
        s = "https://" + s
    p = urlparse(s)
    if not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}"


def _fetch(client: httpx.Client, url: str) -> str:
    """GET 一个 URL, 返回去 BOM 的文本或空串。失败静默(上层跳过)。"""
    try:
        r = client.get(url)
        if r.status_code >= 400:
            return ""
        # httpx 默认按 charset 解码; 兜底 utf-8
        return r.text
    except Exception:
        return ""


def _extract_text(html: str) -> str:
    """HTML → 正文纯文本 (re-based, 鲁棒): 去 script/style/noscript/svg/template 整块,
    去剩余 tag, 解码实体, 压空白。

    用 re 而非 lxml: 实测部分大站(如 trilux.com) lxml text_content 提取异常稀少
    (1.27MB HTML 只吐 53 字符), re 去块+去 tag 更稳。"""
    if not html:
        return ""
    import html as _html
    html = re.sub(r"<(script|style|noscript|svg|template)[^>]*>.*?</\1\s*>", " ",
                  html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    text = _html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _discover_intel_links(html: str, base: str, max_links: int = 8) -> list:
    """从首页 href 提取情报类同站内链 (命 _INTEL_LINK_KEYWORDS), 拼绝对 URL, 去重。

    同站判定: 相对路径 OR 绝对 URL 但 netloc 与 base 同站(去 www 比较)。
    修 trilux 类站点: 导航是绝对 URL(https://www.x.com/en/products),
    旧逻辑把所有 https:// 当外链跳过, 漏掉 about/products。"""
    if not html:
        return []
    base_host = urlparse(base).netloc.lower().lstrip("www.")
    found = []
    seen = {base}
    for href in re.findall(r'href=["\']([^"\']+)["\']', html, re.IGNORECASE):
        h = href.strip().split("#")[0].split("?")[0]
        if not h:
            continue
        low = h.lower()
        if low.startswith(("mailto:", "tel:", "javascript:", "data:")):
            continue
        # 同站: 相对路径 OR 绝对 URL netloc 匹配
        if low.startswith("http"):
            host = urlparse(h).netloc.lower().lstrip("www.")
            if host != base_host:
                continue  # 外链跳过
            full = h
        else:
            full = urljoin(base, h)
        if not any(k in low for k in _INTEL_LINK_KEYWORDS):
            continue
        if full in seen:
            continue
        seen.add(full)
        found.append(full)
        if len(found) >= max_links:
            break
    return found


def _gather_intel_text(website: str):
    """抓官网首页 + 智能发现的内页, 拼成喂 LLM 的情报原文。

    返回 (intel_text, sources)。intel_text 为空 = 官网抓取失败(上层降级)。
    """
    base = _normalize_origin(website)
    if not base:
        return "", []
    parts = []  # (label, text)
    sources = []
    with httpx.Client(
        timeout=httpx.Timeout(_FETCH_TIMEOUT, connect=5.0),
        headers={"User-Agent": _UA, "Accept-Language": "en,de;q=0.8"},
        follow_redirects=True,
    ) as c:
        root_html = _fetch(c, base)
        if not root_html:
            _log("homepage fetch failed:", base)
            return "", []
        root_text = _extract_text(root_html)
        if root_text:
            parts.append(("homepage", root_text[:_MAX_CHARS_PER_PAGE]))
            sources.append(base)
        # 候选页: 链接发现 (优先) + 种子路径
        candidates = _discover_intel_links(root_html, base)
        for p in _SEED_PATHS:
            u = base.rstrip("/") + p
            if u not in candidates:
                candidates.append(u)
        added = 0
        seen_url = {base}
        for url in candidates:
            if added >= _MAX_PAGES:
                break
            if url in seen_url:
                continue
            seen_url.add(url)
            h = _fetch(c, url)
            if not h:
                continue
            t = _extract_text(h)
            if len(t) < 80:
                continue  # 太短, 多半是空页/占位
            parts.append((url, t[:_MAX_CHARS_PER_PAGE]))
            sources.append(url)
            added += 1
    # 拼接, 总长上限
    out, total = [], 0
    for label, t in parts:
        if total >= _MAX_TOTAL_CHARS:
            break
        chunk = t[: _MAX_TOTAL_CHARS - total]
        out.append(f"### page: {label}\n{chunk}")
        total += len(chunk)
    return "\n\n".join(out), sources


# ── DeepSeek 判断 ──

def _build_prompt(company_name, website, country, intel_text, user_ctx):
    industry = (user_ctx.get("industry") or "").strip()
    product = (user_ctx.get("product_description") or "").strip()
    markets = user_ctx.get("target_markets") or []
    markets_s = ", ".join(markets) if isinstance(markets, list) else str(markets)

    if product or industry:
        user_block = (
            f"- 行业: {industry or '未知'}\n"
            f"- 产品: {product or '未知'}\n"
            f"- 目标市场: {markets_s or '未知'}"
        )
    else:
        user_block = "- (用户产品信息未录入, 请从通用中国 B2B 供应商视角判断合作可能性)"

    country_line = f"所在国家: {country}\n" if country else ""
    web_line = f"官网: {website}\n" if website else "官网: (未提供)\n"
    intel_block = intel_text if intel_text else "(官网抓取失败或无内容, 仅凭公司名与你已知信息尽量判断, confidence 给 low)"

    return f"""你是资深的外贸 B2B 尽调分析师。基于抓取到的公司官网内容, 结合"我们(中国供应商)"的产品, 分析目标公司。

【我们(中国供应商)的信息】
{user_block}

【目标公司】
公司名: {company_name}
{web_line}{country_line}

【抓取到的官网内容(已去噪)】
---
{intel_block}
---

请输出**严格 JSON**(不要任何多余文字、不要 markdown 代码围栏), 字段如下:
{{
  "company_type": "品牌方/进口商/代理商/分销商/本土制造商/工程商/零售商/服务商/其他, 之一(后附简短依据)",
  "business_scope": "主营业务与经营品类, 重点说明是否与我们产品相关",
  "scale_signals": "规模信号(员工/工厂/营收/成立年限/覆盖市场等, 抓到什么写什么, 无则写'未知')",
  "location": "国家/地区/城市(抓到才写, 否则'未知')",
  "cooperation_level": "high / medium / low / none 之一",
  "cooperation_reason": "从中国供应商视角的合作可能性分析: 是否需要货源/代工/新品/替代供应商? 结合我们产品给出理由",
  "key_signals": ["关键信号点1", "信号点2", "..."],
  "confidence": "high / medium / low (官网信息多=high, 信息少/抓取失败=low)",
  "summary": "一句话总结这家公司及其对我们产品的价值"
}}

cooperation_level 判定标准:
- high: 明确的进口商/代理商/分销商, 且业务与我们产品相关 (强合作可能)
- medium: 品牌方/制造商, 业务相关, 可能需要货源或代工
- low: 业务弱相关, 或纯本土服务商/信息不足
- none: 明显与我们产品不相关 (如完全不同的行业)

只输出 JSON。"""


def _call_deepseek(prompt: str) -> str:
    """POST DeepSeek chat/completions (json mode), 返回 content 文本。

    key 缺失时优雅降级 (与 _llm_filter_leads / hunter_discover 一致): 不 raise,
    warning + 返回空串, 让 _judge 走 _parse_json("") 兜底 (cooperation_level=unknown,
    confidence=low), enrich_company_profile 工具不崩而是降级返回低置信画像。"""
    if not _DEEPSEEK_KEY:
        _log("WARNING: DEEPSEEK_API_KEY 未配置, 公司画像判断降级(返回低置信结果, "
             "检查 .env 是否被加载)")
        return ""
    url = f"{_DEEPSEEK_BASE}/chat/completions"
    body = {
        "model": _DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0.3,
        "max_tokens": 1200,
    }
    with httpx.Client(timeout=httpx.Timeout(_DEEPSEEK_TIMEOUT, connect=10.0)) as c:
        r = c.post(
            url,
            headers={"Authorization": f"Bearer {_DEEPSEEK_KEY}",
                     "Content-Type": "application/json"},
            json=body,
        )
        r.raise_for_status()
        data = r.json()
    return data["choices"][0]["message"]["content"]


def _parse_json(raw: str) -> dict:
    """容错解析 DeepSeek 返回 (去 markdown 围栏 + 失败兜底)。"""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*", "", s).strip()
        if s.endswith("```"):
            s = s[:-3].strip()
    try:
        return json.loads(s)
    except Exception:
        return {"_parse_error": True, "raw": raw, "cooperation_level": "unknown",
                "confidence": "low"}


def _judge(company_name, website, country, intel_text, user_ctx) -> dict:
    prompt = _build_prompt(company_name, website, country, intel_text, user_ctx)
    raw = _call_deepseek(prompt)
    obj = _parse_json(raw)
    level = (obj.get("cooperation_level") or "").strip().lower()
    if level not in _VALID_LEVELS:
        level = "unknown"
    obj["cooperation_level"] = level
    return obj


# ── 主入口 ──

def research(user_id: str, company_name: str = None, website: str = None,
             country: str = None, lead_id: str = None) -> dict:
    """对一个公司做画像 + 合作可能性判断, 并(若有 lead_id)update 到 leads 表。

    lead_id 优先: 从库读 company_name/website/country 补全空参。
    返回情报 dict (含 cooperation_level/company_type/sources/saved_to_db 等)。
    """
    from storage.db import get_user_config, get_lead, update_lead_intel

    # 1. lead_id 优先补全公司信息
    if lead_id:
        try:
            lead = get_lead(user_id, lead_id)
        except Exception as e:
            _log("get_lead failed:", e)
            lead = None
        if lead:
            company_name = company_name or lead.get("company_name")
            website = website or lead.get("website")
            country = country or lead.get("country")

    company_name = (company_name or "").strip()
    if not company_name:
        return {"error": "缺少 company_name (请传 lead_id 或 company_name)"}

    # 2. 读用户上下文 (合作判断的用户视角)
    try:
        user_ctx = get_user_config(user_id) or {}
    except Exception as e:
        _log("get_user_config failed:", e)
        user_ctx = {}

    # 3. 抓官网情报
    intel_text, sources = ("", [])
    if website:
        try:
            intel_text, sources = _gather_intel_text(website)
        except Exception as e:
            _log("_gather_intel_text failed:", e)
    else:
        _log("no website, 跳过官网抓取, 仅凭公司名判断")

    # 4. DeepSeek 判断
    try:
        intel = _judge(company_name, website, country, intel_text, user_ctx)
    except Exception as e:
        _log("_judge failed:", e)
        return {"error": f"DeepSeek 判断失败: {type(e).__name__}: {e}",
                "company_name": company_name, "website": website or "",
                "sources": sources, "fetched_pages": len(sources)}

    intel["company_name"] = company_name
    intel["website"] = website or ""
    intel["sources"] = sources
    intel["fetched_pages"] = len(sources)

    # 5. 入库 (有 lead_id 才写)
    saved = False
    if lead_id:
        try:
            update_lead_intel(user_id, lead_id,
                              json.dumps(intel, ensure_ascii=False),
                              intel.get("cooperation_level", ""))
            saved = True
        except Exception as e:
            _log("update_lead_intel failed:", e)
    intel["saved_to_db"] = saved
    return intel


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    company_name = ""
    website = None
    country = None
    user_id = ""
    lead_id = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--website" and i + 1 < len(args):
            website = args[i + 1]; i += 2
        elif a == "--country" and i + 1 < len(args):
            country = args[i + 1]; i += 2
        elif a == "--user-id" and i + 1 < len(args):
            user_id = args[i + 1]; i += 2
        elif a == "--lead-id" and i + 1 < len(args):
            lead_id = args[i + 1]; i += 2
        else:
            company_name = a; i += 1

    if not company_name and not lead_id:
        print("用法: python company_intel.py <company_name> "
              "[--website URL --country XX --user-id U --lead-id L]")
        sys.exit(1)

    from dotenv import load_dotenv
    _env = os.environ.get("LEADGEN_ENV_FILE", "").strip()
    if _env:
        load_dotenv(_env)
    else:
        # 自动加载项目根 .env (本文件在 sources/ 子目录, 项目根 = 上两级)
        load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    # 重新读取 env (load_dotenv 后)
    _DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()

    t0 = 0
    import time as _t
    t0 = _t.time()
    r = research(user_id or "test_user_001", company_name or "",
                 website=website, country=country, lead_id=lead_id)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print(f"\n(用时 {_t.time()-t0:.1f}s)", file=sys.stderr)

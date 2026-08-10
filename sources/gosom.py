"""档案源适配器: gosom/google-maps-scraper (Google Maps 经销商档案)。

产出结构化公司档案 (company/website/email/phone 齐全) —— 外贸高质量 lead。

依赖 gosom 的 google_maps_scraper 二进制 (内嵌 playwright, 首次运行自动下载
chromium)。该二进制不随仓库分发, 需自行下载放到项目 bin/google_maps_scraper,
或用环境变量 GOSOM_BIN 指向任意路径。gosom -email 会爬每个公司官网提取邮箱。

通用源 (增强):
- _COUNTRY 表覆盖全球主要贸易国, 英文名/ISO2/ISO3/中文/本地名 → {lang, iso}
- _lang_for/_iso_for: normalize 后查表, fallback lang="en"/iso=None
- _product_terms: 从 query 剥离角色词/国家词/介词, 剩产品词 (绝不写死品类)
- search() 去噪只降分 (相关性 -25 / 地理不符 -20), 绝不硬删行防误杀真客户
"""
import json
import os
import re
import subprocess
import tempfile

from .base import LeadCandidate

ENABLED = True  # 免 docker 二进制已实测稳定

BIN = os.environ.get(
    "GOSOM_BIN",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "bin",
        "google_maps_scraper",
    ),
)
SOURCE_TAG = "archive"

# ---------------------------------------------------------------------------
# 全球主要贸易国 → {lang (ISO639-1), iso (ISO3166-1 alpha-2)} 映射
# 一行一个 iso; 每行 dict 里的 key (英文名/ISO2/ISO3/中文/本地名) 经 _normalize
# 全部归一到同一条, 避免重复.
# 不是"目标白名单", 只是语言/ISO 查表 —— search() 用它给 gosom 传 -lang.
# ---------------------------------------------------------------------------
_COUNTRY = {}


def _register(iso: str, lang: str, **aliases):
    """把一个国家的所有别名注册到同一 {lang, iso, en, cn} 记录。

    aliases 至少含 en (英文名); 可选 cn (中文)/local (本地名)/iso3 等。
    这些字段同时写进 rec 供反查 (_product_terms 剔除国家词 / _en_for_iso)。
    所有查找键统一小写 (_normalize 已小写), iso 也小写存 → 查表全走小写。
    """
    rec = {"lang": lang, "iso": iso.upper()}
    rec["en"] = aliases.get("en", "")
    rec["cn"] = aliases.get("cn", "")
    _COUNTRY[iso.lower()] = rec
    for k, v in aliases.items():
        nv = _normalize(v)
        if nv and nv not in _COUNTRY:
            _COUNTRY[nv] = rec


def _normalize(s: str) -> str:
    """lower/strip/去 the 前缀/去常见后缀 → 统一查表 key。"""
    if not s:
        return ""
    s = str(s).strip().lower()
    # 去 "the " 前缀
    if s.startswith("the "):
        s = s[4:]
    # 去常见后缀
    for suf in (" republic", " of", " federation", " kingdom", " states"):
        s = re.sub(rf"{suf}\b.*$", "", s).strip()
    return s


# --- 欧洲 ---
_register("DE", "de", en="Germany", cn="德国", iso3="DEU", local="Deutschland")
_register("FR", "fr", en="France", cn="法国", iso3="FRA")
_register("IT", "it", en="Italy", cn="意大利", iso3="ITA", local="Italia")
_register("ES", "es", en="Spain", cn="西班牙", iso3="ESP", local="España")
_register("PT", "pt", en="Portugal", cn="葡萄牙", iso3="PRT", local="Portugal")
_register("NL", "nl", en="Netherlands", cn="荷兰", iso3="NLD", local="Nederland")
_register("BE", "nl", en="Belgium", cn="比利时", iso3="BEL", local="België")
_register("CH", "de", en="Switzerland", cn="瑞士", iso3="CHE", local="Schweiz")
_register("AT", "de", en="Austria", cn="奥地利", iso3="AUT", local="Österreich")
_register("PL", "pl", en="Poland", cn="波兰", iso3="POL", local="Polska")
_register("CZ", "cs", en="Czechia", cn="捷克", iso3="CZE", local="Česko", alt="Czech Republic")
_register("HU", "hu", en="Hungary", cn="匈牙利", iso3="HUN")
_register("RO", "ro", en="Romania", cn="罗马尼亚", iso3="ROU")
_register("SE", "sv", en="Sweden", cn="瑞典", iso3="SWE", local="Sverige")
_register("DK", "da", en="Denmark", cn="丹麦", iso3="DNK")
_register("NO", "no", en="Norway", cn="挪威", iso3="NOR", local="Norge")
_register("FI", "fi", en="Finland", cn="芬兰", iso3="FIN", local="Suomi")
_register("GR", "el", en="Greece", cn="希腊", iso3="GRC", local="Ελλάδα")
_register("RU", "ru", en="Russia", cn="俄罗斯", iso3="RUS")
_register("TR", "tr", en="Turkey", cn="土耳其", iso3="TUR", local="Türkiye")
_register("IE", "en", en="Ireland", cn="爱尔兰", iso3="IRL")
_register("GB", "en", en="United Kingdom", cn="英国", iso3="GBR", local="UK", alt="Britain", alt2="Great Britain")
# --- 美洲 ---
_register("US", "en", en="United States", cn="美国", iso3="USA", local="USA", alt="America")
_register("CA", "en", en="Canada", cn="加拿大", iso3="CAN")
_register("MX", "es", en="Mexico", cn="墨西哥", iso3="MEX", local="México")
_register("BR", "pt", en="Brazil", cn="巴西", iso3="BRA", local="Brasil")
_register("AR", "es", en="Argentina", cn="阿根廷", iso3="ARG", local="Argentina")
_register("CL", "es", en="Chile", cn="智利", iso3="CHL")
_register("CO", "es", en="Colombia", cn="哥伦比亚", iso3="COL")
_register("PE", "es", en="Peru", cn="秘鲁", iso3="PER", local="Perú")
# --- 亚太 ---
_register("CN", "zh", en="China", cn="中国", iso3="CHN", local="中国")
_register("JP", "ja", en="Japan", cn="日本", iso3="JPN", local="日本")
_register("KR", "ko", en="South Korea", cn="韩国", iso3="KOR", alt="Korea", local="대한민국")
_register("IN", "en", en="India", cn="印度", iso3="IND", local="भारत")
_register("ID", "id", en="Indonesia", cn="印度尼西亚", iso3="IDN")
_register("VN", "vi", en="Vietnam", cn="越南", iso3="VNM", local="Việt Nam")
_register("TH", "th", en="Thailand", cn="泰国", iso3="THA", local="ประเทศไทย")
_register("MY", "ms", en="Malaysia", cn="马来西亚", iso3="MYS")
_register("PH", "en", en="Philippines", cn="菲律宾", iso3="PHL")
_register("SG", "en", en="Singapore", cn="新加坡", iso3="SGP")
_register("AU", "en", en="Australia", cn="澳大利亚", iso3="AUS")
_register("NZ", "en", en="New Zealand", cn="新西兰", iso3="NZL")
_register("PK", "en", en="Pakistan", cn="巴基斯坦", iso3="PAK")
_register("BD", "en", en="Bangladesh", cn="孟加拉国", iso3="BGD")
# --- 中东非洲 ---
_register("SA", "ar", en="Saudi Arabia", cn="沙特阿拉伯", iso3="SAU")
_register("AE", "ar", en="United Arab Emirates", cn="阿联酋", iso3="ARE", local="UAE", alt="Emirates")
_register("IL", "he", en="Israel", cn="以色列", iso3="ISR", local="ישראל")
_register("EG", "ar", en="Egypt", cn="埃及", iso3="EGY")
_register("NG", "en", en="Nigeria", cn="尼日利亚", iso3="NGA")
_register("ZA", "en", en="South Africa", cn="南非", iso3="ZAF")
_register("KE", "en", en="Kenya", cn="肯尼亚", iso3="KEN")
_register("MA", "ar", en="Morocco", cn="摩洛哥", iso3="MAR")


def _lookup(country: str) -> dict:
    """normalize 后查表; 传 ""/None/查不到 → 返回 {}。"""
    if not country:
        return {}
    n = _normalize(country)
    if not n:
        return {}
    # 直接命中
    if n in _COUNTRY:
        return _COUNTRY[n]
    # 去掉可能的 "republic" 残留等再试一次
    n2 = re.sub(r"\s+", " ", n).strip()
    return _COUNTRY.get(n2, {})


def _lang_for(country: str) -> str:
    """搜不到 fallback 'en'。"""
    return _lookup(country).get("lang", "en")


def _iso_for(country: str):
    """搜不到 fallback None。"""
    rec = _lookup(country)
    return rec.get("iso") if rec else None


# ISO → 英文名反查表 (gosom 返回 complete_address.country 是 ISO 码)
_ISO_TO_EN = {}
for _k, _v in _COUNTRY.items():
    iso = _v["iso"]
    if iso not in _ISO_TO_EN:
        _ISO_TO_EN[iso] = _v.get("en") or iso


def _en_for_iso(iso: str) -> str:
    """ISO → 英文名; 查不到留 ISO。"""
    if not iso:
        return ""
    return _ISO_TO_EN.get(iso.upper(), iso)


# ---------------------------------------------------------------------------
# 产品词提取 (通用, 绝不写死品类)
# ---------------------------------------------------------------------------
# 角色词 (买方/卖方身份) + 介词 → 从 query 剥离, 多语言
_ROLE_WORDS = {
    # 英
    "distributor", "distributors", "dealer", "dealers", "wholesaler", "wholesalers",
    "wholesale", "retailer", "retailers", "retail", "buyer", "buyers", "importer",
    "importers", "supplier", "suppliers", "supplier", "manufacturer", "manufacturers",
    "manufacturing", "reseller", "resellers", "vendor", "vendors", "agent", "agents",
    "broker", "brokers", "trader", "traders", "company", "companies", "store", "stores",
    "shop", "shops", "outlet", "outlets", "seller", "sellers", "agency", "firm", "firms",
    # 德
    "großhändler", "grosshaendler", "hersteller", "vertriebsdienst", "vertrieb",
    "haendler", "händler", "lieferant", "lieferanten", "vertreter", "verkäufer",
    "verkaeufer", "importeure", "importe", "fabrikant", "fabrikanten", "gesellschaft",
    "geschäft", "geschaeft", "laden", "filiale",
    # 法
    "distributeur", "distributeurs", "grossiste", "grossistes", "fabricant",
    "fabricants", "fournisseur", "fournisseurs", "revendeur", "revendeurs", "importateur",
    "importateurs", "détaillant", "detaillele", "vendeur", "vendeurs", "boutique", "magasin",
    # 西/葡/意
    "distribuidor", "distribuidores", "mayorista", "mayoristas", "fabricante",
    "fabricantes", "proveedor", "proveedores", "proveedora", "proveedoras", "revendedor",
    "revendedores", "importador", "importadores", "minorista", "minoristas", "vendedor",
    "vendedores", "negocio", "tienda", "comercio", "azienda", "aziende", "ingrosso",
    "grossista", "grossiste",
    # 荷/北欧/其他常见
    "distributeur", "leverancier", "leveranciers", "fabrikant", "groothandel",
    "winkel", "leverandør", "leverandor", "tillverkare", "valmistaja",
    # 中文角色词 (外贸用户常用中文搜)
    "经销商", "代理商", "分销商", "批发商", "零售商", "供应商", "制造商",
    "生产商", "进口商", "出口商", "贸易商", "厂商", "厂家", "公司", "企业",
    "店铺", "商店", "门市",
}

_PREPOSITIONS = {
    "in", "of", "for", "the", "and", "und", "zu", "di", "da", "de", "del", "du",
    "el", "la", "le", "los", "las", "en", "et", "i", "den", "der", "des", "à",
    "a", "with", "from", "to", "near", "at", "on",
}


def _product_terms(query: str) -> list:
    """分词 (空格/连字符/标点), 剥离角色词/国家词/介词 → 剩产品词 (小写)。

    国家词剔除: 直接查 _COUNTRY 键表 (英文名/中文/ISO/本地名全是 key),
    比 en/cn/iso 三个 set 各查一遍更全 (本地名 deutschland 等都能剔)。
    """
    if not query:
        return []
    tokens = re.split(r"[\s/\-_,;&]+", query.lower())
    out = []
    for t in tokens:
        t = t.strip().strip(".")
        if not t:
            continue
        if t in _ROLE_WORDS:
            continue
        if t in _PREPOSITIONS:
            continue
        if t in _COUNTRY:  # 任意国家别名 (含本地名/ISO/中英)
            continue
        if t.isdigit():
            continue
        out.append(t)
    return out


def _proxy_arg() -> str:
    """gosom -proxies 参数 (纯环境变量驱动): LEADGEN_PROXY > HTTPS_PROXY > HTTP_PROXY。

    返回 http://host:port 形式; 无任何代理 env 则返回空串 (gosom 直连)。
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


def search(query: str, country: str = "", max_results: int = 20) -> list:
    """Run gosom in a private temporary directory that is always removed."""
    if not ENABLED:
        return []
    if not os.path.exists(BIN):
        raise RuntimeError(f"gosom 二进制不存在: {BIN}")
    with tempfile.TemporaryDirectory(prefix="gosom_") as work:
        return _search_in_workdir(query, country, max_results, work)


def _search_in_workdir(query: str, country: str, max_results: int, work: str) -> list:
    """Parse gosom JSONL inside a caller-owned temporary directory."""
    qfile = os.path.join(work, "queries.txt")
    with open(qfile, "w") as f:
        # gosom 接受自然语言查询; 地域塞进查询更精准
        f.write(f"{query}{' in ' + country if country else ''}\n")
    outdir = os.path.join(work, "out")
    os.makedirs(outdir, exist_ok=True)
    rfile = os.path.join(outdir, "r.json")

    cmd = [
        BIN,
        "-input", qfile,
        "-results", rfile,
        "-json", "-email",
        "-depth", "1", "-c", "4",
        "-lang", _lang_for(country),
        "-exit-on-inactivity", "3m",
    ]
    proxy = _proxy_arg()
    if proxy:
        cmd += ["-proxies", proxy]

    env = os.environ.copy()
    if proxy and "@" in proxy:
        # chromium 下载/额外资源走代理 (无 auth 部分)
        env["HTTPS_PROXY"] = proxy.split("@", 1)[-1]
        env["HTTP_PROXY"] = env["HTTPS_PROXY"]

    try:
        subprocess.run(cmd, check=False, timeout=300, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        pass

    if not os.path.exists(rfile) or os.path.getsize(rfile) == 0:
        return []

    # r.json 是 JSONL (每行一个 JSON 对象)
    rows = []
    with open(rfile) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue

    # 去噪上下文
    product_terms = _product_terms(query)
    user_iso = _iso_for(country)
    _BAD_EXT = re.compile(r'\.(png|jpe?g|gif|webp|svg|bmp|ico|css|js)$', re.I)
    _GOOD = re.compile(r'^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$')

    out = []
    for row in rows:
        emails = row.get("emails") or []
        if isinstance(emails, str):
            emails = [emails]
        emails = [e for e in emails if e and _GOOD.match(e) and not _BAD_EXT.search(e)]
        website = row.get("web_site") or row.get("website") or ""

        # 真实国家: gosom complete_address.country (ISO) → 英文名, 比用户传的准
        addr = row.get("complete_address") or {}
        real_iso = (addr.get("country") or "").upper() if isinstance(addr, dict) else ""
        real_country = _en_for_iso(real_iso) if real_iso else (country or "")

        # 相关性: 产品词任一出现在 title 或 categories 里 → 相关; 否则 -25
        cats = row.get("categories") or []
        if isinstance(cats, str):
            cats = [cats]
        main_cat = row.get("category") or ""
        haystack = " ".join([
            (row.get("title") or ""),
            " ".join(str(c) for c in cats),
            str(main_cat),
        ]).lower()
        relevant = True
        if product_terms:
            relevant = any(term in haystack for term in product_terms)

        # 地理校验: 用户传了 country 且双方都有 ISO, 不符 -20
        geo_ok = True
        if user_iso and real_iso:
            geo_ok = (user_iso == real_iso)

        # 有邮箱/网站的略加分, 让 max_results 截断时优先保留可发信的
        score = 70.0
        if emails:
            score += 15
        if website:
            score += 5
        if not relevant:
            score -= 25
        if not geo_ok:
            score -= 20

        out.append(LeadCandidate(
            company_name=(row.get("title") or "")[:160],
            website=website,
            phone=(row.get("phone") or ""),
            email=emails[0] if emails else "",
            country=real_country,
            source="gosom_maps",
            search_query=query,
            score=score,
            detail=(row.get("address") or "")[:200],
        ))
        if len(out) >= max_results:
            break
    return out

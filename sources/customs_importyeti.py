"""海关提单源适配器: ImportYeti (美国海关提单免费查询)。

只做 company 尽调 (验真候选买家是否真实采购方)，不做 discovery:
  - location/search/supplier 反查 都不能用 (实测: location 拉进来的
    IKEA/Walmart 等大公司污染 leads 库)。
  - 免费版只能按公司名搜，进 company 页看「从中国进口占比 + 进什么 HS +
    中国供应商是谁」。
  - 所以不进 registry、不自动入库，只暴露 lookup() 给 MCP
    importyeti_lookup 工具 (和 verify_email 一样是查询类工具)。

CF 过法 (验证):
  ImportYeti 全站前置 Cloudflare Turnstile。chromium + 反检测
  (--disable-blink-features=AutomationControlled + navigator.webdriver=undefined
  + window.chrome 注入) 自动过 turnstile，**不需要 capsolver**。
  轮询 page.title 直到不含 "moment" 和 "performing security" 即过。

数据 (实测):
  - shipments_by_country 表: 找 China 行 → 占比 + 总票数
  - HS codes 表: 扫 9405/8541/8539 前缀 → LED 相关
  - suppliers 表: 找 China 行 → 中国供应商公司名 (最多 8 个)
  数据 SSR 在 <table>，不需要解析 __next_f。
"""
import os
import re
import subprocess
import time
import urllib.parse

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
_CHROMIUM_PATH = os.path.expanduser(
    "~/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome")

_STEALTH_JS = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    "window.chrome = {runtime: {}, app: {}};"
)

# LED 相关 HS 前缀 (实测 LED HS: 9405 灯具/8541.41 LED芯片/8539.52 LED灯)
_LED_HS_PREFIXES = ("9405", "8541", "8539")


def _gateway_proxy() -> str:
    """探测 WSL 网关:7897 (Clash Verge), 给 requests 用。无认证代理。"""
    env_p = os.environ.get("LEADGEN_PROXY") or os.environ.get("HTTPS_PROXY") or ""
    m = re.search(r"(\d+\.\d+\.\d+\.\d+:\d+)", env_p)
    if m:
        return f"http://{m.group(1)}"
    try:
        gw = subprocess.check_output(
            "ip route | awk '/^default/{print $3}'",
            shell=True, text=True,
        ).strip()
        if gw:
            return f"http://{gw}:7897"
    except Exception:
        pass
    return ""


def _empty_result(error: str = "") -> dict:
    return {
        "found": False,
        "slug": "",
        "url": "",
        "company_name": "",
        "china_share": "",
        "has_led_hs": False,
        "led_hs_codes": [],
        "top_suppliers_cn": [],
        "shipments_total": "",
        "error": error,
    }


def _resolve_slug(page, company_name: str) -> str:
    """打开 search 页，抓第一个 /company/ 链接的 slug。"""
    q = urllib.parse.quote(company_name)
    url = f"https://www.importyeti.com/search?q={q}"
    for _ in range(6):
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if resp and resp.status < 500:
                break
        except Exception:
            time.sleep(2)
    _wait_cf(page, need_tables=False)
    href = page.evaluate('''() => {
        const a = document.querySelector('a[href^="/company/"]');
        return a ? a.getAttribute('href') : null;
    }''')
    if not href:
        return ""
    m = re.match(r'/company/([^/?#]+)', href)
    return m.group(1) if m else ""


def _wait_cf(page, need_tables: bool = True) -> bool:
    """过 CF + 等数据渲染。返回 True=数据已渲染, False=超时未渲染。

    ImportYeti 公司页是 Next.js SSR + 异步水合，table 是数据渲染后才出现的。
    只等 title 不够 (title 永远是空但 body 还在渲染导航栏阶段)。
    所以分两阶段:
      1. 等 title 不含 'moment'/'performing security' (过 CF turnstile)
      2. 等 <table> 元素出现 (数据水合完成)。need_tables=False 时跳过 (search 页)。
    table 一直不出现通常是 IP 被 Cloudflare 节流 (高频访问触发)，此时返回 False
    让上层给清晰错误而非 found=True 全空。
    """
    # 阶段1: 过 CF (title 稳定)
    for _ in range(18):
        try:
            t = page.title()
        except Exception:
            t = ""
        tl = (t or "").lower()
        if "moment" not in tl and "performing security" not in tl:
            break
        page.wait_for_timeout(1500)
    # 阶段2: 等数据渲染 (table 出现)
    if need_tables:
        for _ in range(20):  # 最多 30 秒
            count = page.evaluate("() => document.querySelectorAll('table').length")
            if count and count > 0:
                page.wait_for_timeout(1500)  # 让 table 内容填满
                return True
            page.wait_for_timeout(1500)
        return False  # table 没渲染出来
    else:
        page.wait_for_timeout(3000)
        return True


def _scrape_tables(page) -> list:
    """抓页面上所有 <table> → [{cap, rows}], cap 是附近的标题 (<60字)。
    rows 已裁到前 12 行，单元格裁到前 60 字。"""
    return page.evaluate('''() => [...document.querySelectorAll('table')].map(t=>{
        let p=t; let cap='';
        for(let i=0;i<4;i++){
            p=p.previousElementSibling||p.parentElement;
            if(!p)break;
            const tx=(p.innerText||'').trim();
            if(tx&&tx.length<80){cap=tx;break;}
        }
        const rows=[...t.querySelectorAll('tr')].slice(0,15).map(r=>
          [...r.querySelectorAll('th,td')].map(d=>(d.innerText||'').trim().slice(0,100)));
        return {cap, rows};
    })''')


def _classify_table(t: dict) -> str:
    """判断表格类型: shipments / hs / suppliers / other。

    ImportYeti 实际结构 (wal-mart dump 实测):
      - shipments_by_country 表: cap 是百分比 (如 '92.4\n%'),
        表头 ['Country','Shipments']
      - suppliers 表 (顶部 Top suppliers): cap 含 'suppliers' 字样,
        表头第一列含 'Suppliers\n\nCountry' (公司名+国家混排)
      - HS 表: 表头含 'HTS Code'
      - BOL 详情表: 表头含 'Bill of Lading' (忽略)
      - 地址表: 表头含 'Unlock' (忽略)
    """
    cap = (t.get("cap") or "").lower()
    rows = t.get("rows") or []
    header = " ".join((rows[0] if rows else [])).lower()
    # 排除: BOL 详情表 / 地址表 (都是噪声)
    if "bill of lading" in header or "unlock" in header:
        return "other"
    # HS 表 (表头特征明确)
    if "hts code" in header or "hts code" in cap:
        return "hs"
    # suppliers 表: cap 含 'supplier' (Top suppliers 块标题) OR
    # 表头第一列含 'suppliers' (区分 BOL 详情表的 'suppliers\n\ncountry'
    # 已在前面排除)。放 shipments 之前判: suppliers 表也可能含 country/shipments 字样。
    if "supplier" in cap:
        return "suppliers"
    # shipments 表: cap 是百分比 OR 表头是 ['country','shipments']
    if re.search(r"\d+[\.\d]*\s*\n?\s*%", cap):
        return "shipments"
    if header == "country shipments":
        return "shipments"
    return "other"


def _parse_shipments(t: dict) -> tuple:
    """→ (china_share, shipments_total)。

    china_share 从 table caption 提取 (ImportYeti 把百分比放在 table 上方的浮动标签)。
    shipments_total 从 China 行的 'Shipments' 列提取 (如 '406,832')。
    一张 shipments 表只代表一个百分比分组，china 只会出现在第一个表 (cap=最大百分比)。
    """
    cap = t.get("cap") or ""
    rows = t.get("rows") or []
    # china_share: cap 形如 '92.4\n%' → '92.4%'
    china_share = ""
    m = re.search(r"(\d+(?:\.\d+)?)\s*\n?\s*%", cap)
    if m:
        china_share = f"{m.group(1)}%"
    # shipments_total: 找 China 行的第二列 (Shipments 列)
    shipments_total = ""
    for row in rows[1:]:
        cells = [(c or "").lower().strip() for c in row]
        # China 行: 第一列含 'china' 但不含 'republic' (排除 Taiwan, Republic of China)
        first = cells[0] if cells else ""
        if "china" in first and "republic" not in first:
            if len(row) >= 2:
                shipments_total = row[1].strip()
            break
    return china_share, shipments_total


def _parse_hs(rows: list) -> list:
    """→ 命中 LED 前缀的 HS code 列表 (去重)。"""
    out = []
    seen = set()
    if not rows:
        return out
    for row in rows[1:]:
        for cell in row:
            cell_clean = re.sub(r'[^\d.]', '', cell)
            if not cell_clean:
                continue
            for prefix in _LED_HS_PREFIXES:
                if cell_clean.startswith(prefix):
                    # 取前缀+小数点后2位 (如 9405.40 / 8541.41)
                    m = re.match(r'(' + prefix + r'(?:\.\d{1,4})?)', cell_clean)
                    code = m.group(1) if m else prefix
                    if code not in seen:
                        seen.add(code)
                        out.append(code)
                    break
    return out


def _parse_suppliers_cn(rows: list) -> list:
    """→ 中国供应商公司名 (去重，最多 8 个)。

    ImportYeti suppliers 表第一列格式: '公司名 \n 城市, China'
    (公司名和地址用 \n 连在同一单元格)。按 \n 切，取第一段 = 公司名。
    """
    out = []
    seen = set()
    if not rows:
        return out
    for row in rows[1:]:
        # 找含 'china' 的单元格 (排除 'Taiwan, Republic of China')
        for cell in row:
            cell_lower = cell.lower()
            if "china" not in cell_lower or "republic" in cell_lower:
                continue
            # 按 \n 切，第一段是公司名
            parts = [p.strip() for p in cell.split("\n") if p.strip()]
            if not parts:
                continue
            company = parts[0][:100]
            # 公司名不能是纯数字/百分号
            if re.fullmatch(r"[\d.,%\- ]+", company):
                continue
            # 过滤 ImportYeti 渲染占位符 (如 "Missing in source document")
            if "missing in source" in company.lower() or company.lower().startswith("missing"):
                continue
            key = company.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(company)
            if len(out) >= 8:
                return out
            break  # 一行只取一个公司名
    return out


def lookup(company_name: str, company_slug: str = None) -> dict:
    """查 ImportYeti 海关提单，返回该公司从中国进口的情况。"""
    from playwright.sync_api import sync_playwright

    company_name = (company_name or "").strip()
    slug = (company_slug or "").strip()
    if not company_name and not slug:
        return _empty_result("缺少 company_name")

    proxy_url = _gateway_proxy() or None

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            executable_path=_CHROMIUM_PATH,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
            proxy={"server": proxy_url} if proxy_url else None,
        )
        try:
            context = browser.new_context(
                user_agent=_UA, locale="en-US",
                viewport={"width": 1366, "height": 900},
            )
            context.add_init_script(_STEALTH_JS)
            page = context.new_page()

            # Step1: 解析 slug (若未提供)
            if not slug:
                slug = _resolve_slug(page, company_name or slug)
                if not slug:
                    return _empty_result("未在 ImportYeti 找到该公司")

            # Step2: 打开 company 页
            company_url = f"https://www.importyeti.com/company/{slug}"
            for _ in range(6):
                try:
                    resp = page.goto(company_url, wait_until="domcontentloaded",
                                     timeout=30000)
                    if resp and resp.status < 500:
                        break
                except Exception:
                    time.sleep(2)
            rendered = _wait_cf(page)

            # 检查是不是 404 / 无数据页
            body_text = page.evaluate("() => document.body ? document.body.innerText.slice(0,2000) : ''") or ""
            if "not found" in body_text.lower() and "company" in body_text.lower():
                return _empty_result("未在 ImportYeti 找到该公司")
            # table 没渲染出来 = 被 Cloudflare 节流或页面异常
            if not rendered or len(body_text) < 1000:
                return _empty_result(
                    "ImportYeti 页面数据未渲染 (可能被 Cloudflare 节流/限流，或页面异常)。"
                    "建议稍后重试或降低查询频率。"
                )

            tables = _scrape_tables(page)

            china_share = ""
            shipments_total = ""
            led_hs_codes = []
            top_suppliers_cn = []

            for t in tables:
                kind = _classify_table(t)
                rows = t.get("rows") or []
                if kind == "shipments":
                    cs, st = _parse_shipments(t)
                    if cs and not china_share:
                        china_share = cs
                    if st and not shipments_total:
                        shipments_total = st
                elif kind == "hs":
                    codes = _parse_hs(rows)
                    for c in codes:
                        if c not in led_hs_codes:
                            led_hs_codes.append(c)
                elif kind == "suppliers":
                    sups = _parse_suppliers_cn(rows)
                    for s in sups:
                        if s not in top_suppliers_cn:
                            top_suppliers_cn.append(s)

            result = {
                "found": True,
                "slug": slug,
                "url": company_url,
                "company_name": company_name,
                "china_share": china_share,
                "has_led_hs": bool(led_hs_codes),
                "led_hs_codes": led_hs_codes,
                "top_suppliers_cn": top_suppliers_cn,
                "shipments_total": shipments_total,
                "error": "",
            }
            return result
        finally:
            browser.close()


if __name__ == "__main__":
    import sys
    import json

    args = sys.argv[1:]
    company_name = ""
    slug = None
    i = 0
    while i < len(args):
        if args[i] == "--slug" and i + 1 < len(args):
            slug = args[i + 1]
            i += 2
        else:
            company_name = args[i]
            i += 1
    if not company_name and not slug:
        print("用法: python customs_importyeti.py <company_name> [--slug SLUG]")
        sys.exit(1)
    if not company_name:
        company_name = slug

    t0 = time.time()
    try:
        r = lookup(company_name, slug)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"❌ 查询失败: {type(e).__name__}: {e}")
        sys.exit(1)
    print(f"\n(用时 {time.time()-t0:.1f}s)", file=sys.stderr)

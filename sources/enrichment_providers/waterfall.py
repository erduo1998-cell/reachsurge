"""富集瀑布: 按成本/质量排序的 provider 链, 首命中即停, 缓存包裹。

provider 顺序 (静态, 编码了 cost/quality 权衡):
  1. scrape      免费 ~50% 命中, 真实公司邮箱 (scraped=中, 停)
  2. hunter      配额守卫 (Free 50/月), 已验证命名决策人邮箱 (verified=强, 停)
  3. smtp_probe  免费 ~9% 命中 (CN-IP 被挡), 慢 (verified/catchall, guessed 交 guess)
  4. guess       终端兜底 info@ (guessed=弱)

关键顺序理由:
  - scrape 先于 hunter: 免费, 先吃掉易得的 ~50%, 省 Hunter 配额。
  - hunter 先于 smtp_probe: smtp_probe 的 classify 对大服务商 MX 会立刻短路到
    info@(guessed); 若它排在 Hunter 前, 会抢走 Hunter 本可给的已验证命名邮箱。
  - smtp_probe 先于 guess: 先尝试真实 SMTP 验证, 验不出才 info@ 兜底。

停止规则 (首非 None 即停): 任一 provider 返回 EnrichResult 即终态, 不再往后。
  - smtp_probe 在「验不出」时返回 None (而非 guessed), 把 info@ 兜底统一交给 guess
    (classify 内部逻辑不动, 只是 adapter 重新解释其 guessed 分支为「无果, 交下家」)。
  - guess 对已知 domain 永远返回 guessed(info@) = 终端。

缓存: 先查 cache; 命中直接返回; 未命中跑瀑布, 跑完写入。
"""
from typing import List, Optional

from .base import Provider, EnrichInput, EnrichResult
from .cache import EnrichmentCache
from .providers.scrape import ScrapeProvider
from .providers.hunter import HunterProvider
from .providers.smtp_probe import SmtpProbeProvider
from .providers.guess import GuessProvider


def default_providers() -> List[Provider]:
    """默认瀑布顺序。每条 lead 新建一次 (hunter 配额状态内含)。"""
    return [
        ScrapeProvider(),
        HunterProvider(),
        SmtpProbeProvider(),
        GuessProvider(),
    ]


def enrich_domain(domain: str, website: str, country: str,
                  cache: EnrichmentCache, contact_name: str = "") -> Optional[EnrichResult]:
    """对单个 domain 跑富集瀑布。返回 EnrichResult 或 None(无 website/domain 由上层处理)。"""
    # 1. 缓存命中 -> 直接返回, 跳过整条瀑布 (省 Hunter 配额 + 省慢 SMTP)
    cached = cache.get(domain)
    if cached is not None:
        return cached

    # 2. 跑瀑布: 首非 None 即停
    inp = EnrichInput(domain=domain, website=website, country=country, contact_name=contact_name)
    result: Optional[EnrichResult] = None
    for provider in default_providers():
        try:
            r = provider.enrich(inp)
        except Exception:
            r = None   # 单 provider 异常不阻断瀑布, 交下家
        if r is not None:
            result = r
            break

    # 3. 写缓存 (命中或终端兜底都写; 全 None 极少发生因 guess 终端)
    if result is not None:
        cache.put(domain, result)
    return result

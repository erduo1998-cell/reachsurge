"""网站深抓 provider: adapter 包现有 sources.email_enrich._deep_scrape_emails。

不改其内部 (外科手术)。深抓返回的邮箱本身已过 _is_valid_scraped 过滤
(域匹配 + 黑名单 + 非业务 localpart), 再经 _pick_best 选最优 (优先 info/vertrieb/sales
等 PREFIXES 顺序的 role 邮箱, 否则取 localpart 最短)。命中即 scraped (中, 停)。

lazy import email_enrich: email_enrich (orchestrator) ↔ enrichment_providers 互引,
函数内 import 打破循环依赖。
"""
from typing import Optional

from ..base import Provider, EnrichInput, EnrichResult
from ..validate import is_valid_enrichment_email


class ScrapeProvider(Provider):
    name = "scrape"

    def enrich(self, inp: EnrichInput) -> Optional[EnrichResult]:
        if not inp.website:
            return None
        # lazy import (避免 email_enrich ↔ enrichment_providers 循环)
        from sources.email_enrich import _deep_scrape_emails, _pick_best_scraped, _website_is_useful

        if not _website_is_useful(inp.website):
            return None
        try:
            found = _deep_scrape_emails(inp.website, inp.domain, inp.country)
        except Exception:
            return None
        if not found:
            return None
        best = _pick_best_scraped(found, inp.domain)
        if not best or not is_valid_enrichment_email(best, inp.domain):
            return None
        local = best.split("@", 1)[0]
        return EnrichResult(
            email=best,
            status="scraped",
            confidence=70.0,   # 真实公司公开邮箱, 未自验
            evidence=f"scrape:{local}:pages={len(found)}",
            source=self.name,
        )

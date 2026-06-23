"""邮箱富集瀑布包。

入口: enrich_domain(domain, website, country, cache) → EnrichResult | None。
"""
from .base import Provider, EnrichInput, EnrichResult
from .cache import EnrichmentCache
from .waterfall import enrich_domain, default_providers

__all__ = [
    "Provider", "EnrichInput", "EnrichResult",
    "EnrichmentCache", "enrich_domain", "default_providers",
]

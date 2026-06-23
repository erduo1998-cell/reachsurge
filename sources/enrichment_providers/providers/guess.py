"""Guess provider: info@ 兜底 (终端)。

经 smtp_probe 把 'guessed' 分支转译为 None 后, guess 成为 info@ 的唯一出口,
让瀑布的弱结果兜底语义统一。对已知 domain 永远返回 guessed(info@) = 终端。

仅在前面 provider 都无果 (scrape 空 / Hunter 空/配额尽 / smtp_probe 无 MX 之外的
「验不出」) 时到达 —— 即 domain 有 MX 但前缀都验不过, 或 Hunter 配额尽且 smtp 大服务商短路。
info@ 在这类域仍可能投递, 是合理的弱兜底。
"""
from typing import Optional

from ..base import Provider, EnrichInput, EnrichResult


class GuessProvider(Provider):
    name = "guess"

    def enrich(self, inp: EnrichInput) -> Optional[EnrichResult]:
        if not inp.domain:
            return None
        return EnrichResult(
            email=f"info@{inp.domain}",
            status="guessed",
            confidence=15.0,
            evidence="guess:info@fallback",
            source=self.name,
        )

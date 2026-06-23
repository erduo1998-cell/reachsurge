"""SMTP 探测 provider: adapter 包现有 sources.email_enrich.get_mx + classify。

不改其内部 (外科手术)。关键重新解释 (让 guess provider 有意义):
  - classify 返回 ('verified', real_prefix@, ev) → verified (强, 停)
  - classify 返回 ('catchall', info@, ev)       → catchall (停; info@ 在 catch-all 域是最佳)
  - classify 返回 ('guessed',  info@, ev)       → 返回 None (验不出, 交 guess 统一兜底)
  - get_mx 无 MX                                 → no_mx (终端; 无 MX 时 info@ 投递无意义)

classify 内部逻辑 (含大服务商 MX 短路 info@ / catch-all 探测 / prefix 逐个验) 完全不动,
只是 adapter 把它的 'guessed' 分支转译成「无果, 交下家」。
"""
from typing import Optional

from ..base import Provider, EnrichInput, EnrichResult


class SmtpProbeProvider(Provider):
    name = "smtp_probe"

    def enrich(self, inp: EnrichInput) -> Optional[EnrichResult]:
        # lazy import (避免循环)
        from sources.email_enrich import get_mx, classify

        mx_host, mx_ev = get_mx(inp.domain)
        if not mx_host:
            return EnrichResult(
                email="", status="no_mx", confidence=0.0,
                evidence="smtp_probe:no_mx", source=self.name,
            )
        try:
            status, em, ev = classify(inp.domain, mx_host)
        except Exception:
            return None

        if status == "verified":
            return EnrichResult(
                email=em, status="verified", confidence=90.0,
                evidence=f"smtp_probe:{ev}", source=self.name,
            )
        if status == "catchall":
            return EnrichResult(
                email=em, status="catchall", confidence=30.0,
                evidence=f"smtp_probe:{ev}", source=self.name,
            )
        # status == 'guessed' → 验不出, 交 guess provider 统一 info@ 兜底
        return None

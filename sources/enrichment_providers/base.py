"""富集 Provider 抽象 + 统一输入/输出结构。

设计要点:
- 富集以「域名」为单位: 同一 domain → 同一邮箱, 可缓存。
- Provider.enrich(input) 返回 EnrichResult(命中) 或 None(本 provider 无果, 交给下一个)。
- email_status 用现有 DB 枚举 (unknown/existing/verified/scraped/guessed/catchall/no_mx/no_domain),
  不新增状态, 保全对外契约 (email_status/返回格式/幂等全保留)。
- confidence 仅作排序辅助与 provenance, 不改变契约。

命中强度分层 (waterfall 据此决定是否停):
  verified / existing  → 强命中 (停)
  scraped              → 中命中 (停; 真实公司邮箱, 未自验)
  catchall             → 中 (停; info@ 在 catch-all 域已是最佳)
  guessed              → 弱 (guess provider 终端兜底)
  no_mx / no_domain    → 终端 (停; 无 MX 时 info@ 投递无意义)
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class EnrichInput:
    """单个 domain 的富集输入。"""
    domain: str
    website: str = ""
    country: str = ""
    contact_name: str = ""   # 可选 (leadgen 多数无人名)


@dataclass
class EnrichResult:
    """单个 domain 的富集产出。email 可为空串 (no_mx/no_domain)。"""
    email: str
    status: str             # email_status 枚举值
    confidence: float = 0.0
    evidence: str = ""      # provenance, e.g. "scrape:role:info", "hunter:domain-search:conf=92:verification=valid"
    source: str = ""        # provider name
    contact_name: str = ""  # 联系人姓名 (Hunter domain-search first+last; 多数为空)
    contact_title: str = "" # 联系人职位 (Hunter position)


class Provider:
    """富集 provider 基类。子类实现 enrich。"""
    name: str = "base"

    def enrich(self, inp: EnrichInput) -> Optional[EnrichResult]:  # pragma: no cover - abstract
        raise NotImplementedError

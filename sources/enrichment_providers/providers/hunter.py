"""Hunter provider (新, httpx): 调 hunter.io/v2/domain-search。

为何用 domain-search 而非 email-finder+verifier (对草图的细化):
  - 本场景大量线索无人名, email-finder 需 first/last name, 弱场景。
  - domain-search 一次调用返回该域全部已知邮箱, 每个自带 verification.status
    (valid/invalid/accept_all ...) + confidence + 命名联系人(first_name/position/seniority)。
    = finder + verifier 合一, 1 search 配额/域 (省 100 verifier/月 配额)。

status 映射 (诚实, 不过度声称):
  - 选中的邮箱 verification.status == "valid" → verified (Hunter 已 SMTP 验可送达)
  - 否则 confidence ≥ 80 → scraped (真实 pattern 邮箱, 未自验)
  - accept_all 域且无 valid 邮箱 → catchall (info@ 兜底)
  - 其余弱结果 → None (交 smtp_probe/guess)

配额守卫 (Free 50 search/月): __init__ 探一次 account.remaining; 用尽则整轮禁用。
单次响应非 200 / 明显 quota 错误 → 即时禁用, 后续域直接跳过 (不烧配额)。
Hunter 域搜索走 WSL 代理 (https_proxy=172.23.64.1:7897), httpx 显式传 proxy。
"""
import os
from typing import Optional

from ..base import Provider, EnrichInput, EnrichResult
from ..validate import is_valid_enrichment_email

_DOMAIN_SEARCH = "https://api.hunter.io/v2/domain-search"
_ACCOUNT = "https://api.hunter.io/v2/account"

# 决策人 seniority 排序 (越靠前越想找): 真实采购决策权通常在 executive/vp/director/manager
_SENIORITY_RANK = {
    "executive": 5, "vp": 4, "director": 3, "manager": 2,
    "senior": 1, "junior": 0,
}


class HunterProvider(Provider):
    name = "hunter"

    def __init__(self):
        self.api_key = os.environ.get("HUNTER_API_KEY", "").strip()
        self.proxy = self._read_proxy()
        # 整轮配额开关: 无 key / 配额耗尽 → False, 所有 enrich 直接 None
        self._active = bool(self.api_key)
        if self._active:
            self._active = self._check_quota()

    @staticmethod
    def _read_proxy() -> Optional[str]:
        for k in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
            v = os.environ.get(k, "").strip()
            if v:
                return v
        return None

    def _check_quota(self) -> bool:
        """启动探一次账户剩余配额。<=0 或出错 → 关闭本轮 Hunter。

        domain-search 消耗 credits; credits.remaining 是统配额(Hunter 返回结构:
        requests.credits.{used,available,remaining}, 而 searches 只有 used/available
        无 remaining)。取不到则不禁用, 靠运行时 429 兜底。
        """
        try:
            import httpx
            with httpx.Client(timeout=20, proxy=self.proxy) as c:
                r = c.get(_ACCOUNT, params={"api_key": self.api_key})
            if r.status_code != 200:
                return False
            req = r.json().get("data", {}).get("requests", {}) or {}
            remaining = (req.get("credits") or {}).get("remaining")
            if remaining is None:
                return True   # 取不到不禁用, 靠 429
            return float(remaining) > 0
        except Exception:
            return False   # 探测失败不阻断瀑布, 只关 Hunter

    def _disable(self) -> None:
        self._active = False

    def enrich(self, inp: EnrichInput) -> Optional[EnrichResult]:
        if not self._active:
            return None
        try:
            import httpx
            with httpx.Client(timeout=25, proxy=self.proxy) as c:
                r = c.get(_DOMAIN_SEARCH, params={
                    "domain": inp.domain, "api_key": self.api_key, "limit": 10,
                })
        except Exception:
            return None   # 网络错, 本域交下家; 不关全局 (可能是单域超时)

        if r.status_code != 200:
            # 401/403/429 = key/配额问题 → 关闭本轮, 不再烧
            if r.status_code in (401, 403, 429):
                self._disable()
            return None
        try:
            data = r.json().get("data", {})
        except Exception:
            return None

        accept_all = bool(data.get("accept_all", False))
        emails = data.get("emails") or []

        best = self._pick_best(emails, inp.domain)
        if best is None:
            # 无可用邮箱: accept_all 域给 info@(catchall), 否则交下家
            if accept_all:
                return EnrichResult(
                    email=f"info@{inp.domain}", status="catchall", confidence=0.0,
                    evidence="hunter:accept_all:no_valid_email", source=self.name,
                )
            return None

        em = best["value"].lower().strip()
        conf = float(best.get("confidence") or 0)
        verif = (best.get("verification") or {}).get("status", "")

        if verif == "valid":
            status = "verified"
        elif conf >= 80:
            status = "scraped"
        elif accept_all:
            # 非 valid + 中低置信 + catch-all 域 → 降级 info@ catchall
            em = f"info@{inp.domain}"
            status = "catchall"
        else:
            return None   # 弱结果, 交 smtp_probe/guess

        pos = best.get("position") or ""
        ev = f"hunter:domain-search:conf={int(conf)}:verification={verif}:accept_all={accept_all}"
        if pos:
            ev += f":pos={pos[:40]}"
        # 透传联系人: first_name+last_name 拼全名 (Hunter domain-search 返回字段)
        first = (best.get("first_name") or "").strip()
        last = (best.get("last_name") or "").strip()
        full_name = " ".join(n for n in (first, last) if n).strip()
        return EnrichResult(
            email=em, status=status, confidence=conf,
            evidence=ev, source=self.name,
            contact_name=full_name,
            contact_title=pos.strip(),
        )

    @staticmethod
    def _pick_best(emails: list, domain: str) -> Optional[dict]:
        """从 Hunter 返回的邮箱里选最佳: 优先 valid+personal+高 seniority+高 confidence。"""
        valid_pool = [e for e in emails
                      if (e.get("verification") or {}).get("status") == "valid"
                      and is_valid_enrichment_email(e.get("value"), domain)]
        pool = valid_pool or [e for e in emails
                              if is_valid_enrichment_email(e.get("value"), domain)]
        if not pool:
            return None

        def score(e):
            is_valid = 1 if (e.get("verification") or {}).get("status") == "valid" else 0
            is_personal = 1 if e.get("type") == "personal" else 0   # 命名人 > role
            sen = _SENIORITY_RANK.get((e.get("seniority") or "").lower(), 0)
            conf = float(e.get("confidence") or 0)
            return (is_valid, is_personal, sen, conf)

        return max(pool, key=score)

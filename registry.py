"""SOURCE_REGISTRY: 数据驱动的多源编排。

各适配器返回 list[LeadCandidate]; registry 合并、去重、按 score 排序。
加新源 = 注册一个 adapter, 不改 orchestrate 逻辑。
"""
from sources import last30days_intent, gosom, europages, hunter_discover
from sources.base import LeadCandidate  # noqa: F401  (re-export)

# source_key -> adapter spec
SOURCE_REGISTRY = {
    "last30days_intent": {
        "module": last30days_intent,
        "enabled": True,    # 免 key, 已验证 import
        "weight": 1.0,
        "kind": "intent",
    },
    "gosom_maps": {
        "module": gosom,
        "enabled": True,    # 免docker二进制实测稳定 (43%邮箱命中)
        "weight": 1.2,      # 档案源信息更完整, 略加权
        "kind": "archive",
    },
    "europages": {
        "module": europages,
        "enabled": True,    # playwright 过 WAF 实测稳定 (12s 拿 token)
        "weight": 1.1,      # 介于 intent(1.0) 和 gosom(1.2) 之间
        "kind": "archive",
    },
    "hunter_discover": {
        "module": hunter_discover,
        "enabled": True,    # Hunter Discover POST /v2/discover, Free 档不消耗 credits (实测)
        "weight": 1.1,      # 不设 1.5: weight 是 score 乘数, Discover 精度靠 LLM 过滤, 与 europages 同级避免碾压 gosom
        "kind": "archive",
    },
}

# 每次 orchestrate 的采集错误 (源失败不中断整体)
_last_errors: list = []


def orchestrate(query: str, country: str = "", max_results: int = 20,
                sources: list = None) -> list:
    """跑选中的源, 合并去重排序, 返回 list[LeadCandidate]。"""
    global _last_errors
    _last_errors = []

    selected = [
        k for k, v in SOURCE_REGISTRY.items()
        if v["enabled"] and (sources is None or k in sources)
    ]

    pooled = []
    for key in selected:
        spec = SOURCE_REGISTRY[key]
        mod = spec["module"]
        weight = spec["weight"]
        try:
            leads = mod.search(query=query, country=country, max_results=max_results)
        except Exception as e:  # 单源失败不影响其他源
            _last_errors.append(f"{key}: {type(e).__name__}: {e}")
            leads = []
        for lc in leads:
            lc.score = lc.score * weight
            pooled.append(lc)

    # 去重: 按 company_name 归一化 (lowercase + strip)
    seen = set()
    dedup = []
    for lc in pooled:
        k = (lc.company_name or "").strip().lower()
        if k and k in seen:
            continue
        if k:
            seen.add(k)
        dedup.append(lc)

    # 防噪声: 过滤低分 (挡掉 reddit RSS 噪声帖 score=40 基线)
    dedup = [lc for lc in dedup if lc.score >= 50]
    dedup.sort(key=lambda x: x.score, reverse=True)
    return dedup[:max_results]


def last_errors() -> list:
    return list(_last_errors)


def enabled_sources() -> list:
    return [k for k, v in SOURCE_REGISTRY.items() if v["enabled"]]

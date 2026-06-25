"""意图源适配器: last30days (reddit + hackernews)。

挖"采购意向"讨论 —— reddit/hn 上发"求 LED 供应商"的人就是潜在买家。
免 key (纯 stdlib urllib)。产出 LeadCandidate (讨论信号)。

该源需另装 last30days 引擎才可用。引擎不随本仓库分发；未安装时
_L30D_AVAILABLE=False, search() 直接返回空列表, 不影响其他源。
"""
import os
import sys

# 可选依赖: last30days 引擎不随本仓库分发, 缺失时降级为空结果。
# (仅当 _L30D_AVAILABLE=True 时才注入 sys.path, 避免在缺失时污染 sys.path)
_HERE = os.path.dirname(os.path.abspath(__file__))
_L30D_SCRIPTS = os.path.abspath(os.path.join(_HERE, "..", "last30days", "scripts"))

_L30D_AVAILABLE = False
if os.path.isdir(_L30D_SCRIPTS):
    if _L30D_SCRIPTS not in sys.path:
        sys.path.insert(0, _L30D_SCRIPTS)
    try:
        from lib import pipeline, env  # noqa: F401
        _L30D_AVAILABLE = True
    except (ImportError, ModuleNotFoundError):
        _L30D_AVAILABLE = False

# 默认采集的意图源 (均为免 key)
ENABLED_SOURCES = ["reddit", "hackernews"]

SOURCE_TAG = "intent"  # registry 里的 kind


def _ensure_proxy():
    """代理 URL (纯环境变量驱动): LEADGEN_PROXY > HTTPS_PROXY > HTTP_PROXY。

    无任何代理 env 返回 None; 取到则原样返回 (调用方再 setdefault 进 os.environ/config)。
    """
    existing = (os.environ.get("LEADGEN_PROXY")
                or os.environ.get("HTTPS_PROXY")
                or os.environ.get("https_proxy")
                or os.environ.get("HTTP_PROXY")
                or os.environ.get("http_proxy"))
    return existing.strip() if existing else None


def search(query: str, country: str = "", max_results: int = 20,
           depth: str = "default") -> list:
    """跑 last30days, 把 ranked_candidates 映射成 LeadCandidate。

    query: 产品+市场意图, 自然语言 (如 'LED lighting distributor Germany')

    若 last30days 引擎未安装, 直接返回 [] (该源为可选依赖)。
    """
    if not _L30D_AVAILABLE:
        # 引擎未安装: 静默降级, 不影响其他源
        return []

    from lib import pipeline, env
    from .base import LeadCandidate

    # 自动配代理 (MCP 进程无外部代理 env, 这里自包含)
    proxy = _ensure_proxy()
    if proxy:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ.setdefault(k, proxy)

    config = env.get_config()
    if proxy:
        config["HTTPS_PROXY"] = proxy
        config["HTTP_PROXY"] = proxy
    # 主题里塞采购意向词, 帮 last30days planner 捞到买家讨论
    intent_suffix = f" buyer intent {country}".strip() if country else " buyer intent wholesale distributor B2B"
    topic = f"{query} {intent_suffix}".strip()

    report = pipeline.run(
        topic=topic,
        config=config,
        depth=depth,
        requested_sources=ENABLED_SOURCES,
    )

    out = []
    for c in getattr(report, "ranked_candidates", []) or []:
        score = _scale_score(getattr(c, "final_score", 0.0) or 0.0)

        # contact_name 取首个 source_item 的 author (reddit 用户名 = 潜在买家)
        contact = ""
        items = getattr(c, "source_items", None) or []
        if items:
            author = getattr(items[0], "author", None)
            if author:
                contact = str(author)

        snippet = getattr(c, "snippet", "") or getattr(c, "explanation", "") or ""

        out.append(LeadCandidate(
            company_name=(getattr(c, "title", "") or "(unnamed discussion)")[:160],
            website=getattr(c, "url", "") or "",
            country=country,
            contact_name=contact,
            contact_title="buyer (discussion)",
            source=f"{getattr(c, 'source', 'unknown')}_intent",
            search_query=query,
            score=score,
            detail=snippet[:300],
        ))
        if len(out) >= max_results:
            break
    return out


def _scale_score(s: float) -> float:
    """last30days local rerank 的 final_score 范围不确定。
    <=1 视为 0-1 概率分 → 映射到 40-90 (意图信号基线);
    >1 视为已是百分制 → 压缩到 40-90 区间。"""
    if s <= 1.0:
        return 40.0 + s * 50.0
    return max(40.0, min(90.0, s))

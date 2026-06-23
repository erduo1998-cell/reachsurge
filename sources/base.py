"""统一线索数据结构。

两类源各自归一化到 LeadCandidate, 不强行合并原始 schema:
- 意图源 (reddit/hackernews): 产出"采购讨论信号", contact_name=发帖人
- 档案源 (gosom/google-maps): 产出"公司档案", 有完整 website/email/phone
"""
from dataclasses import dataclass, asdict


@dataclass
class LeadCandidate:
    company_name: str
    website: str = ""
    country: str = ""
    city: str = ""
    contact_name: str = ""
    contact_title: str = ""
    email: str = ""
    phone: str = ""
    linkedin_url: str = ""
    buyer_type: str = ""     # distributor/wholesaler/manufacturer/importer (europages)
    source: str = ""          # "reddit_intent" / "hackernews_intent" / "gosom_maps"
    search_query: str = ""
    score: float = 50.0       # 统一 0-100
    detail: str = ""          # 备注 (snippet/why), 仅用于汇总展示, 不入库

    def to_lead_dict(self, user_id: str) -> dict:
        """对齐 save_lead / insert_lead 的字段契约。"""
        d = asdict(self)
        d.pop("detail", None)
        d["user_id"] = user_id
        d["score"] = int(round(max(0.0, min(100.0, float(self.score)))))
        return d

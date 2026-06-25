"""
获客管线 MCP Server — 兼容 Hermes 等 MCP 客户端 / agent 框架的外贸获客工具。

工具列表:
- save_user_config   录入产品/市场/邮箱配置
- get_user_config    查询用户配置
- add_knowledge      添加产品知识到知识库
- search_knowledge   检索产品知识
- save_lead          保存一条线索
- list_leads         列出/筛选线索
- update_lead_status 更新线索状态
- verify_email       验证邮箱有效性（语法+MX+SMTP）
- compose_outreach   撰写开发信（基于知识库+线索信息）
- enrich_lead_emails 批量补全线索邮箱（SMTP验证）

运行方式:
  cd ~/reachsurge && .venv/bin/python mcp_server.py
"""

import os
import sys
import json
import uuid
import asyncio
import logging
import threading
import traceback
from datetime import datetime

# 确保项目根在 sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 加载 profile .env —— MCP 客户端的 env 段通常只透传 HOME/LEADGEN_DATA_DIR,
# HUNTER_API_KEY / http_proxy 等需自行 load_dotenv(LEADGEN_ENV_FILE) 才进得了子进程 environ。
# 出错不阻断启动, 仅相关 provider 降级。
try:
    from dotenv import load_dotenv
    _env_file = os.environ.get("LEADGEN_ENV_FILE", "").strip()
    if _env_file:
        load_dotenv(_env_file)
except Exception:
    pass

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from storage.db import (
    init_db, upsert_user_config, get_user_config, user_exists,
    insert_lead, list_leads, update_lead_status, lead_exists, DATA_DIR,
    create_task, set_task_running, set_task_progress, complete_task,
    fail_task, get_task, count_pending_leads, get_lead, update_lead_intel,
    insert_outreach_record, insert_inquiry, list_inquiries,
)
from storage.rag import add_knowledge, search_knowledge, get_knowledge_count
from tools.email_verify import verify_email_smtp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("leadgen-mcp")

server = Server("leadgen")

# ── Tool definitions ──

TOOLS = [
    Tool(
        name="save_user_config",
        description="录入或更新用户的产品信息、目标市场和邮箱配置。当用户告诉你他做什么产品、目标市场、邮箱信息时调用。",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "用户 ID（兼容各类 MCP 客户端的 chat_id 或 open_id）"},
                "name": {"type": "string", "description": "用户称呼"},
                "industry": {"type": "string", "description": "行业，如 LED灯具、机械配件"},
                "target_markets": {"type": "string", "description": "目标市场，逗号分隔，如 '德国,美国,英国'"},
                "product_description": {"type": "string", "description": "产品描述"},
                "smtp_host": {"type": "string", "description": "SMTP 服务器地址（可选）"},
                "smtp_port": {"type": "integer", "description": "SMTP 端口，默认 587"},
                "smtp_user": {"type": "string", "description": "SMTP 用户名/邮箱"},
                "smtp_password": {"type": "string", "description": "SMTP 密码"},
                "imap_host": {"type": "string", "description": "IMAP 服务器地址（可选）"},
                "imap_user": {"type": "string", "description": "IMAP 用户名/邮箱"},
                "imap_password": {"type": "string", "description": "IMAP 密码"},
                "daily_send_limit": {"type": "integer", "description": "每日发送上限，默认 30"},
            },
            "required": ["user_id"],
        },
    ),
    Tool(
        name="get_user_config",
        description="查询用户已保存的产品信息和配置。",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "用户 ID"},
            },
            "required": ["user_id"],
        },
    ),
    Tool(
        name="add_knowledge",
        description="将产品资料存入知识库，后续写开发信时会检索这些内容。用户发送产品描述、公司介绍等文本时调用。",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "用户 ID"},
                "content": {"type": "string", "description": "产品资料内容"},
                "title": {"type": "string", "description": "资料标题，默认'产品资料'"},
            },
            "required": ["user_id", "content"],
        },
    ),
    Tool(
        name="search_knowledge",
        description="从用户知识库中检索产品相关信息，用于撰写开发信时获取产品细节。",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "用户 ID"},
                "query": {"type": "string", "description": "搜索查询"},
                "n_results": {"type": "integer", "description": "返回结果数，默认 3"},
            },
            "required": ["user_id", "query"],
        },
    ),
    Tool(
        name="save_lead",
        description="保存一条潜在客户线索到数据库。从搜索结果中提取客户信息后调用。",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "用户 ID"},
                "company_name": {"type": "string", "description": "公司名称"},
                "website": {"type": "string", "description": "公司网站"},
                "country": {"type": "string", "description": "国家"},
                "city": {"type": "string", "description": "城市"},
                "contact_name": {"type": "string", "description": "联系人姓名"},
                "contact_title": {"type": "string", "description": "联系人职位"},
                "email": {"type": "string", "description": "邮箱地址"},
                "phone": {"type": "string", "description": "电话"},
                "linkedin_url": {"type": "string", "description": "LinkedIn 链接"},
                "source": {"type": "string", "description": "来源，如 google_maps, linkedin, web_search"},
                "search_query": {"type": "string", "description": "使用的搜索词"},
                "score": {"type": "integer", "description": "评分 0-100，默认 50"},
            },
            "required": ["user_id", "company_name"],
        },
    ),
    Tool(
        name="list_leads",
        description="列出用户已保存的线索，支持按状态筛选。",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "用户 ID"},
                "status": {"type": "string", "description": "筛选状态: new, contacted, replied, interested, not_interested, invalid"},
                "limit": {"type": "integer", "description": "返回条数上限，默认 20"},
            },
            "required": ["user_id"],
        },
    ),
    Tool(
        name="update_lead_status",
        description="更新一条线索的状态。",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "用户 ID"},
                "lead_id": {"type": "string", "description": "线索 ID"},
                "status": {"type": "string", "description": "新状态: new, contacted, replied, interested, not_interested, invalid"},
                "email_status": {"type": "string", "description": "邮箱验证状态: valid, invalid, unknown"},
            },
            "required": ["user_id", "lead_id", "status"],
        },
    ),
    Tool(
        name="verify_email",
        description="验证单个邮箱地址的有效性。检查格式、MX记录和SMTP握手。",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "用户 ID"},
                "email": {"type": "string", "description": "要验证的邮箱地址"},
            },
            "required": ["user_id", "email"],
        },
    ),
    Tool(
        name="compose_outreach",
        description="基于产品知识库生成 B2B 开发信【邮件正文草稿文本】。⚠️只产出邮件正文,不发送邮件、不联系SMTP、不真正触达对方。生成后必须把草稿交给用户确认,发送由用户自己完成。本工具完成后只能对用户说「已生成草稿」,绝不能说「已发送/已发/已联系/发信成功」。会自动检索用户知识库个性化内容。",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "用户 ID"},
                "lead_id": {"type": "string", "description": "目标线索 ID（可选，如已保存）"},
                "company_name": {"type": "string", "description": "目标公司名"},
                "contact_name": {"type": "string", "description": "联系人姓名"},
                "contact_title": {"type": "string", "description": "联系人职位"},
                "product_focus": {"type": "string", "description": "开发信重点突出的产品/卖点"},
                "language": {"type": "string", "description": "邮件语言: en, de, fr, es, zh。默认 en"},
                "tone": {"type": "string", "description": "语气: professional, friendly, direct。默认 professional"},
            },
            "required": ["user_id", "company_name"],
        },
    ),
    Tool(
        name="send_email",
        description="【真发信工具】通过用户已配置的 SMTP 账号真实发送一封开发信给客户。这是唯一合法的发信途径,禁止用 execute_code 自己写 smtplib 脚本。发送成功后自动:①记录到 outreach_records(status=sent) ②若有 lead_id 把线索标记为 contacted。失败也会记录(status=failed+error)。调用前用户必须已用 save_user_config 配好 SMTP。",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "用户 ID"},
                "to_email": {"type": "string", "description": "收件人邮箱地址"},
                "subject": {"type": "string", "description": "邮件主题"},
                "body": {"type": "string", "description": "邮件正文（纯文本）"},
                "lead_id": {"type": "string", "description": "关联线索 ID（可选，传则发信成功后自动标记该线索 contacted）"},
            },
            "required": ["user_id", "to_email", "subject", "body"],
        },
    ),
    Tool(
        name="check_inbox",
        description="【收信工具】通过用户已配置的 IMAP 账号拉取最新收到的邮件,入库到 inquiries 表,用于发现客户回复。自动按 IMAP UID 去重(不重复入库)。调用前用户必须已用 save_user_config 配好 IMAP。用户说'看谁回复了/查收件箱/有没有新邮件'时调用。",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "用户 ID"},
                "limit": {"type": "integer", "description": "最多拉取新邮件条数,默认 20"},
            },
            "required": ["user_id"],
        },
    ),
    Tool(
        name="search_customers",
        description="搜索海外客户线索并自动入库。融合多源采集(意图源: reddit/hackernews 上的采购讨论=潜在买家; 档案源: Google Maps 经销商档案)。每条线索存为 status=new。这是获客SOP第二步, 用户说'搜客户/找买家'时调用。",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "用户 ID"},
                "query": {"type": "string", "description": "搜索意图, 产品+市场, 自然语言。例: 'LED lighting distributor Germany'"},
                "country": {"type": "string", "description": "目标国家(可选), 聚焦地域。例: Germany, USA"},
                "max_results": {"type": "integer", "description": "线索条数上限, 默认 20"},
            },
            "required": ["user_id", "query"],
        },
    ),
    Tool(
        name="enrich_lead_emails",
        description="批量为无邮箱的线索补全邮箱并做SMTP验证。按网站域名查MX做RCPT探测：命中的标verified填真实前缀邮箱；验不了的填info@标guessed；catch-all域标catchall。只补全email为空的行（绝不覆盖已有/已验证邮箱），幂等。search_customers入库新线索后可选调用以提升邮箱覆盖率。注意SMTP探测慢（约10秒/条），用limit控制批量，默认20。",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "用户 ID"},
                "limit": {"type": "integer", "description": "本次处理的线索条数上限（每条约10秒）。默认 20"},
            },
            "required": ["user_id"],
        },
    ),
    Tool(
        name="importyeti_lookup",
        description="查询美国海关提单(ImportYeti)，验证某公司是否真实从中国进口、进口什么(含LED相关HS code 9405/8541.41/8539.52)。返回中国采购占比、LED进口迹象、中国供应商。用于验真候选买家是否真实采购方。注意：仅美国市场有提单数据，数据滞后到2023年，中小公司可能无记录；europages/gosom拉的欧洲经销商多数查不到(欧洲非美国提单)。输入公司名(自动搜slug)或已知slug。",
        inputSchema={
            "type": "object",
            "properties": {
                "company_name": {"type": "string", "description": "公司名(自动解析slug)"},
                "company_slug": {"type": "string", "description": "已知ImportYeti slug则直接传，省一次搜索"},
            },
            "required": ["company_name"],
        },
    ),
    Tool(
        name="social_profile_lookup",
        description="免认证抓 TikTok / Instagram 公开 profile（粉丝数、bio、网站、邮箱）。用于当潜在客户/创作者在社交平台有公开账号时补全联系方式。查询类工具，不写库（返回结果给上层判断）。TikTok 走 SSR JSON 稳定；Instagram 走移动 UA HTML 解析，可能降级（标 degraded=True 则不可信需重试）。代理出口必须美国节点（TikTok 对香港节点 302 跳转 /hk/about）。",
        inputSchema={
            "type": "object",
            "properties": {
                "platform": {"type": "string", "description": "平台: tiktok 或 instagram"},
                "username": {"type": "string", "description": "用户名（不带 @，自动 strip）"},
            },
            "required": ["platform", "username"],
        },
    ),
    Tool(
        name="get_task_status",
        description="查询异步任务(如邮箱补充 enrich_lead_emails)的进度和结果。enrich_lead_emails 现在是异步任务: 调用后立即返回 task_id, 真正处理在后台进行(SMTP 探测慢)。用户问'邮箱补充好了吗''查进度''好了没'或给了 task_id 时调用此工具。返回 status(pending/running/done/failed)、进度 processed/total、结果摘要(verified/guessed 等计数)或错误信息。",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "用户 ID"},
                "task_id": {"type": "string", "description": "enrich_lead_emails 返回的 task_id"},
            },
            "required": ["user_id", "task_id"],
        },
    ),
    Tool(
        name="enrich_company_profile",
        description="对一条线索做深度公司调研: 深抓其官网(about/products/team), 用 DeepSeek 产出公司画像+合作可能性判断+信号分级, 写回线索详情。当 gosom/europages 等源给的线索只有公司名、信息密度不够、判不了值不值得发开发信时调用本工具。结果存 signal_level(high/medium/low/none)+company_intel。注意: 只抓官网不联网; 中国采购痕迹用 importyeti_lookup 另查。单条约 15-40 秒。",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "用户 ID"},
                "lead_id": {"type": "string", "description": "线索 ID(优先): 传则从库读公司名/官网, 调研完写回该线索"},
                "company_name": {"type": "string", "description": "公司名(无 lead_id 时用)"},
                "website": {"type": "string", "description": "公司官网(有则直接抓, 无则信息密度低)"},
                "country": {"type": "string", "description": "国家(可选)"},
            },
            "required": ["user_id"],
        },
    ),
    Tool(
        name="osm_overpass_search",
        description="通过 OpenStreetMap Overpass API 搜海外商户线索 (经销商/批发商)。直连免key公共端点(号池轮换), 原生提取 email/website/phone。最适合找带官网和邮箱的本地经销商 (OSM shop/office tag 比搜索引擎黄页更结构化)。返回线索并入库 status=new。目标为某国本地经销商/分销商且想要邮箱时调用。例: 德国照明经销商、美国电子批发商。基于号池(KeyPool): 首次自动 bootstrap 6 个公共端点, 单端点限流自动切换。单次约 30-40 秒。",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "用户 ID"},
                "query": {"type": "string", "description": "品类搜索意图。含 LED/light/灯→灯具店; wholesale/批发→office=wholesale; electric→电工。例: 'LED lighting distributor'"},
                "country": {"type": "string", "description": "目标国家(中英文/ISO2)。例: Germany/德国/DE"},
                "max_results": {"type": "integer", "description": "返回条数上限, 默认 15"},
            },
            "required": ["user_id", "query"],
        },
    ),
    Tool(
        name="keypool_status",
        description="查询 API 号池状态: 各 provider (overpass/hunter/apollo...) 的 key 数量、active/exhausted、配额用量、近期 usage_log、代理池。用于运维和验证号池投喂/轮换是否生效。投喂新号后可用本工具确认产能叠加。",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "用户 ID"},
            },
            "required": ["user_id"],
        },
    ),
    Tool(
        name="search_leads",
        description="【统一发现入口 / 首选】找海外客户线索时调本工具,不要直接调 search_customers/osm_overpass_search/importyeti_lookup/social_profile_lookup。按意图自动路由到对的源: 某国本地经销商/批发商(要邮箱)→ OSM+gosom+hunter+serpapi_maps 多源; 验证某公司真进口过→ ImportYeti 海关; 找品牌/某人社交联系方式→ TikTok/IG; 展会展商→ Tavily 全网搜展商+LLM提取。结果合并、跨源去重、过 LLM 精度过滤(MediaMarkt/Saturn/占位邮箱等一眼假标 invalid),返回存活线索摘要+各源命中数+过滤丢弃数。无法判定意图时默认走经销商路由(最常见)。用户说'搜客户/找买家/找经销商/验这家公司是不是真买家/查某人社交/搜某展会展商'时调本工具。",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "用户 ID"},
                "query": {"type": "string", "description": "搜索意图文本(品类+市场/公司名/品牌@用户名),自然语言。例: 'LED lighting distributor Germany' / 'Walmart 是不是真进口LED' / '@khaby.lame TikTok 联系方式'"},
                "country": {"type": "string", "description": "目标国家(可选, 经销商意图时聚焦地域)。例: Germany, USA"},
                "intent": {"type": "string", "description": "显式覆盖自动意图判定(可选): distributor(经销商/批发商) | customs_verify(海关验真) | social(社交联系方式) | exhibition(展会展商)。不传则按 query 关键词自动判定, 无法判定兜底 distributor"},
                "max_results": {"type": "integer", "description": "各源返回条数上限, 默认 15"},
            },
            "required": ["user_id", "query"],
        },
    ),
]


# ── Tool handlers ──

@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    logger.info(f"Tool called: {name}, args: {json.dumps(arguments, ensure_ascii=False)[:200]}")

    try:
        if name == "save_user_config":
            result = _handle_save_user_config(arguments)
        elif name == "get_user_config":
            result = _handle_get_user_config(arguments)
        elif name == "add_knowledge":
            result = _handle_add_knowledge(arguments)
        elif name == "search_knowledge":
            result = _handle_search_knowledge(arguments)
        elif name == "save_lead":
            result = _handle_save_lead(arguments)
        elif name == "list_leads":
            result = _handle_list_leads(arguments)
        elif name == "update_lead_status":
            result = _handle_update_lead_status(arguments)
        elif name == "verify_email":
            result = _handle_verify_email(arguments)
        elif name == "compose_outreach":
            result = _handle_compose_outreach(arguments)
        elif name == "send_email":
            result = _handle_send_email(arguments)
        elif name == "check_inbox":
            result = await asyncio.to_thread(_handle_check_inbox, arguments)
        elif name == "search_customers":
            result = await asyncio.to_thread(_handle_search_customers, arguments)
        elif name == "enrich_lead_emails":
            result = _handle_enrich_lead_emails(arguments)
        elif name == "importyeti_lookup":
            result = await asyncio.to_thread(_handle_importyeti_lookup, arguments)
        elif name == "social_profile_lookup":
            result = await asyncio.to_thread(_handle_social_profile_lookup, arguments)
        elif name == "enrich_company_profile":
            result = await asyncio.to_thread(_handle_enrich_company_profile, arguments)
        elif name == "osm_overpass_search":
            result = await asyncio.to_thread(_handle_osm_overpass_search, arguments)
        elif name == "search_leads":
            result = await asyncio.to_thread(_handle_search_leads, arguments)
        elif name == "keypool_status":
            result = _handle_keypool_status(arguments)
        elif name == "get_task_status":
            result = _handle_get_task_status(arguments)
        else:
            result = f"❌ 未知工具: {name}"

        return [TextContent(type="text", text=result)]

    except Exception as e:
        logger.exception(f"Tool {name} failed")
        return [TextContent(type="text", text=f"❌ 工具执行失败: {type(e).__name__}: {e}")]


# ── Handler implementations ──

def _handle_save_user_config(args: dict) -> str:
    user_id = args["user_id"]
    markets = [m.strip() for m in args.get("target_markets", "").split(",") if m.strip()] if args.get("target_markets") else []

    config = {
        "user_id": user_id,
        "feishu_open_id": user_id,
        "name": args.get("name", ""),
        "industry": args.get("industry", ""),
        "target_markets": markets,
        "product_description": args.get("product_description", ""),
        "smtp_host": args.get("smtp_host", ""),
        "smtp_port": args.get("smtp_port", 587),
        "smtp_user": args.get("smtp_user", ""),
        "smtp_password": args.get("smtp_password", ""),
        "imap_host": args.get("imap_host", ""),
        "imap_user": args.get("imap_user", ""),
        "imap_password": args.get("imap_password", ""),
        "daily_send_limit": args.get("daily_send_limit", 30),
    }

    upsert_user_config(config)

    lines = ["✅ 已保存你的信息："]
    if config["name"]:
        lines.append(f"• 称呼：{config['name']}")
    if config["industry"]:
        lines.append(f"• 行业：{config['industry']}")
    if markets:
        lines.append(f"• 目标市场：{', '.join(markets)}")
    if config["product_description"]:
        desc = config["product_description"]
        lines.append(f"• 产品描述：{desc[:200]}{'...' if len(desc) > 200 else ''}")
    if config["smtp_host"]:
        lines.append(f"• 发信邮箱：{config['smtp_user']} ({config['smtp_host']})")
    if config["imap_host"]:
        lines.append(f"• 收信邮箱：{config['imap_user']} ({config['imap_host']})")

    return "\n".join(lines)


def _handle_get_user_config(args: dict) -> str:
    user_id = args["user_id"]
    config = get_user_config(user_id)

    if not config or not config.get("product_description"):
        return "📝 你还没有录入产品信息。请告诉我：\n• 你做什么产品？\n• 目标市场是哪些国家？\n• 有邮箱的话也可以一并配置（SMTP/IMAP）"

    lines = ["📋 你的当前配置："]
    if config.get("name"):
        lines.append(f"• 称呼：{config['name']}")
    if config.get("industry"):
        lines.append(f"• 行业：{config['industry']}")
    markets = config.get("target_markets", [])
    if markets:
        if isinstance(markets, str):
            lines.append(f"• 目标市场：{markets}")
        else:
            lines.append(f"• 目标市场：{', '.join(markets)}")
    if config.get("product_description"):
        lines.append(f"• 产品：{config['product_description'][:300]}")
    if config.get("smtp_host"):
        lines.append(f"• 发信：{config['smtp_user']} ({config['smtp_host']}:{config.get('smtp_port', 587)})")
    if config.get("imap_host"):
        lines.append(f"• 收信：{config['imap_user']} ({config['imap_host']})")

    kb_count = get_knowledge_count(user_id)
    lines.append(f"• 知识库条目：{kb_count} 条")

    return "\n".join(lines)


def _handle_add_knowledge(args: dict) -> str:
    user_id = args["user_id"]
    content = args["content"]
    title = args.get("title", "产品资料")

    doc_id = uuid.uuid4().hex[:12]
    add_knowledge(
        user_id=user_id,
        documents=[content],
        metadatas=[{"title": title, "type": "product_info"}],
        ids=[doc_id],
    )

    count = get_knowledge_count(user_id)
    return f"✅ 已存入知识库（{doc_id}）\n📁 标题：{title}\n📊 知识库共 {count} 条\n\n后续写开发信时会自动引用这些内容。"


def _handle_search_knowledge(args: dict) -> str:
    user_id = args["user_id"]
    query = args["query"]
    n_results = args.get("n_results", 3)

    results = search_knowledge(user_id, query, n_results)

    if not results:
        return "📭 未找到相关知识。请先录入产品资料。"

    lines = [f"📚 找到 {len(results)} 条相关知识："]
    for i, r in enumerate(results, 1):
        lines.append(f"\n--- 结果 {i} ---")
        lines.append(r[:500])

    return "\n".join(lines)


def _handle_save_lead(args: dict) -> str:
    user_id = args["user_id"]
    lead_id = uuid.uuid4().hex[:12]

    lead = {
        "lead_id": lead_id,
        "user_id": user_id,
        "company_name": args["company_name"],
        "website": args.get("website", ""),
        "country": args.get("country", ""),
        "city": args.get("city", ""),
        "contact_name": args.get("contact_name", ""),
        "contact_title": args.get("contact_title", ""),
        "email": args.get("email", ""),
        "phone": args.get("phone", ""),
        "linkedin_url": args.get("linkedin_url", ""),
        "source": args.get("source", ""),
        "search_query": args.get("search_query", ""),
        "score": args.get("score", 50),
        "status": "new",
    }

    insert_lead(lead)

    contact = lead["contact_name"] or "未知"
    email = lead["email"] or "未知"
    return f"✅ 已保存线索 #{lead_id}\n• 公司：{lead['company_name']}\n• 联系人：{contact}\n• 邮箱：{email}\n• 国家：{lead['country']}\n• 来源：{lead['source']}"


def _handle_list_leads(args: dict) -> str:
    user_id = args["user_id"]
    status = args.get("status")
    limit = args.get("limit", 20)

    leads = list_leads(user_id, status=status, limit=limit)

    if not leads:
        return "📭 暂无保存的线索。"

    lines = [f"📊 共 {len(leads)} 条线索："]
    for lead in leads:
        status_icon = {"new": "🆕", "contacted": "📨", "replied": "📩", "interested": "🔥", "not_interested": "❌", "invalid": "🚫"}.get(lead.get("status", ""), "❓")
        lines.append(
            f"\n{status_icon} {lead['lead_id'][:8]} | {lead['company_name']}"
            f"\n   联系人：{lead.get('contact_name', '未知')} | 邮箱：{lead.get('email', '未知')}"
            f"\n   国家：{lead.get('country', '未知')} | 状态：{lead.get('status', 'new')}"
        )

    return "\n".join(lines)


def _handle_update_lead_status(args: dict) -> str:
    user_id = args["user_id"]
    lead_id = args["lead_id"]
    status = args["status"]
    email_status = args.get("email_status")

    update_lead_status(user_id, lead_id, status, email_status=email_status)
    return f"✅ 已更新线索 {lead_id[:8]} 状态为: {status}"


def _handle_verify_email(args: dict) -> str:
    email = args["email"]
    result = verify_email_smtp(email)
    return result


def _handle_compose_outreach(args: dict) -> str:
    user_id = args["user_id"]
    company_name = args["company_name"]
    contact_name = args.get("contact_name", "")
    contact_title = args.get("contact_title", "")
    product_focus = args.get("product_focus", "")
    language = args.get("language", "en")
    tone = args.get("tone", "professional")

    # 获取用户配置
    config = get_user_config(user_id)
    if not config:
        return "⚠️ 请先录入产品信息和目标市场（使用 save_user_config）。"

    # 检索知识库
    search_terms = product_focus or config.get("product_description", "")
    knowledge_results = search_knowledge(user_id, search_terms[:200], n_results=3)

    # 组装邮件草稿所需的上下文
    product_desc = config.get("product_description", "")
    industry = config.get("industry", "")
    sender_name = config.get("name", "")

    kb_context = ""
    if knowledge_results:
        kb_context = "\n\n--- 产品知识库参考 ---\n" + "\n---\n".join(knowledge_results)

    salutation = f"Hi {contact_name}" if contact_name else "Dear Sir/Madam"
    if contact_title:
        salutation = f"Dear {contact_name} ({contact_title})"

    tone_instruction = {
        "professional": "专业、正式、简洁",
        "friendly": "友好、亲和、有温度",
        "direct": "直接、干练、不废话",
    }.get(tone, "专业、正式、简洁")

    lang_instruction = {
        "en": "ENGLISH",
        "de": "GERMAN",
        "fr": "FRENCH",
        "es": "SPANISH",
        "zh": "CHINESE",
    }.get(language, "ENGLISH")

    # 构建详细的写邮件的上下文，返回给调用方（agent 框架），由其调 LLM 完成
    prompt = f"""请用{lang_instruction}撰写一封B2B开发信，语气：{tone_instruction}。

发信人信息：
- 行业：{industry}
- 产品：{product_desc[:500]}

收信人信息：
- 公司：{company_name}
- 联系人：{contact_name} / {contact_title}
- 重点产品卖点：{product_focus or '通用介绍'}

知识库参考：
{kb_context}

要求：
1. 开头简短自我介绍（公司+产品一句话）
2. 1-2段说明产品价值/差异化（引用知识库中的具体细节）
3. 结尾CTA（询问是否方便简短通话/发产品目录）
4. 邮件主题单独标注为 "Subject: ..."
5. 整封邮件控制在150词以内（英文）或200字以内（中文）
6. 称呼使用：{salutation}
7. 签名使用：{sender_name or '[Your Name]'}
"""

    return prompt


def _handle_send_email(args: dict) -> str:
    """真发信。读 user_config 的 SMTP 配置, smtplib 发送, 记 outreach_records + 标 contacted。"""
    user_id = args["user_id"]
    to_email = args["to_email"]
    subject = args["subject"]
    body = args["body"]
    lead_id = args.get("lead_id", "")

    config = get_user_config(user_id)
    if not config or not config.get("smtp_host"):
        return "⚠️ 还没配置发信邮箱。请先用 save_user_config 配置 SMTP（smtp_host/smtp_user/smtp_password）。"

    smtp_host = config["smtp_host"]
    smtp_port = config.get("smtp_port") or 587
    smtp_user = config.get("smtp_user", "")
    smtp_password = config.get("smtp_password", "")
    sender_name = config.get("name", "") or "ReachSurge"

    if not smtp_user or not smtp_password:
        return "⚠️ SMTP 用户名或密码缺失，请用 save_user_config 补全 smtp_user / smtp_password。"

    import smtplib
    from email.mime.text import MIMEText
    from email.utils import formataddr, formatdate, make_msgid

    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = formataddr((sender_name, smtp_user))
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()

    try:
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30)
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
            server.ehlo()
            server.starttls()
            server.ehlo()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, [to_email], msg.as_string())
        server.quit()
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        rid = insert_outreach_record(user_id, lead_id, subject, body, status="failed", error_message=err)
        return f"❌ 发送失败（已记录 #{rid}）：{err}"

    rid = insert_outreach_record(user_id, lead_id, subject, body, status="sent")
    if lead_id:
        try:
            update_lead_status(user_id, lead_id, "contacted")
        except Exception:
            pass
    return f"✅ 已发送给 {to_email}（记录 #{rid}）"


def _handle_check_inbox(args: dict) -> str:
    """收信。读 user_config 的 IMAP 配置, imaplib 拉新邮件, 按 UID 去重, 入库 inquiries。"""
    user_id = args["user_id"]
    limit = args.get("limit", 20)

    config = get_user_config(user_id)
    if not config or not config.get("imap_host"):
        return "⚠️ 还没配置收信邮箱。请先用 save_user_config 配置 IMAP（imap_host/imap_user/imap_password）。"

    imap_host = config["imap_host"]
    imap_port = config.get("imap_port") or 993
    imap_user = config.get("imap_user", "")
    imap_password = config.get("imap_password", "")
    if not imap_user or not imap_password:
        return "⚠️ IMAP 用户名或密码缺失，请用 save_user_config 补全 imap_user / imap_password。"

    import os as _os, json as _json
    safe_uid = user_id.replace("/", "_").replace("\\", "_").replace(":", "_")
    seen_path = _os.path.join(str(DATA_DIR), f"inbox_seen_{safe_uid}.json")
    seen = set()
    if _os.path.exists(seen_path):
        try:
            seen = set(_json.loads(open(seen_path, encoding="utf-8").read()))
        except Exception:
            seen = set()

    import imaplib
    import email
    from email.header import decode_header

    new_count = 0
    summaries = []
    try:
        mail = imaplib.IMAP4_SSL(imap_host, imap_port)
        mail.login(imap_user, imap_password)
        mail.select("INBOX")
        # 用 UID 而非 sequence number 去重：sequence 会随邮箱删信/清理移位，
        # 同一封已处理邮件下次拿到新序号 → 不命中 seen → 重复入库。UID 恒定。
        typ, data = mail.uid("search", None, "ALL")
        ids = data[0].split() if data and data[0] else []
        recent = ids[-(limit * 3):] if ids else []
        for uid in recent:
            uid_s = uid.decode() if isinstance(uid, bytes) else str(uid)
            if uid_s in seen:
                continue
            typ, msg_data = mail.uid("fetch", uid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            from_hdr = msg.get("From", "")
            subj_hdr = msg.get("Subject", "")
            try:
                parts = decode_header(subj_hdr)
                subject = "".join(
                    (p[0].decode(p[1] or "utf-8", "replace") if isinstance(p[0], bytes) else str(p[0]))
                    for p in parts
                )
            except Exception:
                subject = subj_hdr
            body_text = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        payload = part.get_payload(decode=True)
                        if payload:
                            body_text = payload.decode(part.get_content_charset() or "utf-8", "replace")
                        break
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    body_text = payload.decode(msg.get_content_charset() or "utf-8", "replace")
            body_text = (body_text or "").strip()[:2000]
            insert_inquiry(user_id, from_address=from_hdr, subject=subject, body=body_text)
            seen.add(uid_s)
            new_count += 1
            summaries.append(f"• {from_hdr} — {subject}")
            if new_count >= limit:
                break
        try:
            mail.logout()
        except Exception:
            pass
    except Exception as e:
        return f"❌ 收信失败：{type(e).__name__}: {e}"

    try:
        with open(seen_path, "w", encoding="utf-8") as f:
            _json.dump(sorted(seen), f)
    except Exception:
        pass

    if new_count == 0:
        return "📭 没有新回复。"
    return f"📬 收到 {new_count} 封新邮件：\n" + "\n".join(summaries)


def _handle_search_customers(args: dict) -> str:
    import os as _os, sys as _sys, uuid as _uuid
    # 确保 ~/reachsurge 在 path (能 import registry)
    _here = _os.path.dirname(_os.path.abspath(__file__))
    if _here not in _sys.path:
        _sys.path.insert(0, _here)

    user_id = args["user_id"]
    query = args["query"]
    country = args.get("country", "")
    max_results = args.get("max_results", 20)

    try:
        from registry import orchestrate, last_errors, enabled_sources
        leads = orchestrate(query=query, country=country, max_results=max_results)
    except Exception as e:
        return f"❌ 搜索失败: {type(e).__name__}: {e}"

    if not leads:
        errs = last_errors()
        msg = f"🔍 未找到线索。查询: {query}"
        if errs:
            msg += "\n采集错误:\n  " + "\n  ".join(errs)
        msg += "\n建议: 换更具体的产品词/市场, 或稍后重试。"
        return msg

    saved = 0
    skipped = 0
    country_count = {}
    for lc in leads:
        lead = lc.to_lead_dict(user_id)
        # 防重复入库: 同用户同源同公司已存在则跳过（避免重复触发累积）
        if lead_exists(user_id, lead.get("company_name", ""), lead.get("source", "")):
            skipped += 1
            continue
        lead["lead_id"] = _uuid.uuid4().hex[:12]
        lead["status"] = "new"
        try:
            insert_lead(lead)
            saved += 1
            cc = lc.country or "未知"
            country_count[cc] = country_count.get(cc, 0) + 1
        except Exception:
            pass

    dup_note = f", 跳过重复 {skipped} 条" if skipped else ""
    lines = [f"🔍 搜索完成: 找到 {len(leads)} 条线索, 入库 {saved} 条{dup_note}"]
    for cc, n in sorted(country_count.items(), key=lambda x: -x[1]):
        lines.append(f"  • {cc}: {n} 条")
    lines.append("\nTOP 线索预览:")
    for lc in leads[:5]:
        web = f" {lc.website[:40]}" if lc.website else ""
        lines.append(f"  - [{lc.source}] {lc.company_name} (score {int(lc.score)}){web}")
    lines.append("\n下一步: verify_email 验邮箱 → compose_outreach 写开发信")
    return "\n".join(lines)


def _format_count(n):
    """大数字段(likes/followers)可读化。

    MCP 客户端传输层对大 int 做 int32 强转可能产生负数; 这里先按 uint32 重解释回正,
    再格式化为中文可读字符串(亿/万)。None 原样返回。非 int 尝试转 int, 失败原样返回。
    """
    if n is None:
        return None
    try:
        v = int(n)
    except (TypeError, ValueError):
        return n
    if v < 0:
        v = v & 0xFFFFFFFF
        if v >= 0x80000000:
            # 仍可能是真负数或更高位截断; 兜底按 uint32 已是合理猜测, 直接用
            pass
    if v >= 100_000_000:
        s = f"{v/100_000_000:.1f}亿".replace(".0亿", "亿")
    elif v >= 10_000:
        s = f"{v/10_000:.1f}万".replace(".0万", "万")
    else:
        s = str(v)
    return s


def _enrich_worker(task_id: str, user_id: str, limit: int):
    """daemon 线程: 真正跑 enrich_emails。worker 内异常全 catch -> fail_task。

    进度策略(MVP): 0 -> 100。不动 enrich_emails 内部逻辑, 调用前 set_running,
    正常返回后 complete_task(result_summary=counts); 异常 fail_task。
    worker 跑多久无所谓(call_tool 已秒回, 客户端不再因 enrich 超时)。
    """
    try:
        from sources.email_enrich import enrich_emails
        set_task_running(task_id, user_id)
        logger.info(f"[enrich_worker] start task={task_id} user={user_id} limit={limit}")
        res = enrich_emails(user_id, limit=limit)
        summary = json.dumps({
            "total": res.get("total"),
            "updated": res.get("updated"),
            "counts": res.get("counts", {}),
        }, ensure_ascii=False)
        complete_task(task_id, user_id, summary)
        logger.info(f"[enrich_worker] done task={task_id} summary={summary}")
    except Exception as e:
        err = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        try:
            fail_task(task_id, user_id, err[:4000])
        except Exception:
            logger.exception(f"[enrich_worker] fail_task itself failed task={task_id}")
        logger.exception(f"[enrich_worker] FAILED task={task_id}")


def _handle_enrich_lead_emails(args: dict) -> str:
    """异步化(方案B): 立即创建 task + 起 daemon worker + 秒回 task_id。

    user_id 由 MCP 客户端的安全插件/中间件强制注入(此处 args 里的 user_id 即真实用户)。
    整个 handler <1s 返回, 永不触发 300s MCP 超时(P0 治本)。
    worker 独立 daemon 线程, 不再让 mcp 子进程因 call_tool 阻塞而空跑(P1 消解)。
    """
    user_id = args["user_id"]
    limit = args.get("limit", 20)

    # 预估 total: 优先 count_pending_leads(真实候选数), 上限 limit
    try:
        pending = count_pending_leads(user_id)
    except Exception:
        pending = 0
    total = min(pending, limit) if pending else 0
    if pending == 0:
        return ("📭 当前没有待补全邮箱的线索(所有有网站的线索都已有 verified/existing/catchall 邮箱, 或无网站)。"
                "\n提示: 先用 search_customers 拉新线索后再补充邮箱。")

    task_id = create_task(user_id, task_type="enrich_lead_emails", limit=limit, total=total)

    t = threading.Thread(target=_enrich_worker, args=(task_id, user_id, limit), daemon=True)
    t.start()

    est_minutes = max(1, round(total * 10 / 60))
    return (
        f"🚀 邮箱补充任务已创建，正在后台处理。\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"task_id: {task_id}\n"
        f"状态: running (后台 SMTP 探测中)\n"
        f"待处理: {total} 条线索 (每条约10秒，预计约 {est_minutes} 分钟)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"我现在就能继续帮你做别的。完成后查询结果请说 \"查邮箱补充进度\" 或直接给我这个 task_id: {task_id}，"
        f"我会调用 get_task_status 给你看 verified/guessed 等计数。"
    )


def _handle_get_task_status(args: dict) -> str:
    user_id = args["user_id"]
    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        return "❌ 缺少 task_id"
    try:
        t = get_task(task_id, user_id)
    except Exception as e:
        return f"❌ 查询失败: {type(e).__name__}: {e}"
    if not t:
        # 跨用户查不到或 task_id 错 -> 不泄漏存在性, 统一提示
        return f"❌ 未找到 task_id={task_id} (属于当前用户的任务)。请确认 task_id 正确。"

    status = t.get("status")
    processed = t.get("processed") or 0
    total = t.get("total") or 0
    pct = int(processed / total * 100) if total else 0

    status_emoji = {"pending": "⏳", "running": "🔄", "done": "✅", "failed": "❌"}.get(status, "❓")
    lines = [f"{status_emoji} 任务状态: {status}  ({processed}/{total} = {pct}%)"]
    lines.append(f"task_id: {t.get('id')}")
    lines.append(f"类型: {t.get('task_type')}")
    lines.append(f"创建: {t.get('created_at')}  更新: {t.get('updated_at')}")

    if status == "done":
        try:
            summary = json.loads(t.get("result_summary") or "{}")
            total_s = summary.get("total")
            updated = summary.get("updated")
            c = summary.get("counts", {})
            lines.append("")
            lines.append(f"📧 邮箱补全完成：处理 {total_s} 条，更新 {updated} 条")
            labels = {
                "verified": "✅ verified（SMTP实锤）",
                "scraped": "🌐 scraped（网站深抓）",
                "guessed": "📝 guessed（info@猜测）",
                "catchall": "🌀 catchall（catch-all域）",
                "no_mx": "❌ no_mx（无MX）",
                "no_domain": "❌ no_domain（无网站）",
            }
            for k in ("verified", "scraped", "guessed", "catchall", "no_mx", "no_domain"):
                if c.get(k):
                    lines.append(f"  • {labels[k]}：{c[k]} 条")
            lines.append("建议：先发 verified/existing；guessed 走小批量 warm-up 监测 bounce。")
        except Exception:
            lines.append(f"结果摘要(原始): {t.get('result_summary')}")
    elif status == "failed":
        lines.append(f"错误: {t.get('error')}")
    elif status == "running":
        lines.append("后台仍在 SMTP 探测中，稍后再查。")
    return "\n".join(lines)


def _handle_importyeti_lookup(args: dict) -> str:
    from sources.customs_importyeti import lookup
    name = (args.get("company_name") or "").strip()
    slug = (args.get("company_slug") or "").strip() or None
    if not name:
        return "❌ 缺少 company_name"
    try:
        r = lookup(name, slug)
    except Exception as e:
        return f"❌ 查询失败: {type(e).__name__}: {e}"
    if not r.get("found"):
        return f"❌ {r.get('error', '未在 ImportYeti 找到该公司')}\n\n提示：仅美国市场有提单数据，且数据滞后到2023年。欧洲经销商/中小公司可能无记录。"
    lines = [
        f"📊 ImportYeti 海关提单查询结果",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"公司: {r.get('company_name') or r.get('slug')}",
        f"页面: {r.get('url')}",
        f"",
        f"🇨🇳 中国采购占比: {r.get('china_share') or '无数据'}",
        f"📦 总海运票数: {r.get('shipments_total') or '无数据'}",
    ]
    if r.get("has_led_hs"):
        lines.append(f"💡 LED 进口迹象: ✅ 有 (HS: {', '.join(r.get('led_hs_codes', []))})")
    else:
        lines.append(f"💡 LED 进口迹象: ❌ 未在 Top HS 中发现 LED 相关 (9405/8541/8539)")
    sups = r.get("top_suppliers_cn") or []
    if sups:
        lines.append(f"")
        lines.append(f"🏭 中国供应商 (前 {len(sups)} 个):")
        for s in sups:
            lines.append(f"  • {s}")
    lines.append(f"")
    lines.append(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"解读: china_share 高=真实从中国采购的进口商; has_led_hs=有LED进货记录(强信号);")
    lines.append(f"      无 LED HS 但 china_share 高=采购中国货但非LED(可结合其业务判断是否拓展LED品类)。")
    return "\n".join(lines)


def _handle_social_profile_lookup(args: dict) -> str:
    from sources.social_scout import social_profile_lookup
    platform = (args.get("platform") or "").strip()
    username = (args.get("username") or "").strip()
    if not platform:
        return "❌ 缺少 platform（支持: tiktok, instagram）"
    if not username:
        return "❌ 缺少 username"
    try:
        r = social_profile_lookup(platform, username)
    except Exception as e:
        return f"❌ 抓取失败: {type(e).__name__}: {e}"

    if r.get("error") and not r.get("followers") and r.get("followers") != 0:
        return (
            f"❌ {r.get('error')}\n"
            f"platform: {r.get('platform', platform)} | username: {r.get('username', username)}\n"
            f"status_code: {r.get('status_code')}\n"
            f"提示: IG 降级可稍后重试；TikTok 302 /hk/about=代理节点非美国，换 Clash Verge 美国节点。"
        )

    degraded = r.get("degraded", False)
    head = "⚠️ Instagram 降级（数据不可信，建议稍后重试）" if degraded else "✅ 抓取成功"
    lines = [
        head,
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"平台: {r.get('platform')}",
        f"用户名: @{r.get('username')}",
    ]
    if r.get("full_name"):
        lines.append(f"昵称: {r.get('full_name')}")
    if r.get("verified"):
        lines.append(f"认证: ✅ 已认证")
    if r.get("followers") is not None:
        lines.append(f"粉丝: {_format_count(r.get('followers'))}")
    if r.get("following") is not None:
        lines.append(f"关注: {_format_count(r.get('following'))}")
    if r.get("likes") is not None:
        lines.append(f"获赞: {_format_count(r.get('likes'))}")
    if r.get("videos") is not None:
        lines.append(f"视频数: {r.get('videos')}")
    if r.get("posts") is not None:
        lines.append(f"帖子数: {r.get('posts')}")
    if r.get("bio"):
        bio = r.get("bio")
        lines.append(f"Bio: {bio[:200]}{'...' if len(bio) > 200 else ''}")
    if r.get("website"):
        lines.append(f"网站: {r.get('website')}")
    if r.get("email"):
        lines.append(f"邮箱: {r.get('email')}  (可继续 verify_email 实锤)")
    else:
        lines.append(f"邮箱: (Bio/Website 未暴露邮箱；如需邮箱可 enrich_lead_emails 按网站域名猜测验证)")
    lines.append(f"profile_url: {r.get('profile_url')}")
    lines.append(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    if not degraded:
        lines.append("用途: 社交证据=品牌活跃度+受众规模；bio/website 暴露的邮箱可直接发信。")
    return "\n".join(lines)


def _handle_enrich_company_profile(args: dict) -> str:
    """对一条线索做深度公司调研 → 公司画像 + 合作可能性 + 分级, 写回 leads。

    慢操作(抓官网+DeepSeek, 15-40s), 走 to_thread 不阻塞 mcp 主循环。
    """
    from sources.company_intel import research
    user_id = args["user_id"]
    lead_id = (args.get("lead_id") or "").strip() or None
    company_name = (args.get("company_name") or "").strip() or None
    website = (args.get("website") or "").strip() or None
    country = (args.get("country") or "").strip() or None

    if not lead_id and not company_name:
        return "❌ 缺少 lead_id 或 company_name (至少传一个)"

    try:
        r = research(user_id, company_name=company_name, website=website,
                     country=country, lead_id=lead_id)
    except Exception as e:
        return f"❌ 调研失败: {type(e).__name__}: {e}"

    if r.get("error"):
        return f"❌ {r['error']}"

    level = r.get("cooperation_level", "unknown")
    level_icon = {"high": "🟢", "medium": "🟡", "low": "⚪", "none": "⚫"}.get(level, "❓")
    confidence = r.get("confidence", "")

    lines = [
        f"🔎 公司情报调研结果",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"公司: {r.get('company_name')}",
    ]
    if r.get("website"):
        lines.append(f"官网: {r['website']}")
    lines.append("")
    lines.append(f"🏢 公司类型: {r.get('company_type', '未知')}")
    lines.append(f"📦 业务范围: {r.get('business_scope', '未知')}")
    lines.append(f"📊 规模信号: {r.get('scale_signals', '未知')}")
    if r.get("location") and r.get("location") != "未知":
        lines.append(f"📍 所在地: {r['location']}")
    lines.append("")
    lines.append(f"{level_icon} 合作可能性: {level}")
    lines.append(f"   理由: {r.get('cooperation_reason', '未知')}")
    signals = r.get("key_signals") or []
    if signals:
        lines.append("")
        lines.append("🔑 关键信号:")
        for s in signals[:6]:
            lines.append(f"  • {s}")
    lines.append("")
    lines.append(f"📝 总结: {r.get('summary', '未知')}")
    lines.append(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    foot = [f"信息置信度: {confidence or '未知'}", f"抓取页数: {r.get('fetched_pages', 0)}"]
    if r.get("saved_to_db"):
        foot.append(f"✅ 已写回线索 {lead_id} (signal_level={level})")
    else:
        foot.append("ℹ️ 未入库(未传 lead_id)")
    foot.append("采购痕迹可 importyeti_lookup 补; 画像已具备可写开发信")
    lines.append(" | ".join(foot))
    return "\n".join(lines)


# ── KeyPool / Overpass (阶段0 号池竖切) ──

def _handle_osm_overpass_search(args: dict) -> str:
    """OSM Overpass 搜商户 → 入库。走号池轮换公共端点, 直连国内。

    重操作(~35s) 经 asyncio.to_thread 调度, 不阻塞 call_tool。
    """
    import uuid as _uuid
    from sources import overpass

    user_id = args["user_id"]
    query = args["query"]
    country = args.get("country", "")
    max_results = args.get("max_results", 15)

    try:
        leads = overpass.search(query=query, country=country, max_results=max_results)
    except Exception as e:
        return f"❌ Overpass 查询失败: {type(e).__name__}: {e}"

    if not leads:
        return (f"🗺️ Overpass 未找到带联系方式的商户。查询: {query} 国家: {country or '全球'}\n"
                "提示: OSM 邮箱覆盖率有限, 可换更宽品类词, 或换 gosom_maps/europages 源。")

    saved = 0
    skipped = 0
    with_email = 0
    for lc in leads:
        lead = lc.to_lead_dict(user_id)
        if lead_exists(user_id, lead.get("company_name", ""), lead.get("source", "")):
            skipped += 1
            continue
        lead["lead_id"] = _uuid.uuid4().hex[:12]
        lead["status"] = "new"
        try:
            insert_lead(lead)
            saved += 1
            if lc.email:
                with_email += 1
        except Exception:
            pass

    dup_note = f", 跳过重复 {skipped} 条" if skipped else ""
    lines = [f"🗺️ OSM Overpass 完成: 找到 {len(leads)} 条带联系方式商户 (含 {with_email} 条带邮箱), 入库 {saved} 条{dup_note}"]
    for lc in leads[:8]:
        em = f" 📧{lc.email}" if lc.email else ""
        web = f" 🔗{lc.website[:30]}" if lc.website else ""
        lines.append(f"  - [{lc.buyer_type or 'distributor'}] {lc.company_name} ({lc.city or '?'}{em}{web})")
    if saved:
        lines.append("\n下一步: verify_email 验邮箱 → compose_outreach 写开发信")
    return "\n".join(lines)


def _handle_serpapi_maps_search(args: dict) -> str:
    """SerpApi google_maps 列表查询 → 入库。走号池 serpapi key 轮换 + SQLite 缓存。

    字段碾压 gosom (phone/website/type_ids/rating/reviews), type_ids 天然 buyer_type。
    google_maps 每调必扣额度 → adapter 内置 serpapi_query_cache 缓存 (命中不调 API)。
    不带 type 参数 (列表模式返 ~10 条)。
    """
    import uuid as _uuid
    from sources import serpapi_maps

    user_id = args["user_id"]
    query = args["query"]
    country = args.get("country", "")
    city = args.get("city", "") or _extract_city_from_query(query)
    max_results = args.get("max_results", 10)

    try:
        leads = serpapi_maps.search_maps(query=query, country=country,
                                         city=city, max_results=max_results)
    except Exception as e:
        return f"❌ SerpApi google_maps 查询失败: {type(e).__name__}: {e}"

    if not leads:
        return (f"🗺️ SerpApi google_maps 未找到商户。查询: {query} 国家: {country or '全球'} 城市: {city or '未指定'}\n"
                "提示: 可换品类词或指定城市。SerpApi 免费档每调必扣额度, adapter 已内置缓存。")

    saved = 0
    skipped = 0
    with_phone = 0
    with_website = 0
    for lc in leads:
        lead = lc.to_lead_dict(user_id)
        if lead_exists(user_id, lead.get("company_name", ""), lead.get("source", "")):
            skipped += 1
            continue
        lead["lead_id"] = _uuid.uuid4().hex[:12]
        lead["status"] = "new"
        try:
            insert_lead(lead)
            saved += 1
            if lc.phone:
                with_phone += 1
            if lc.website:
                with_website += 1
        except Exception:
            pass

    dup_note = f", 跳过重复 {skipped} 条" if skipped else ""
    lines = [f"🗺️ SerpApi google_maps 完成: 找到 {len(leads)} 条商户 "
             f"(含 {with_phone} 电话 {with_website} 官网), 入库 {saved} 条{dup_note}"]
    for lc in leads[:8]:
        web = f" 🔗{lc.website[:30]}" if lc.website else ""
        ph = f" ☎{lc.phone[:20]}" if lc.phone else ""
        bt = lc.buyer_type or "distributor"
        lines.append(f"  - [{bt}] {lc.company_name} ({lc.city or '?'}{ph}{web}) [{lc.detail[:30]}]")
    if saved:
        lines.append("\n下一步: enrich_lead_emails 补邮箱 → compose_outreach 写开发信")
    return "\n".join(lines)


def _extract_city_from_query(query: str) -> str:
    """从 query 尽力提取城市名 (用于 serpapi_maps ll 参数)。
    启发式: 逗号/分隔符后取末段; 单 token query 不提取。"""
    if not query:
        return ""
    # 按常见分隔符切
    import re as _re
    parts = _re.split(r"[,，/|]+", query)
    parts = [p.strip() for p in parts if p and p.strip()]
    if len(parts) >= 2:
        return parts[-1]  # 最后一段往往是地理 (如 "LED distributor, Berlin")
    # 无分隔符: 看末尾 token 是否像城市 (只取 ASCII 单词, 2-20 字符)
    toks = [t for t in query.strip().split() if t]
    if len(toks) >= 2:
        last = toks[-1].strip(",.，。")
        if last and last[0].isalpha() and 2 <= len(last) <= 20:
            return last
    return ""


def _handle_tavily_exhibition(args: dict) -> str:
    """Tavily 展会源: 全网搜展商 → LLM 提取公司 → 入库。走号池 Tavily key 轮换。

    不锁 include_domains (锁官网只拿主站 answer 泛泛); 英文精准 query 让 Tavily
    全网聚合出展商名, DeepSeek 提取为结构化线索。
    """
    import uuid as _uuid
    from sources import tavily

    user_id = args["user_id"]
    query = args["query"]
    max_results = args.get("max_results", 15)

    try:
        leads = tavily.search_exhibition(query=query, max_results=max_results)
    except Exception as e:
        return f"❌ Tavily 展会源失败: {type(e).__name__}: {e}"

    if not leads:
        return (f"🎪 Tavily 未搜到展商。查询: {query}\n"
                "提示: query 带展会名 (如 'Light+Building 法兰克福LED展') 命中率更高。")

    saved = 0
    skipped = 0
    for lc in leads:
        lead = lc.to_lead_dict(user_id)
        if lead_exists(user_id, lead.get("company_name", ""), lead.get("source", "")):
            skipped += 1
            continue
        lead["lead_id"] = _uuid.uuid4().hex[:12]
        lead["status"] = "new"
        try:
            insert_lead(lead)
            saved += 1
        except Exception:
            pass

    dup_note = f", 跳过重复 {skipped} 条" if skipped else ""
    lines = [f"🎪 Tavily 展会源完成: 提取 {len(leads)} 家展商, 入库 {saved} 条{dup_note}"]
    for lc in leads[:8]:
        web = f" 🔗{lc.website[:30]}" if lc.website else ""
        biz = f" ({lc.detail[:30]})" if lc.detail else ""
        lines.append(f"  - [展商] {lc.company_name}{web}{biz}")
    if saved:
        lines.append("\n下一步: enrich_company_profile 深度调研 → enrich_lead_emails 补邮箱")
    return "\n".join(lines)


def _handle_keypool_status(args: dict) -> str:
    """号池运维视图: 各 provider key/配额/usage_log/代理。"""
    import sqlite3
    from keypool import KeyPool, ProxyPool, KEYPOOL_DB

    kp = KeyPool()
    st = kp.status()
    if not st:
        return ("🔑 号池为空 (未投喂任何 key/端点)。\n"
                "Overpass 首次查询会自动 bootstrap 6 个公共端点; "
                "投喂私有 key 在代码层 keypool.add(provider, api_key=...) 。")

    lines = ["🔑 号池状态 (tenant=ear):"]
    for provider, info in st.items():
        total = info["total"]
        quota = f"{info['used']}/{total}" if total else f"{info['used']}/∞"
        lines.append(f"  • {provider}: active={info['active']} exhausted={info['exhausted']} used={quota}")

    try:
        cc = sqlite3.connect(str(KEYPOOL_DB))
        cc.row_factory = sqlite3.Row
        recent = cc.execute(
            "SELECT provider, result, COUNT(*) AS n FROM usage_log "
            "GROUP BY provider, result ORDER BY provider"
        ).fetchall()
        cc.close()
        if recent:
            lines.append("近期用量:")
            for r in recent:
                lines.append(f"  {r['provider']:12} {r['result']:8} × {r['n']}")
    except Exception:
        pass

    ps = ProxyPool().status()
    if ps:
        lines.append(f"代理池: {len(ps)} 个 (当前: {ps[0].get('proxy_url', '')})")
    else:
        lines.append("代理池: 空 (Overpass 直连不需要; Apollo 等 C 类待住宅代理)")
    return "\n".join(lines)


# ── search_leads: 统一发现入口 (薄路由, v1) ──
#
# 架构决策:
#  ① 意图分类用关键词规则(非 LLM): v1 表只有 4 类且边界清晰, LLM 加延迟且失败仍降级回规则。
#  ② 经销商双源顺序调用(非并发): 两源各 ~30s, 顺序 ~60s 在 to_thread 内可接受; 并发要加锁/超时复杂度高, 收益<成本。
#  ③ LLM 精度过滤放 search_leads 合并层(共享 helper): 现有源 handler 代码层无 LLM 过滤(SKILL.md 是提示词层, 噪声线索就这么进来的), 不新加就实现不了精度过滤。
#     helper 名 _llm_filter_leads, 将来其他入口可复用。
#
# 架构岔路(选最小方案 A, 已记录): handler 内部直接 insert_lead, search_leads 调 handler 后线索已入库。
#   方案 A: 调 handler → 按时间戳筛本次入库的 → 过 LLM → 垃圾标 status=invalid。守 DON'Ts "只调用 handler 不重写源", 代价是垃圾先入库再标 invalid(列表筛 status!=invalid 即可, 符合硬过滤精神)。
#   方案 B(未选): 绕过 handler 直接调源模块 search()。需重写入库/去重逻辑, 违反 "只调用 handler 不重写源"。

def _classify_search_intent(query: str, intent: str = "") -> str:
    """v1 意图分类: 关键词规则。返回 distributor/customs_verify/social/exhibition。

    显式 intent 参数优先(已校验); 否则按 query 关键词判定; 兜底 distributor(最常见)。
    """
    i = (intent or "").strip().lower()
    if i in ("distributor", "customs_verify", "social", "exhibition"):
        return i
    q = (query or "").lower()
    # 海关验真(强信号词优先于经销商, 因 "import distributor" 可能两词都有)
    # 强词: 单独出现即海关验真
    if any(k in q for k in (
        "海关", "提单", "进口过", "进口记录", "验真", "验证公司", "真实买家", "真实进口",
        "customs", "importyeti", "bill of lading", "import record", "verify company",
        "real importer", "importer verify", "verify importer", "is ... a real importer",
    )):
        return "customs_verify"
    # 弱词组合: "进口" + 质疑词(是不是/是否/有没有/吗) = 验真意图; 单"进口经销商"不算
    if "进口" in q and any(k in q for k in ("是不是", "是否", "有没有", "吗", "真不真", "真的")):
        return "customs_verify"
    if "import" in q and any(k in q for k in ("really", "actually", "verify", "is it")):
        return "customs_verify"
    # 社交联系方式
    if any(k in q for k in (
        "tiktok", "instagram", "社交", "粉丝", "博主", "创作者", "influencer",
        "社媒", "社交媒体", " IG ", "@",
    )) or any(tok.startswith("@") for tok in q.split()):
        return "social"
    # 展会展商
    if any(k in q for k in (
        "展会", "展商", "博览", "exhibition", "trade show", "expo", "fair",
        "exhibitor", "light+building",
    )):
        return "exhibition"
    # 经销商/批发商(默认兜底也走这里)
    return "distributor"


def _llm_filter_leads(leads: list, product_intent: str) -> tuple:
    """LLM 批量精度过滤: 对一批已入库的 lead dict 判 keep/discard。

    治噪声 (MediaMarkt/Saturn 类一眼假混进库)。复用 hunter_discover/company_intel 同款
    DeepSeek env (DEEPSEEK_API_KEY/BASE_URL/MODEL) + https_proxy。

    判定规则(对齐 SKILL.md Step 3.5 关卡 A 一眼假):
      - 占位假邮箱 (@beispiel.de / @example.* / user@domain.com)
      - 国家与目标市场不符
      - 品类完全不沾边 (搜 LED 回来医院/教会/银行)
      - 明显垃圾目录站/SEO 农场

    Args:
      leads: list[dict], 每 dict 至少含 lead_id/company_name/country/email/source/website
      product_intent: 用户的产品+市场意图文本 (LLM 据此判品类沾边)

    Returns:
      (kept_ids, discarded_ids, discard_reasons): 三个 list; LLM 失败/无 key 时
      kept_ids=全部, discarded_ids=[] (降级=不过滤, 不阻断主流程)。
    """
    if not leads:
        return [], [], {}
    import os as _os
    key = _os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        logger.warning("search_leads: DEEPSEEK_API_KEY 未配置, LLM 过滤降级=不过滤(全保留)")
        return [l["lead_id"] for l in leads], [], {}

    # 限制批量大小 (LLM 上下文 + 延迟): 单批最多 25 条
    batch = leads[:25]
    base = _os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip().rstrip("/")
    model = _os.environ.get("DEEPSEEK_MODEL", "deepseek-chat").strip()
    proxy = _os.environ.get("https_proxy") or _os.environ.get("HTTPS_PROXY") or _os.environ.get("http_proxy") or _os.environ.get("HTTP_PROXY") or None

    # 构造紧凑候选清单 (只给 LLM 判定所需的最少字段)
    cand = []
    for idx, l in enumerate(batch):
        cand.append({
            "i": idx,
            "company": (l.get("company_name") or "")[:80],
            "country": (l.get("country") or "")[:30],
            "email": (l.get("email") or "")[:60],
            "website": (l.get("website") or "")[:60],
            "source": (l.get("source") or "")[:20],
        })

    # 从 product_intent 里剥掉"经销商/批发商/distributor/wholesaler"等角色词:
    # 这些词会让 LLM 误以为"只要经销商, 制造商/品牌方都丢", 误杀优质线索。
    # 留下产品+市场, LLM 只判品类沾不沾边。
    intent_clean = product_intent
    for w in ("distributor", "wholesaler", "dealer", "retailer", "reseller",
              "经销商", "批发商", "代理商", "分销商", "零售商", "贸易商"):
        intent_clean = intent_clean.replace(w, "")
    intent_clean = " ".join(intent_clean.split())  # 压缩多余空格

    prompt = f"""你是 B2B 外贸获客的线索质检员。用户的产品+市场意图:
{intent_clean[:200]}

下面是搜回来的候选线索。请逐条判定它是 **keep**(对的目标公司) 还是 **discard**(一眼假/垃圾/完全不沾边)。

⚠️ 重要边界 (宁可保留, 不要误杀):
- 凡是产品和用户意图沾边的公司, 一律 keep —— 不管它是 制造商/品牌方/生产商/进口商/工程商/批发商/经销商 哪种角色 (制造商/品牌方是潜在代工/OEM/贴牌客户, 不是垃圾)
- 只丢弃"一眼假"和"完全不沾边的无关行业"

discard 判据 (命中任一即 discard, 否则 keep):
1. 占位假邮箱: *@beispiel.* / *@example.* / *@domain.* / user@ / name@domain.tld
2. 完全不沾边的无关行业: 搜 LED照明, 回来的明显是 医院/教会/银行/大学/政府/律所/保险/餐饮/服装/房地产 等无关行业公司 (消费电子超市如 MediaMarkt/Saturn/BestBuy 也算无关, 是消费品零售非专业照明)
3. 国家与意图市场完全不符 (除非明显跨国连锁)
4. 明显垃圾目录站/SEO 农场/纯导航站

凡是产品品类沾边的 (含同类产品的制造商/品牌方), 一律 keep。

只输出 JSON, 不要 markdown 围栏:
{{"verdicts": [{{"i": 0, "keep": true, "reason": "德国照明公司, 品类沾边"}}, ...]}}

候选线索 (i 是下标):
{json.dumps(cand, ensure_ascii=False)}"""

    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
        "max_tokens": 1500,
    }
    try:
        import httpx
        with httpx.Client(timeout=45, proxy=proxy) as c:
            r = c.post(f"{base}/chat/completions",
                       headers={"Authorization": f"Bearer {key}",
                                "Content-Type": "application/json"},
                       json=body)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"] or ""
    except Exception as e:
        logger.warning("search_leads: LLM 过滤调用失败, 降级=不过滤(全保留): %s: %s", type(e).__name__, e)
        return [l["lead_id"] for l in leads], [], {}

    # 解析 JSON (容错: 去 markdown 围栏)
    s = content.strip()
    if s.startswith("```"):
        s = s.lstrip("`").lstrip("json").lstrip().rstrip("`").strip()
    try:
        obj = json.loads(s)
        verdicts = obj.get("verdicts", [])
    except Exception:
        logger.warning("search_leads: LLM 过滤响应解析失败, 降级=不过滤。原始: %s", content[:300])
        return [l["lead_id"] for l in leads], [], {}

    kept_ids = []
    discarded_ids = []
    discard_reasons = {}
    verdict_map = {int(v.get("i")): v for v in verdicts if isinstance(v, dict) and v.get("i") is not None}
    for idx, l in enumerate(batch):
        v = verdict_map.get(idx)
        if v and v.get("keep") is False:
            discarded_ids.append(l["lead_id"])
            discard_reasons[l["lead_id"]] = (v.get("reason") or "LLM 判定不沾边")[:80]
        else:
            kept_ids.append(l["lead_id"])
    # 超出 batch 的 (leads > 25) 一律保留 (没过 LLM, 不武断丢弃)
    for l in leads[len(batch):]:
        kept_ids.append(l["lead_id"])
    return kept_ids, discarded_ids, discard_reasons


def _handle_search_leads(args: dict) -> str:
    """统一发现入口: 意图分类 → 路由源 → 合并去重 → LLM 过滤 → 返回摘要。

    源 handler 内部已 lead_exists 去重 + insert_lead 入库 (status=new)。
    本函数调 handler 后, 按时间戳筛本次入库的 new leads → 过 LLM → 垃圾标 invalid。
    """
    from storage.db import list_leads as _list_leads, update_lead_status as _update_status

    user_id = args["user_id"]
    query = args["query"]
    country = args.get("country", "")
    intent_arg = args.get("intent", "")
    max_results = args.get("max_results", 15)

    # ① 意图分类
    intent = _classify_search_intent(query, intent_arg)

    # ② 按意图选源 + 路由
    # 记录路由前的 new leads id 集合, 用于精准识别本次新增
    pre_new_ids = {l["lead_id"] for l in _list_leads(user_id, status="new", limit=500)}
    pre_tstamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    source_hits = {}      # source_key -> 搜到的原始数(从 handler 字符串解析不可靠, 用入库数近似)
    source_saved = {}     # source_key -> 本次新入库数
    handler_logs = []     # 各源 handler 返回的文本 (附在最终结果)

    if intent == "distributor":
        # 双源顺序: OSM Overpass + search_customers(gosom/hunter)
        sub_args = {"user_id": user_id, "query": query, "country": country, "max_results": max_results}
        # OSM Overpass
        try:
            osm_res = _handle_osm_overpass_search(sub_args)
        except Exception as e:
            osm_res = f"❌ OSM Overpass 失败: {type(e).__name__}: {e}"
        handler_logs.append(("osm_overpass", osm_res))
        # search_customers (gosom/hunter/europages)
        try:
            sc_res = _handle_search_customers(sub_args)
        except Exception as e:
            sc_res = f"❌ search_customers 失败: {type(e).__name__}: {e}"
        handler_logs.append(("search_customers", sc_res))
        # SerpApi google_maps (第三源: 字段碾压, type_ids 天然 buyer_type)
        try:
            sa_res = _handle_serpapi_maps_search(sub_args)
        except Exception as e:
            sa_res = f"❌ serpapi_maps 失败: {type(e).__name__}: {e}"
        handler_logs.append(("serpapi_maps", sa_res))

    elif intent == "customs_verify":
        # 海关验真: importyeti_lookup (单源, 查询类不写库, 直接返 handler 文本)
        company = query
        try:
            iy_res = _handle_importyeti_lookup({"company_name": company})
        except Exception as e:
            iy_res = f"❌ importyeti_lookup 失败: {type(e).__name__}: {e}"
        handler_logs.append(("importyeti", iy_res))
        intent_label = "🛃 海关验真"
        return _format_search_leads_result(intent, intent_label, [], [], {}, handler_logs,
                                           extra_note="海关源为查询类(不写库), 结果见上方 importyeti 输出。")

    elif intent == "social":
        # 社交: 解析 username/platform 从 query
        import re as _re
        username = ""
        platform = "tiktok"
        q_low = query.lower()
        if "instagram" in q_low or " ig " in q_low:
            platform = "instagram"
        # 提取 @username 或末尾裸词
        m = _re.search(r"@([a-z0-9._]+)", q_low)
        if m:
            username = m.group(1)
        else:
            # 取最后一个 token 作为 username 尝试
            toks = [t for t in _re.split(r"[\s,]+", query.strip()) if t]
            if toks:
                username = toks[-1].lstrip("@")
        if not username:
            return "❌ 社交意图但未从 query 解析出 username。请在 query 里带 @用户名 (例: '@khaby.lame TikTok')"
        try:
            so_res = _handle_social_profile_lookup({"platform": platform, "username": username})
        except Exception as e:
            so_res = f"❌ social_profile_lookup 失败: {type(e).__name__}: {e}"
        handler_logs.append(("social", so_res))
        intent_label = "📱 社交联系方式"
        return _format_search_leads_result(intent, intent_label, [], [], {}, handler_logs,
                                           extra_note="社交源为查询类(不写库), 结果见上方 social_profile_lookup 输出。")

    elif intent == "exhibition":
        # 展会源: Tavily 全网搜展商 → 入库 (走 distributor 同款入库 + LLM 过滤流程)
        sub_args = {"user_id": user_id, "query": query, "country": country, "max_results": max_results}
        try:
            tv_res = _handle_tavily_exhibition(sub_args)
        except Exception as e:
            tv_res = f"❌ Tavily 展会源失败: {type(e).__name__}: {e}"
        handler_logs.append(("tavily_exhibition", tv_res))

    else:
        return f"❌ 未知意图: {intent}"

    # ③ 识别本次新增的 new leads (按时间戳 + 不在 pre_new_ids 集合里)
    post_new = _list_leads(user_id, status="new", limit=500)
    new_leads = [l for l in post_new
                 if l["lead_id"] not in pre_new_ids
                 and (l.get("discovered_at") or "") >= pre_tstamp]
    # 兜底: 如果时间戳筛空(handler 写库极快跨秒边界), 用集合差
    if not new_leads:
        new_leads = [l for l in post_new if l["lead_id"] not in pre_new_ids]

    # 按源统计命中
    for l in new_leads:
        src = l.get("source") or "unknown"
        source_saved[src] = source_saved.get(src, 0) + 1

    # ④ LLM 精度过滤 → 垃圾标 invalid
    product_intent_text = f"{query} | country={country or '(未指定)'}"
    kept_ids, discarded_ids, discard_reasons = _llm_filter_leads(new_leads, product_intent_text)
    for lid in discarded_ids:
        try:
            _update_status(user_id, lid, status="invalid")
        except Exception:
            pass  # 标 invalid 失败不阻断, 该 lead 保持 new

    # ⑤ 格式化结果
    intent_labels = {
        "distributor": "🏪 经销商/批发商 (双源: OSM + gosom/hunter)",
        "customs_verify": "🛃 海关验真",
        "social": "📱 社交联系方式",
        "exhibition": "🎪 展会展商",
    }
    surviving = [l for l in new_leads if l["lead_id"] in kept_ids]
    return _format_search_leads_result(intent, intent_labels.get(intent, intent), surviving, discarded_ids,
                                       discard_reasons, handler_logs, source_saved=source_saved)


def _format_search_leads_result(intent: str, intent_label: str, surviving: list,
                                discarded: list, discard_reasons: dict,
                                handler_logs: list, source_saved: dict = None,
                                extra_note: str = "") -> str:
    """统一格式化 search_leads 返回。"""
    lines = [
        f"🔎 search_leads 统一发现结果",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"意图: {intent_label}",
    ]
    if source_saved:
        total_in = sum(source_saved.values())
        lines.append(f"各源新入库: {total_in} 条")
        for src, n in sorted(source_saved.items(), key=lambda x: -x[1]):
            lines.append(f"  • {src}: {n} 条")

    if intent in ("customs_verify", "social") or not surviving:
        # 查询类: 直接展示 handler 输出
        if extra_note:
            lines.append("")
            lines.append(extra_note)
        for src, log in handler_logs:
            lines.append("")
            lines.append(f"── {src} ──")
            lines.append(log)
        return "\n".join(lines)

    # distributor 意图: 展示合并后存活线索 + 过滤丢弃统计
    with_email = sum(1 for l in surviving if l.get("email"))
    lines.append(f"")
    lines.append(f"✅ 存活线索: {len(surviving)} 条 (邮箱: {with_email} 条)")
    if discarded:
        lines.append(f"🗑️ LLM 过滤丢弃: {len(discarded)} 条 (已标 status=invalid)")
    lines.append("")
    lines.append("存活线索 TOP:")
    for l in surviving[:10]:
        em = f" 📧{l.get('email','')[:40]}" if l.get("email") else ""
        web = f" 🔗{(l.get('website') or '')[:30]}" if l.get("website") else ""
        lines.append(f"  - [{l.get('source','?')}] {l.get('company_name','?')} ({l.get('country','?')}{em}{web})")
    if len(surviving) > 10:
        lines.append(f"  ... 另 {len(surviving)-10} 条见 list_leads(status='new')")

    if discarded:
        lines.append("")
        lines.append("丢弃明细 (TOP 5):")
        for lid in discarded[:5]:
            r = discard_reasons.get(lid, "LLM 判定不沾边")
            lines.append(f"  - {lid[:8]}: {r}")

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("各源原始输出 (供 debug):")
    for src, log in handler_logs:
        lines.append(f"── {src} ──")
        # 截断过长的 handler 输出
        lines.append(log[:600] + ("..." if len(log) > 600 else ""))
    lines.append("")
    lines.append("下一步: list_leads 看全量 → enrich_lead_emails 补邮箱 → enrich_company_profile 深调 → compose_outreach 写开发信")
    return "\n".join(lines)


# ── Entry point ──

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

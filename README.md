# ReachSurge

> 一个把 AI agent 变成 B2B 获客流水线的 MCP 服务：多源海外买家发现 + 邮箱富集瀑布 + 逐租户 CRM 存储，开箱即接进任何支持 MCP 的 agent（Claude、Hermes、自建 agent 等）。

---

## 它解决什么问题

做外贸、做 SBD、做出海冷启动的人，找海外买家这件事被切碎在十几个工具里：Google Maps 刮经销商、海关提单验真、B2B 平台翻档案、社交平台扒联系方式、再挨个补邮箱、验邮箱、写开发信、跟进回复。每一步都简单，串起来就是体力活，且高度依赖「会不会用工具」的经验。

ReachSurge 把这条链路固化成一 MCP 服务，暴露约 20 个工具。Agent 不用记「先搜哪、再验哪、邮箱去哪补」，调 `search_leads` 自动按意图路由到对的数据源，调 `enrich_lead_emails` 自动跑富集瀑布，调 `send_email` 真发信、`check_inbox` 看回复。线索、知识库、发信记录都按用户隔离存进 SQLite。

它不是 SaaS、不托管你的数据、不替你做判断——它给你一套可被 agent 驱动的获客工具箱。

---

## 核心特性

- **多数据源融合**：Google Maps 经销商（SerpApi / gosom）、OpenStreetMap Overpass、Hunter Discover、欧洲 B2B 平台 Europages、美国海关提单 ImportYeti、TikTok/Instagram 社交档案、Tavily 全网搜展商，统一归一化成 `LeadCandidate`，跨源去重、按分数排序。
- **统一发现入口 `search_leads`**：一个工具按意图自动路由到对的数据源——找经销商走多源地图、验真买家走海关、扒社交联系方式走 TikTok/IG、搜展会展商走 Tavily。不用让 agent 在十几个工具里自己选。
- **邮箱富集瀑布**：网站深抓 → Hunter（带配额守卫）→ SMTP 探测 → info@ 兜底，首命中即停。命中结果分级（verified / scraped / guessed / catchall）并写回线索，带 SQLite 域名缓存避免重复烧配额。
- **LLM 精度过滤**：搜索结果默认过一道大模型过滤，把 MediaMarkt/Saturn 这类一眼假、占位邮箱、品类不匹配的噪声标成 `invalid`，不污染线索库。产品词由 LLM 跨语言提取，避免关键词匹配误杀。
- **号池与配额管理**：`keypool.py` 跨租户共享 API key + 代理池，配额用量内化、自动轮换、限流自动切下一个 key。Overpass 等公共端点走号池轮询免 key。
- **逐租户隔离**：每个用户一个独立 SQLite 文件，`user_id` 贯穿所有工具。SMTP/IMAP 密码、API key 落库前用 Fernet 对称加密。
- **邮箱闭环**：`compose_outreach` 出开发信草稿 → `send_email` 走 SMTP 真发信并记 `outreach_records` → `check_inbox` 走 IMAP 拉新回复入库 `inquiries`，IMAP UID 去重。
- **零硬编码密钥**：所有凭证走环境变量 + `.env`（python-dotenv），代码内无任何明文 key。

---

## 架构概览

```
                       ┌─────────────────────────────────────┐
   MCP 客户端           │          mcp_server.py              │
  (Claude / Hermes /  ─►│  标准 mcp.server，聚合 ~20 个工具    │
   自建 agent 等)       │  按意图路由 + LLM 精度过滤           │
                       └───┬──────────┬──────────┬───────────┘
                           │          │          │
              ┌────────────▼─┐  ┌─────▼──────┐  ┌▼──────────────┐
              │ sources/     │  │ keypool.py │  │ storage/      │
              │ 数据源 adapter│  │ API 号池    │  │ db.py (SQLite)│
              │ ─────────── │  │ + 代理池    │  │ rag.py (知识库)│
              │ • serpapi_   │  │ + 配额守卫  │  │ Fernet 加密   │
              │   maps       │  │            │  │ per-user 隔离  │
              │ • overpass   │  │            │  │               │
              │ • gosom      │  │            │  │ tools/        │
              │ • hunter_    │  │            │  │ email_verify  │
              │   discover   │  │            │  │ (MX+SMTP 验)  │
              │ • europages  │  │            │  └───────────────┘
              │ • tavily     │  │            │
              │ • customs_   │  │     ┌──────▼──────────────────┐
              │   importyeti │  │     │ sources/enrichment_     │
              │ • social_    │  │     │ providers/              │
              │   scout      │  │     │ 邮箱富集瀑布             │
              │ • company_   │  │     │ scrape→hunter→smtp→guess│
              │   intel      │  │     └─────────────────────────┘
              │ • last30days │  │
              │   _intent*   │  │
              └──────────────┘  │
                                │
              LeadCandidate 统一结构 ←  registry.py 合并去重排序
```

数据流：客户端调工具 → `mcp_server` 分发 → `sources/` 各 adapter 采集（经 `keypool` 取 key / 代理）→ 结果归一化 → `registry` 合并去重 → LLM 过滤 → 写入 `storage/db`（per-user）。邮箱富集与发信/收信走独立的工具分支。

`*` last30days 意图源基于独立项目，默认未启用，见 [可选依赖](#可选依赖)。

---

## 工具清单

ReachSurge 通过单个 MCP 服务暴露以下工具（按类别分组）。

### 配置与知识库

| 工具 | 说明 |
|------|------|
| `save_user_config` | 录入或更新用户的产品信息、目标市场、SMTP/IMAP 邮箱配置 |
| `get_user_config` | 查询用户已保存的产品信息和配置 |
| `add_knowledge` | 把产品资料存入知识库，供后续写开发信检索 |
| `search_knowledge` | 从用户知识库检索产品相关信息 |

### 线索管理

| 工具 | 说明 |
|------|------|
| `save_lead` | 保存一条潜在客户线索到数据库 |
| `list_leads` | 列出用户线索，支持按状态筛选（new/contacted/replied/interested/not_interested/invalid） |
| `update_lead_status` | 更新线索状态与邮箱验证状态 |
| `enrich_company_profile` | 深抓官网 + 大模型产出公司画像与合作可能性判断，写回 `signal_level` / `company_intel` |

### 邮箱闭环

| 工具 | 说明 |
|------|------|
| `verify_email` | 验证单个邮箱：格式 → MX → SMTP 握手（不真发信） |
| `enrich_lead_emails` | 批量为无邮箱线索跑富集瀑布并 SMTP 验证，异步任务模式 |
| `compose_outreach` | 基于知识库生成开发信草稿（仅出文本，不发送） |
| `send_email` | 走已配置 SMTP 真发信，自动记 `outreach_records` 并标记线索 `contacted` |
| `check_inbox` | 走已配置 IMAP 拉新回复入库，IMAP UID 去重 |
| `get_task_status` | 查询异步任务（如 `enrich_lead_emails`）的进度与结果 |

### 数据发现

| 工具 | 说明 |
|------|------|
| `search_leads` | **统一发现入口 / 首选**。按意图自动路由到对的数据源，跨源去重 + LLM 精度过滤 |
| `search_customers` | 多源融合搜索并入库（意图源采购讨论 + 档案源经销商），SOP 式入口 |
| `osm_overpass_search` | OpenStreetMap Overpass API 搜海外商户，免 key、原生提取 email/website/phone |
| `importyeti_lookup` | 查美国海关提单，验某公司是否真从中国进口、进口什么 |
| `social_profile_lookup` | 免认证抓 TikTok / Instagram 公开 profile（粉丝数、bio、网站、邮箱） |

### 情报与号池

| 工具 | 说明 |
|------|------|
| `keypool_status` | 查询 API 号池状态：各 provider 的 key 数量、配额用量、代理池 |

---

## 快速开始

### 1. 克隆与装依赖

```bash
git clone https://github.com/erduo1998-cell/reachsurge.git
cd reachsurge
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium # 仅供 europages / customs_importyeti 抓取用
```

> **可选 · gosom 经销商源二进制**：`search_customers` 默认会调 gosom 源（Google Maps 高质量经销商档案 + 官网邮箱），它依赖 [`gosom/google-maps-scraper`](https://github.com/gosom/google-maps-scraper) 项目提供的 `google_maps_scraper` 二进制（内嵌 playwright，首次运行自动下载 chromium）。该二进制**不随仓库分发**——需自行编译或下载，放到项目 `bin/google_maps_scraper`，或用环境变量 `GOSOM_BIN` 指向任意路径。未提供时 gosom 源会在 `search()` 报错并降级，其余源不受影响。

### 2. 配置环境变量

在项目根目录建 `.env`（示例见 `.env.example`）：

```dotenv
# ── 必填：大模型（精度过滤 / 公司画像 / 产品词提取都依赖它）──
DEEPSEEK_API_KEY=YOUR_DEEPSEEK_API_KEY
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat

# ── 数据源 key（按需配，不配则对应源自动降级）──
HUNTER_API_KEY=YOUR_HUNTER_API_KEY                 # 邮箱富集 + Discover 找公司
SERPAPI_API_KEYS=key1,key2                         # 多 key 逗号分隔，号池轮换
TAVILY_API_KEYS=tvly-key1,tvly-key2                # 展会展商搜索
CAPSOLVER_API_KEY=YOUR_CAPSOLVER_API_KEY           # 仅 europages 过 WAF 需要

# ── 代理（出口 IP 受限的数据源需要）──
HTTPS_PROXY=http://127.0.0.1:7890
HTTP_PROXY=http://127.0.0.1:7890

# ── 可选：数据目录与凭证加密密钥（不设则用默认路径 / 首次自动生成）──
LEADGEN_DATA_DIR=/path/to/data
LEADGEN_FERNET_KEY=YOUR_FERNET_KEY                 # urlsafe base64，留空则自动生成
```

SMTP / IMAP 凭证不放进 `.env`，而是通过 `save_user_config` 工具录入，落库前 Fernet 加密。

### 3. 启动 MCP 服务

```bash
python mcp_server.py
```

服务通过 stdio 与 MCP 客户端通信。在你的 agent 配置里把它注册成一个 MCP server（stdio 类型，命令 `python /path/to/mcp_server.py`）即可。

---

## 环境变量配置表

| 变量 | 必填 | 用途 | 不配的后果 |
|------|:----:|------|-----------|
| `DEEPSEEK_API_KEY` | 是 | 大模型调用（精度过滤、公司画像、产品词提取） | LLM 相关功能全部降级或报错 |
| `DEEPSEEK_BASE_URL` | 否 | 大模型 API 端点，默认 `https://api.deepseek.com` | 用默认值。可指向任何 OpenAI 兼容端点 |
| `DEEPSEEK_MODEL` | 否 | 模型名，默认 `deepseek-chat` | 用默认值 |
| `HUNTER_API_KEY` | 否 | Hunter 邮箱富集 + Discover 找公司 | Hunter 相关源不可用 |
| `SERPAPI_API_KEYS` | 否 | SerpApi Google Maps 经销商源，逗号分隔多 key | serpapi_maps 源不可用 |
| `TAVILY_API_KEYS` | 否 | Tavily 展会展商搜索，逗号分隔多 key | 展会路由不可用 |
| `CAPSOLVER_API_KEY` | 否 | europages 过 Cloudflare WAF | europages 源不可用（其他源不受影响） |
| `GOSOM_BIN` | 否 | gosom `google_maps_scraper` 二进制路径，默认项目内 `bin/google_maps_scraper` | gosom 源不可用（其他源不受影响） |
| `HTTPS_PROXY` / `HTTP_PROXY` | 否 | 出口代理 | 部分对出口 IP 敏感的源（社交、海关）可能失败 |
| `LEADGEN_DATA_DIR` | 否 | SQLite 与知识库存储目录，默认 `data/` | 用默认值 |
| `LEADGEN_ENV_FILE` | 否 | 额外的 `.env` 文件路径，启动时 `load_dotenv` 注入 | 不加载额外 env |
| `LEADGEN_FERNET_KEY` | 否 | 凭证加密密钥（urlsafe base64） | 首次自动生成并写入 `LEADGEN_DATA_DIR`（默认项目 `data/`）下 `leadgen_fernet.key` |

> SMTP / IMAP 的 `smtp_host` / `smtp_user` / `smtp_password` / `imap_host` 等不走环境变量，由 `save_user_config` 工具录入、加密落库。

---

## 数据源说明

| 源 | 用途 | 是否需 key | 免费额度 / 说明 |
|----|------|:----------:|----------------|
| **SerpApi google_maps** | Google Maps 经销商档案（phone/website/type_ids） | 是 | 免费档每月有搜索额度，type_ids 天然映射 buyer_type |
| **OpenStreetMap Overpass** | 本地经销商 / 批发商，原生带 email/website/phone | 否 | 公共端点，号池轮询 6 个端点避限流 |
| **gosom Google Maps scraper** | Google Maps 经销商档案（内嵌 playwright） | 否 | 免 docker 二进制（需自备，见[快速开始](#1-克隆与装依赖)），邮箱命中率较高 |
| **Hunter Discover** | 按域找公司 + 邮箱富集（domain-search） | 是 | Free 档 50 search/月，Discover 找公司不消耗 credits |
| **Europages** | 欧洲最大 B2B 平台档案 | 否（需 CAPSOLVER） | playwright 过 Cloudflare WAF |
| **Tavily** | 全网搜展会展商 + LLM 提取展商名单 | 是 | 免费档每月有搜索额度 |
| **ImportYeti** | 美国海关提单，验某公司是否真从中国进口 | 否 | 仅美国市场有提单数据，数据有滞后 |
| **TikTok / Instagram** | 公开社交 profile（粉丝、bio、网站、邮箱） | 否 | TikTok 走 SSR JSON 稳定；IG 走 HTML 解析可能降级，需美国出口 |
| **last30days intent** | Reddit / HackerNews 上的采购讨论信号 | 否 | **基于独立项目，默认未启用**，见下方 |

### 可选依赖

`last30days_intent`（Reddit + HackerNews 上的采购讨论意图源）依赖一个独立的 `last30days` 调研引擎项目，ReachSurge 本身不打包它。默认未启用：仓库 `.gitignore` 已排除 `last30days/` 目录。需要的话自行安装该引擎到项目同级目录，意图源会在 import 成功后自动可用；import 失败则该源静默降级，其余功能不受影响。

---

## 多租户说明

每个用户对应一个独立的 SQLite 文件（位于 `LEADGEN_DATA_DIR` 下，按 `user_id` 命名），线索、知识库、邮箱富集缓存、发信/收信记录全部按 `user_id` 隔离。`user_id` 作为每个工具的必填入参贯穿全链路，互不可见。

凭证安全：SMTP / IMAP 密码、API key 落库前用 Fernet 对称加密（`cryptography` 库），密钥取自 `LEADGEN_FERNET_KEY` 或首次自动生成。代码内无任何明文密钥。

---

## License

MIT，见 [LICENSE](LICENSE)。

## 贡献

欢迎提 issue 和 PR。改动数据源或新增工具时，请保持「工具必须接进 `search_leads` 路由表，而非裸挂独立 Tool 让 agent 自己选」的原则——富集类（`enrich_*`）工具保持独立，发现类（拿新线索）工具应接入路由。

---

## 联系作者

有问题、想交流、或者想一起把获客流水线跑起来,扫码加我微信:

<p align="center">
  <img src="assets/wechat-qr.jpg" alt="耳朵 微信二维码" width="260">
</p>

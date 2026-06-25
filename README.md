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

## 3 分钟接入（小白首选入口）

不管你用哪个 AI 客户端（Claude Code、Cursor、Claude Desktop），把 ReachSurge 挂上去都只是一行命令 / 一段配置的事。**先把仓库 clone 到本地、装好依赖、建好 `.env`**（见下方[快速开始](#快速开始)），然后从下面三份里挑你用的那个客户端，照抄即可。

> 下面所有 `/绝对路径/` 都换成你 clone 后 reachsurge 目录的真实绝对路径，例如 Mac 上是 `/Users/你的用户名/reachsurge`，Windows 上是 `C:\\Users\\你的用户名\\reachsurge`（JSON 里反斜杠要双写）。

### A. Claude Code（命令行，最简单）

一条命令搞定：

```bash
claude mcp add reachsurge -- python /绝对路径/mcp_server.py
```

Claude Code 会自动把 `reachsurge` 注册成一个 stdio 类型的 MCP server。如果它没自动继承你的 `.env`（子进程透传有时受限），改用带环境变量的完整写法，把 `.env` 路径和数据目录显式传进去：

```bash
claude mcp add reachsurge \
  --env LEADGEN_ENV_FILE=/绝对路径/.env \
  --env LEADGEN_DATA_DIR=/绝对路径/data \
  -- python /绝对路径/mcp_server.py
```

挂上后在 Claude Code 里说「用 reachsurge 帮我搜德国 LED 采购商」就能用。

### B. Cursor

在项目根目录建 `.cursor/mcp.json`（或打开 Cursor 的 Settings → MCP → Add new MCP Server），写入：

```json
{
  "mcpServers": {
    "reachsurge": {
      "command": "python",
      "args": ["/绝对路径/mcp_server.py"],
      "env": {
        "LEADGEN_ENV_FILE": "/绝对路径/.env",
        "LEADGEN_DATA_DIR": "/绝对路径/data"
      }
    }
  }
}
```

保存后重启 Cursor，在 Composer / Chat 里 @reachsurge 或直接说「用 reachsurge 搜线索」即可调用。

### C. Claude Desktop

编辑 Claude Desktop 的配置文件（Mac: `~/Library/Application Support/Claude/claude_desktop_config.json`；Windows: `%APPDATA%\\Claude\\claude_desktop_config.json`），加入 `reachsurge`：

```json
{
  "mcpServers": {
    "reachsurge": {
      "command": "python",
      "args": ["/绝对路径/mcp_server.py"],
      "env": {
        "LEADGEN_ENV_FILE": "/绝对路径/.env",
        "LEADGEN_DATA_DIR": "/绝对路径/data"
      }
    }
  }
}
```

保存后完全退出再重开 Claude Desktop，在对话框左下角的工具图标里能看到 `reachsurge` 已连接即可。

> 三种客户端的 `env` 段是关键：MCP 子进程不一定能读到你自己 shell 里的环境变量，显式把 `LEADGEN_ENV_FILE` 指向项目根的 `.env`，ReachSurge 启动时会自己 `load_dotenv` 把里面的 key 注入进去。

---

## 5 分钟拿到第一条线索（最小可用路径）

想以最快速度验证「这玩意儿真的能搜出买家」？你只需要配两个 key，剩下的源全部自动降级跳过，照样能跑通。

**最小可用 = 只配这俩：**

| 变量 | 去哪申请 | 说明 |
|------|---------|------|
| `DEEPSEEK_API_KEY` | platform.deepseek.com | 免费档够测，LLM 精度过滤 / 产品词提取全靠它 |
| `SERPAPI_API_KEYS` | serpapi.com | 免费档每月有搜索额度，Google Maps 经销商源靠它 |

**这几个不用配、不用装、不用翻墙：**

- 不需要翻墙 —— Overpass（OpenStreetMap）的 `mail.ru` 镜像对中国大陆直连友好，DeepSeek / SerpApi 国内都能直连。
- 不需要 gosom 二进制 —— 那是可选的高质量邮箱源，缺了会自动降级到其他源（详见[这里](#gosom-二进制可选高质量邮箱源)）。
- 不需要 playwright —— 只有 europages / customs_importyeti 两个源用得到，不装只是这俩源降级（详见[这里](#playwright-与-chromium可选抓取依赖)）。

**操作步骤：**

1. 按[快速开始](#快速开始) clone 仓库、装依赖、建 `.env`，里面只填 `DEEPSEEK_API_KEY` 和 `SERPAPI_API_KEYS` 两行。
2. 按[3 分钟接入](#3-分钟接入小白首选入口)把 reachsurge 挂到你的 AI 客户端。
3. 直接对 agent 说这句话（原样复制，把「德国 LED」换成你自己的产品 + 市场）：

   > 帮我搜德国 LED 采购商，用 reachsurge 的 search_leads 工具，找到 10 条经销商线索存进库，user_id 用 trial。

   agent 会自动调 `search_leads` → 路由到 SerpApi 地图源 + Overpass → 跑 LLM 精度过滤 → 写进你的 SQLite。跑完后问它「列一下我刚存的线索」，就能看到公司名、网站、电话、邮箱（如果有）。

就这一句话，你已经完成了「找买家 + 入库」的核心闭环。

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

### 环境要求

| 项目 | 要求 |
|------|------|
| Python | 3.10 及以上 |
| 操作系统 | **Mac / Windows 原生即可跑主流程**（搜索 + 富集 + 发信 + 收信全部正常）；但 `europages`、`customs_importyeti`、`gosom` 三个源在 Mac/Windows 上可能不兼容会降级。**Linux / WSL 全功能**，推荐用 WSL 跑生产。 |
| 依赖项 | 见 `requirements.txt`；可选的 playwright / gosom 见下方说明 |

### 1. 克隆与装依赖

```bash
git clone https://github.com/erduo1998-cell/reachsurge.git
cd reachsurge
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium # 仅供 europages / customs_importyeti 抓取用
```

> **可选 · gosom 经销商源二进制**：`search_customers` 默认会调 gosom 源（Google Maps 高质量经销商档案 + 官网邮箱），它依赖 [`gosom/google-maps-scraper`](https://github.com/gosom/google-maps-scraper) 项目提供的 `google_maps_scraper` 二进制（内嵌 playwright，首次运行自动下载 chromium）。该二进制**不随仓库分发**——需自行编译或下载，放到项目 `bin/google_maps_scraper`，或用环境变量 `GOSOM_BIN` 指向任意路径。未提供时 gosom 源会在 `search()` 报错并降级，其余源不受影响。详细获取方式见 [gosom 二进制](#gosom-二进制可选高质量邮箱源)。

> **可选 · playwright 抓取依赖**：上面那行 `playwright install chromium` 只给 `europages`（欧洲 B2B 平台）和 `customs_importyeti`（美国海关提单）这两个源用，**不装它俩也能正常跑主流程**，只是这两个源会降级不可用。详细说明见 [playwright 与 chromium](#playwright-与-chromium可选抓取依赖)。

### 2. 配置环境变量

在项目根目录建 `.env`（示例见 `.env.example`）：

```dotenv
# ── 必填：大模型（精度过滤 / 公司画像 / 产品词提取都依赖它）──
DEEPSEEK_API_KEY=YOUR_DEEPSEEK_API_KEY
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash

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
| `DEEPSEEK_MODEL` | 否 | 模型名，默认 `deepseek-v4-flash`（`deepseek-chat` 是 7/24 起弃用的旧别名，仍可用但建议换新名） | 用默认值 |
| `HUNTER_API_KEY` | 否 | Hunter 邮箱富集 + Discover 找公司 | Hunter 相关源不可用 |
| `SERPAPI_API_KEYS` | 否 | SerpApi Google Maps 经销商源，逗号分隔多 key | serpapi_maps 源不可用 |
| `TAVILY_API_KEYS` | 否 | Tavily 展会展商搜索，逗号分隔多 key | 展会路由不可用 |
| `CAPSOLVER_API_KEY` | 否 | europages 过 Cloudflare WAF | europages 源不可用（其他源不受影响） |
| `GOSOM_BIN` | 否 | gosom `google_maps_scraper` 二进制路径，默认项目内 `bin/google_maps_scraper` | gosom 源不可用（其他源不受影响） |
| `HTTPS_PROXY` / `HTTP_PROXY` | 否 | 出口代理 | 部分对出口 IP 敏感的源（社交、海关）可能失败 |
| `LEADGEN_DATA_DIR` | 否 | SQLite 与知识库存储目录，默认 `data/` | 用默认值 |
| `LEADGEN_ENV_FILE` | 否 | 额外的 `.env` 文件路径，启动时 `load_dotenv` 注入。自动找项目根 `.env`；仅当 `.env` 不在项目根时才需显式指定绝对路径 | 不加载额外 env |
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

## gosom 二进制（可选高质量邮箱源）

`gosom` 是一个**可选的** Google Maps 经销商高质量抓取源，邮箱命中率比其他地图源高，但有个门槛：它依赖一个外部二进制，不是装个 pip 包就完事。

- **平台限制**：仅 **Linux / WSL** 可用（Mac / Windows 原生跑不起来，会自动降级到其他地图源，主流程不受影响）。
- **怎么获取**：二进制来自 [`gosom/google-maps-scraper`](https://github.com/gosom/google-maps-scraper) 项目，需自行编译或下载 release，得到一个叫 `google_maps_scraper`（或 `google_maps_scraper.exe`）的可执行文件。
- **怎么放**：把它丢到项目下 `bin/google_maps_scraper`（默认路径），或者设环境变量 `GOSOM_BIN` 指向任意绝对路径，例如 `.env` 里写 `GOSOM_BIN=/opt/gosom/google_maps_scraper`。
- **不装会怎样**：gosom 源在 `search()` 时报「二进制不存在」并自动降级，其余地图源（SerpApi / Overpass）照常工作。**对小白来说，跳过它完全没问题，等你要榨干邮箱命中率了再回头装。**

> 该二进制内嵌 playwright，首次运行会自己下载 chromium，无需你手动 `playwright install`。

## playwright 与 chromium（可选抓取依赖）

`europages`（欧洲最大 B2B 平台）和 `customs_importyeti`（美国海关提单）这两个源用 headless chromium 抓页面，所以要额外装 playwright：

```bash
pip install playwright
playwright install chromium
```

- **平台兼容性**：这两个源在 **Mac / Windows 原生环境可能不兼容**（europages 还要过 Cloudflare WAF，依赖 CAPSOLVER）。缺失 chromium 或平台不兼容时，对应源会降级跳过，不影响其他源。
- **推荐环境**：要稳定用这两个源，建议在 **Linux / WSL** 下跑。
- **不装会怎样**：主流程（搜经销商、富集邮箱、发信、收信）完全不受影响，只是少了欧洲 B2B 档案和美国海关提单验真这两条数据。

---

## FAQ / 常见报错排查

新手跑不通时，先对照这张表。90% 的问题都在这里。

| 现象 | 原因 | 解决 |
|------|------|------|
| **配了 key 但 LLM 还是在降级 / 没过滤** | MCP 子进程没加载到你的 `.env` | 在客户端配置的 `env` 段里加 `"LEADGEN_ENV_FILE": "/绝对路径/.env"`，或确认 `.env` 在项目根目录（会被自动加载）。重启客户端再试 |
| **europages / customs 报 `executable doesn't exist`** | 没装 chromium，或当前平台不兼容 | 跑 `playwright install chromium`；Mac/Windows 上这俩源本来就可能不兼容，会自动降级，换 Linux/WSL 可解 |
| **gosom 源报「二进制不存在」** | 没下载 `google_maps_scraper` 二进制 | 要么按[这里](#gosom-二进制可选高质量邮箱源)装好并设 `GOSOM_BIN`；要么直接忽略（会降级到其他地图源） |
| **代理连不上 / 请求超时** | 代理配错或没开 | 检查 `LEADGEN_PROXY`（优先级最高，专给 europages/gosom/customs/scout 用）或 `HTTPS_PROXY` 是否指向能用的代理；不需要代理就留空走直连 |
| **`save_user_config` 报权限错误 / 写库失败** | 数据目录不可写 | 检查 `LEADGEN_DATA_DIR` 指向的路径是否存在、当前用户有无写权限；留空则用项目下 `data/`，确保项目目录可写 |
| **`send_email` 报 SMTP 认证失败** | SMTP 凭证没正确加载 | SMTP 凭证不走 `.env`，是通过 `save_user_config` 工具录入并加密落库的；确认 `save_user_config` 调成功过、`smtp_host`/`smtp_user`/`smtp_password` 没填错（注意很多邮箱要的是应用专用密码，不是登录密码） |
| **客户端里看不到 reachsurge 工具** | MCP server 没挂上或启动失败 | 检查配置里 `command` 是 `python`、`args` 是 `["/绝对路径/mcp_server.py"]`、路径真实存在；在终端先手动跑 `python /绝对路径/mcp_server.py` 看有无报错（缺依赖就 `pip install -r requirements.txt`） |
| **搜出来的线索很少 / 0 条** | 多数源因缺 key 降级了 | 只配了 DeepSeek 时主要走 Overpass；想线索多就再配 `SERPAPI_API_KEYS`，按[5 分钟教程](#5-分钟拿到第一条线索最小可用路径)把最小组合配齐 |

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

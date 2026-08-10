# ReachSurge 首次运行向导（给用户和 Agent）

这份文档用于 ReachSurge 安装完成后的第一次连接。适用于 Claude、Codex、WorkBuddy、豆包及其他支持本地 stdio MCP 的智能体。

## 一句话开始

把下面这句话发给你的 Agent：

> 请先调用 ReachSurge 的 `setup_status`，按返回结果一步步带我完成首次设置。不要让我在聊天中粘贴任何 API Key、Token、邮箱密码、Cookie 或 `.env` 内容。

ReachSurge 会在服务端拦住尚未完成设置的业务工具，因此即使 Agent 没读 README，也会被引导回首次运行流程。

## Agent 必须执行的流程

```text
调用 setup_status
        │
        ├─ needs_profile ─→ 询问 3 类非秘密业务信息
        │                  └─ 调用 save_user_config
        │
        ├─ needs_security_fix ─→ 让用户只在本机修复 .env 权限
        │
        ├─ ready_to_complete ─→ 询问是否启用可选能力
        │                      └─ 用户确认后调用 complete_setup
        │
        ├─ complete ─→ 不再重复向导，直接工作
        │
        └─ blocked / needs_repair ─→ 按返回提示修复本地状态
```

首次运行期间，只有 `setup_status`、`save_user_config`、`get_user_config`、`complete_setup` 可以使用。

### 第 1 步：检查，不猜测

Agent 调用：

```text
setup_status
```

这个工具只返回本地目录是否可写、业务资料缺什么、各项能力是否已经配置。它不会联网、不会消耗 API 额度，也不会返回秘密的内容、长度或前后缀。

### 第 2 步：向用户收集 3 类信息

缺少资料时，Agent 应用小白能理解的问题逐项询问：

1. 你主要生产或销售什么产品？属于什么行业或品类？
2. 请简单介绍产品。最好包括优势、规格、认证、MOQ（最小起订量）和交期；不知道的可以不填。
3. 你准备开发哪些国家或地区的客户？可以填多个。

称呼、每日发信偏好上限是可选项。Agent 收齐后调用一次 `save_user_config`。如果分多次保存，ReachSurge 会保留之前已填的字段，不会把它们清空。

### 第 3 步：决定是否启用可选能力

没有任何 API Key 也能完成首次设置，并使用本地 CRM、知识库和 OpenStreetMap 等零 Key 能力。不要为了“安装成功”一次申请全部 Key。

| 想要的能力 | 本机 `.env` 配置项 | 必选？ |
|---|---|---:|
| 本地保存、读取线索和知识库 | 无 | 否 |
| OpenStreetMap 搜索 | 无 | 否 |
| 智能筛选、产品词提取和公司画像 | `DEEPSEEK_API_KEY` | 否 |
| Google Maps 搜索 | `SERPAPI_API_KEYS` | 否 |
| 查找公司公开邮箱 | `HUNTER_API_KEY` | 否 |
| 展会和展商搜索 | `TAVILY_API_KEYS` | 否 |
| Europages 可选 WAF 处理 | `CAPSOLVER_API_KEY` | 否 |
| 本机 Google Maps 抓取 | `GOSOM_BIN` | 否，且它不是 Key |
| 发信 / 收信 | SMTP / IMAP 环境变量 | 否，默认关闭 |

### 第 4 步：完成并自动清理

用户确认“先这样使用”或“本地配置完成”后，Agent 调用：

```text
complete_setup
```

ReachSurge 会再次检查资料和本地安全条件，然后：

- 原子写入一个只含版本、完成时间和本地命名空间的完成标记；
- 自动删除 `.pending.json` 临时首次运行文件；
- 不保存任何 Key、密码、账号或能力快照；
- 当前进程立即解除业务工具限制；
- 下次启动识别完成标记，不再重复询问。

完成标记必须保留，否则 ReachSurge 会把下一次启动视为新的首次运行。若产品资料数据库被删除或完成标记损坏，向导会安全地重新出现。

## API Key 的官方申请步骤

额度、价格和免费政策可能变化，请以服务商当前控制台为准。给 ReachSurge 单独创建 Key，便于限额、审计和吊销。

### DeepSeek

用途：智能筛选、产品词提取和公司画像。

1. 打开 [DeepSeek 开放平台注册页](https://platform.deepseek.com/sign_up)注册并登录。
2. 进入 [API Keys](https://platform.deepseek.com/api_keys)，创建一个 Key。
3. 在聊天之外，把它写入 ReachSurge 项目根目录的 `.env`：`DEEPSEEK_API_KEY=你的值`。
4. 重启 ReachSurge MCP，再让 Agent 调用 `setup_status`；Agent 只应报告“已配置/未配置”。

官方说明：[DeepSeek API 文档](https://api-docs.deepseek.com/)。

### SerpApi

用途：Google Maps 企业搜索。

1. 在 [SerpApi 官方注册页](https://serpapi.com/users/sign_up)创建账户。
2. 登录后打开 [API Key 管理页](https://serpapi.com/manage-api-key)。
3. 把 Key 写入本机 `.env`：`SERPAPI_API_KEYS=你的值`。
4. 多个 Key 用英文逗号分隔；第一次只填一个即可。重启 MCP 后复查。

官方说明：[Google Maps API 文档](https://serpapi.com/google-maps-api)。

### Hunter

用途：查找企业公开邮箱。

1. 在 [Hunter 官方注册页](https://hunter.io/users/sign_up)创建账户。
2. 登录后进入账户的 [API Keys 页面](https://hunter.io/api-keys)，创建 Key。
3. 写入本机 `.env`：`HUNTER_API_KEY=你的值`，然后重启 MCP。

官方说明：[Hunter API 文档](https://hunter.io/api-documentation)。邮箱查找和验证可能消耗额度。

### Tavily

用途：公开网页、展会与展商搜索。

1. 打开 [Tavily 控制台](https://app.tavily.com/)注册或登录。
2. 从控制台复制 API Key。
3. 写入本机 `.env`：`TAVILY_API_KEYS=你的值`。
4. 多个 Key 用英文逗号分隔；重启 MCP 后复查。

官方说明：[Tavily Quickstart](https://docs.tavily.com/documentation/quickstart)。

### CapSolver

用途：Europages 的可选 WAF 处理，可能产生费用，没有明确需求时跳过。

1. 打开 [CapSolver 控制台](https://dashboard.capsolver.com/)注册或登录。
2. 从控制台首页获取 API Key，并确认账户有可用余额。
3. 写入本机 `.env`：`CAPSOLVER_API_KEY=你的值`，然后重启 MCP。

官方说明：[CapSolver Getting Started](https://docs.capsolver.com/en/guide/getting-started/)。

### GoSOM 不是 API Key

GoSOM 是一个可选的本机二进制程序。缺少时 ReachSurge 会跳过它，不影响首次设置。安装或排错请把 [GoSOM Agent 修复指南](GOSOM_AGENT_FIX_GUIDE.zh-CN.md)交给 Agent。

## 密钥只在本机填写

Agent 必须明确告诉用户在哪个本地文件操作，但不得要求用户把秘密粘贴到聊天或工具参数。

1. 在 ReachSurge 项目根目录复制 `.env.example` 为 `.env`。
2. 用本机文本编辑器填入所需的可选配置。
3. macOS / Linux 执行 `chmod 600 .env`，限制为仅当前用户可读写。
4. 重启 ReachSurge MCP，再调用 `setup_status`。

`0600/0700` 是 macOS/Linux 的权限保护；Windows 使用不同的 ACL 机制，ReachSurge 当前只检查文件是否存在，不声称自动验证 Windows ACL。请把 `.env` 和数据目录放在自己的用户目录中，不要放在共享目录。

严禁把真实 Key 写进 README、MCP 客户端 JSON、截图、日志或 Git。若秘密曾出现在聊天、截图或提交记录中，应立即在服务商控制台吊销并重新创建。

## 邮箱配置首次运行请跳过

发信和收件箱读取默认关闭，不是完成向导的条件。推荐以后使用独立测试邮箱配置，并保持：

```dotenv
REACHSURGE_ENABLE_SEND_EMAIL=0
REACHSURGE_ENABLE_CHECK_INBOX=0
```

当前 ReachSurge 使用 SMTP/IMAP 用户名和应用专用密码，不支持 OAuth。不要为了兼容而关闭 MFA、组织安全默认值或邮箱安全策略。Google 应用专用密码需要先启用两步验证，且部分组织账户或高级保护账户不会提供该选项，详见 [Google 官方说明](https://support.google.com/accounts/answer/185833)。如果邮箱只允许 OAuth，就暂时不要接入。

## 常见问题

### 配置 Key 后仍显示未配置

`.env` 只在 MCP 进程启动时加载。保存文件后重启 ReachSurge MCP，再调用 `setup_status`。不要批量结束电脑上的其他 Python 或 Agent 进程。

### 提示 `.env` 权限不安全

在 macOS / Linux 的项目目录执行 `chmod 600 .env`，然后重新调用 `setup_status`。工具不会显示 `.env` 的内容。

### 为什么业务工具返回 `SETUP_REQUIRED`

这是正常保护。先调用 `setup_status`，保存缺失的业务资料，再调用 `complete_setup`。

### MCP 根本无法启动，Agent 调不到 `setup_status`

`setup_status` 只能在 MCP 进程成功启动后运行。先检查 MCP 配置中的 command 是否是虚拟环境启动文件的绝对路径，以及 `LEADGEN_DATA_DIR` 的父目录是否存在、当前用户是否有写权限；修复后重启当前 MCP 客户端。

### 我想重新走一遍向导

不要随意删除数据目录。完成标记和业务数据库有关联；如确实需要重置，请先备份 `LEADGEN_DATA_DIR`，再按照项目 issue 模板说明场景寻求帮助。

## Agent 的不可违反规则

1. 先调用 `setup_status`，不得根据 README 或记忆猜测状态。
2. 只收集产品、行业、市场等非秘密资料。
3. 所有 API Key 都是可选项，零 Key 可以完成。
4. 不接收、不读取、不回显、不记录任何 Key、Token、密码、Cookie 或完整 `.env`。
5. 不为验证安装自动调用付费 API。
6. 邮件默认跳过；真发信仍须逐次展示收件人、主题、正文并取得用户确认。
7. 用户本地修改 `.env` 后只重启 ReachSurge MCP，再重新检查。
8. `complete_setup` 成功后停止重复提问，直接执行用户的业务任务。

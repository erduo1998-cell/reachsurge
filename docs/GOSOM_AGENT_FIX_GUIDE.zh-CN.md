# ReachSurge：让用户 Agent 解决 gosom 二进制缺失

> 把本文完整交给能够操作本机终端的 Agent。本文不是要求普通用户手工执行所有命令，而是给 Agent 的安全运行手册。

## 任务目标

解决以下错误或同类问题：

```text
gosom 二进制不存在
gosom 还缺少它自己的二进制
google_maps_scraper not found
```

完成后必须得到以下结果之一：

- **不需要 gosom：** 更新 ReachSurge 后让它自动跳过该可选来源，其他功能继续正常运行。
- **需要 gosom：** 安装与 ReachSurge 运行环境匹配的 `google_maps_scraper`，设置 `GOSOM_BIN`，验证后重启 ReachSurge MCP。

## 先理解问题

ReachSurge 不是通过 `pip` 安装 gosom。它会启动一个本机可执行文件：

```text
google_maps_scraper
```

gosom 只是高级可选的 Google Maps 采集来源。它缺失时不会影响 ReachSurge MCP、本地 CRM、OSM，以及已经配置的 SerpApi、Hunter、Tavily 等来源。新版 ReachSurge 会在完全未安装时静默跳过；如果默认位置已经有文件但不可执行，或用户显式填写了无效的 `GOSOM_BIN`，则只报告 gosom 配置错误，仍不会中断其他来源。

官方来源只有：

- [gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper)
- [官方 Releases](https://github.com/gosom/google-maps-scraper/releases)
- [最新 Release API](https://api.github.com/repos/gosom/google-maps-scraper/releases/latest)

gosom 的 Docker 镜像、Python 包或 Agent Skill 都不能直接填写到 `GOSOM_BIN`。ReachSurge 当前需要的是本机可执行文件的绝对路径。

新版 ReachSurge 还会默认设置 `DISABLE_TELEMETRY=1`，关闭 gosom 上游遥测；同时仅向 gosom 子进程传递运行必需的环境变量，并通过权限受限的临时文件传代理，避免 API Key、邮箱凭证和代理密码出现在第三方子进程环境或命令行中。

## 执行边界

你可以：

- 读取 ReachSurge 仓库、运行环境和与 gosom 有关的配置。
- 在用户目录下安装官方 gosom 二进制。
- 只修改 ReachSurge 实际使用的 `GOSOM_BIN`。
- 安全重启当前 ReachSurge MCP 进程。

你不可以：

- 输出、复制或上传 `.env` 全文。
- 在聊天、命令或日志中暴露 API Key、邮箱密码、Cookie、Token 或代理凭证。
- 使用 `curl ... | sh`、不明镜像、第三方 fork 或聊天附件。
- 使用 `sudo`，或擅自安装 Rosetta、Docker、Go、系统包管理器软件。
- 使用 `pkill python`、`killall python` 等模糊命令终止无关进程。
- 暂存、提交或推送下载的二进制文件。
- 覆盖或删除已有的可用版本。

如果需要管理员权限或安装新的系统级软件，停止并说明原因。

## 第一步：更新 ReachSurge

先定位真正的 ReachSurge 仓库。至少应同时存在：

```text
mcp_server.py
sources/gosom.py
.env.example
README.md
```

读取当前目录和上级最近的 `AGENTS.md`。确认 Git remote 指向用户预期的 ReachSurge 仓库，并先检查工作树：

- 工作树干净：可使用 `git pull --ff-only` 获取最新版。
- 存在本地修改：不要覆盖、清理或 stash 用户改动；先报告冲突风险并采用不会丢数据的更新方式。
- 不是 Git 安装：从官方 ReachSurge 发布源获取新版，保留原配置和数据目录。

更新后确认 `sources/gosom.py` 中存在 `availability()`。如果用户不需要本地 Google Maps 抓取，直接重启 ReachSurge MCP 并进入“验收”；不需要安装 gosom。

## 第二步：确认实际运行环境

以“运行 ReachSurge Python 的环境”为准，不要只看桌面系统：

- macOS Python 使用 Darwin 二进制。
- Windows Python 使用 Windows `.exe`。
- WSL Python 使用 Linux 二进制。
- 容器或远程 Python 必须使用容器或远程环境内部的二进制。

记录但不要泄露：

- ReachSurge 仓库绝对路径。
- ReachSurge 实际使用的 Python 或虚拟环境。
- 操作系统和 CPU 架构。
- `.env` 是否存在、`GOSOM_BIN` 是否非空；不要显示其他变量值。

常见架构映射：

| 系统报告 | Release 常用名称 |
|---|---|
| `x86_64`、`AMD64` | `amd64` |
| `arm64`、`aarch64` | `arm64` |
| Darwin | `darwin` |
| Linux / WSL | `linux` |
| Windows | `windows` |

## 第三步：优先检查已有二进制

按顺序检查有限范围，不要扫描整个磁盘：

1. `.env` 当前设置的 `GOSOM_BIN`。
2. `<ReachSurge>/bin/google_maps_scraper`。
3. Windows 下的 `<ReachSurge>\bin\google_maps_scraper.exe`。
4. 当前 PATH 中的 `google_maps_scraper` 或 `google-maps-scraper`。
5. 用户目录内明确属于 ReachSurge/gosom 的工具目录。

候选文件必须满足：

- 是普通文件，路径可转换为绝对路径。
- 当前用户能够执行；macOS/Linux 需要执行权限。
- 文件格式和 CPU 架构与 ReachSurge 运行环境匹配。
- 帮助命令能在短超时内启动。
- 帮助文本至少支持 ReachSurge 使用的参数：

```text
-input
-results
-json
-email
-depth
-c
-lang
-exit-on-inactivity
```

帮助内容可能输出到 stderr，帮助命令也可能返回非零状态；结合输出内容和进程是否正常启动判断。找到通过检查的现有程序后，不要无故下载新版本。

## 第四步：安全安装（确实需要时）

安装优先级：

1. 官方 Release 中与当前系统和架构完全匹配的二进制。
2. 已经安装 Go 时，从官方固定 Release tag 构建原生二进制。
3. 没有安全安装条件时停止，或选择不用 gosom。

### 方案 A：官方 Release

从 GitHub 官方 Latest Release API 动态读取：

- `tag_name`
- 匹配的资产名称
- `browser_download_url`
- 资产 `digest`

不要在自动化中永久硬编码“最新版”版本号，也不要假定官方提供所有 ARM64 资产。

下载和验证规则：

1. 使用新建的临时目录。
2. 只接受 `github.com/gosom/google-maps-scraper` 的资产。
3. 下载后先计算 SHA-256，必须与 Release API 的 `digest` 一致。
4. 校验前不执行文件。
5. 校验后添加执行权限并检查帮助参数。
6. 安装到用户级、版本化目录，不覆盖已有可用版本。

推荐安装位置：

macOS/Linux：

```text
<用户数据目录>/reachsurge/gosom/<tag>/google_maps_scraper
```

Windows：

```text
%LOCALAPPDATA%\ReachSurge\gosom\<tag>\google_maps_scraper.exe
```

如果当前是 Apple Silicon 或其他 ARM64，而 Release 只有 AMD64：不要擅自安装 Rosetta，也不要把 AMD64 文件当成原生文件；使用下面的源码构建方案，或安全跳过。

### 方案 B：从官方固定 tag 构建

只在系统已经安装可用 Go、且不需要管理员权限时执行：

1. 从官方 Release API 读取固定 `tag_name`。
2. 在临时目录 clone `https://github.com/gosom/google-maps-scraper.git`。
3. checkout 固定 tag，不直接从变化中的 `main` 构建。
4. 读取该 tag 的 `go.mod`，确认当前 Go 或其安全的自动 toolchain 能满足版本要求。
5. 运行官方构建方式，生成当前系统的原生 `google_maps_scraper`。
6. 核对文件架构与帮助参数。
7. 安装到用户级版本目录，并清理临时源码。

如果必须升级或安装 Go，先停止并征得用户授权。

## 第五步：设置 `GOSOM_BIN`

只修改 ReachSurge 实际读取的 `.env`，使用通过验证的绝对路径：

```dotenv
GOSOM_BIN=/绝对路径/google_maps_scraper
```

Windows 推荐正斜杠，减少转义问题：

```dotenv
GOSOM_BIN=C:/Users/用户名/AppData/Local/ReachSurge/gosom/vX.Y.Z/google_maps_scraper.exe
```

修改规则：

- 已有 `GOSOM_BIN` 时只替换这一行；没有时只追加这一行。
- 不把本机真实路径写进 `.env.example`。
- 不显示或改动其他环境变量。
- 不创建包含整份 `.env` 的公开备份。
- macOS/Linux 保持 `.env` 仅当前用户可读。

## 第六步：验证

使用 ReachSurge 实际运行的 Python，在项目目录进行无网络验证：

```bash
<ReachSurge 的 Python> -c "import mcp_server; from sources.gosom import availability, _resolve_binary; print(_resolve_binary()); print(availability())"
```

预期结果：解析出的路径正确，且状态为 `(True, '可用')`。输出中不得包含 `.env` 全文或任何密钥。

如果状态不可用，按提示检查：

- 文件不存在：核对 `GOSOM_BIN` 的绝对路径。
- 不是普通文件：不要把目录、Docker 镜像或 Skill 路径填进去。
- 没有执行权限：仅对确认过的目标文件添加执行权限。
- 架构不匹配：换匹配版本或从官方 tag 原生构建。

真实网络 smoke test不是安装成功的必要条件。用户同意执行时，只做一个通用的小查询、最多 1 条结果，设置明确超时。首次运行可能下载浏览器组件；网络限制或 Google Maps 限流应与“二进制接线失败”分开报告。

## 第七步：安全重启 MCP

ReachSurge 在进程启动时加载 `.env`，所以修改后必须重启或重新连接 ReachSurge MCP。

优先使用 MCP 客户端自己的重启/重新连接功能。如果必须终止进程：

- 先核对 PID 的完整命令行确实指向当前 ReachSurge 的 `mcp_server.py` 或 `reachsurge-mcp`。
- 只正常终止这个确定的进程。
- 不批量终止 Python、Codex、Claude、Cursor 或其他 MCP Server。
- 重启前确认没有正在发送邮件、读取收件箱或执行需要保留的后台任务。

gosom 超时或异常退出后，极少数平台可能残留它启动的浏览器子进程。只有在核对完整命令行确认属于本次 gosom 任务后才终止该进程，不要按 `Chrome`、`Chromium` 或 `python` 名称批量清理。

重启后确认 ReachSurge 能重新列出工具，普通本地工具仍可用。

## 验收

### 不需要 gosom

- [ ] ReachSurge 已更新到包含自动降级修复的版本。
- [ ] `availability()` 返回不可用及友好原因。
- [ ] `enabled_sources()` 不再把 `gosom_maps` 列为可用来源。
- [ ] 搜索不会再出现“gosom 二进制不存在”的采集错误。
- [ ] 其他 ReachSurge 工具和数据源正常。

### 需要 gosom

- [ ] 系统和 CPU 架构已确认。
- [ ] 二进制来自官方 Release，或由官方固定 tag 构建。
- [ ] Release 资产 SHA-256 已核验。
- [ ] 文件架构、执行权限和帮助参数均通过。
- [ ] `GOSOM_BIN` 是正确的绝对路径。
- [ ] 没有输出、修改或提交其他密钥。
- [ ] `availability()` 返回 `(True, '可用')`。
- [ ] ReachSurge MCP 已安全重启，工具列表正常。

## 回滚

如果新二进制无法启动：

1. 不删除原有可用版本。
2. 恢复修改前的 `GOSOM_BIN`，或将其留空以使用新版自动降级。
3. 安全重启 ReachSurge MCP。
4. 确认其他来源恢复正常。
5. 只报告真实失败层：无匹配架构、校验失败、Go 版本不足、构建失败、权限、浏览器组件、网络或限流。

不要通过关闭校验、下载第三方二进制、提升系统权限或泄露代理凭证来绕过失败。

## 最终汇报格式

```markdown
## gosom 配置结果

- 状态：已启用 / 已安全跳过 / 未完成
- ReachSurge 环境：macOS arm64 / Linux amd64 / Windows amd64 / 其他
- 来源：已有程序 / 官方 Release / 官方固定 tag 构建 / 未安装
- gosom 版本：<tag 或无法确认>
- 安装位置：<绝对路径；未安装则写“无”>
- SHA-256：<校验值；未下载则写“不适用”>
- GOSOM_BIN：已设置 / 已留空自动降级
- 离线验证：通过 / 失败
- 网络 smoke test：通过 / 未执行 / 网络受限
- MCP 重启：已安全重启 / 无需重启
- 其他 ReachSurge 工具：正常 / 存在问题

限制或下一步：
- <只写真实限制，不输出密钥>
```

## 合规提醒

gosom 的 `-email` 会访问 Google Maps 结果中的企业公开网站，首次运行可能下载浏览器组件，也会消耗本机 CPU、内存和网络资源。大批量抓取可能触发限制。使用者需要遵守目标网站条款、当地法律和隐私/营销规则。

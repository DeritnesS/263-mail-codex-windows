# Windows 11：由 Codex 带着一步步安装

本文同时给使用者和她的 Codex 看。使用者只需配合登录、输入秘密和核对结果；命令、配置和错误分析主要由 Codex 完成。

## 1. 准备本地项目

使用者在 GitHub 点击 **Code → Download ZIP**，下载后右键 **全部解压缩**。不要在压缩包预览里直接双击程序。建议放到用户目录下的普通本地文件夹，例如 `C:\Users\你的用户名\CodexProjects\263-mail-codex-windows`；避开 OneDrive/共享网盘和系统目录。

在 Windows 本机 Codex 打开该文件夹，发送 README 中的启动指令。不要使用只运行在网页云端的任务；本机 Windows 凭据和缓存不会自动出现在云环境中。

Codex 检查 Python。支持范围为 Python 3.11–3.14，建议使用公司允许的 64 位受支持安装版；没有时从 [Python 官方 Windows 页面](https://www.python.org/downloads/windows/) 引导安装。不要让新手自行找第三方下载站。Codex 桌面版可按原生 Windows 路线工作，WSL 不是本项目必要条件，见 [OpenAI 官方说明](https://learn.chatgpt.com/docs/windows/windows-sandbox)。

## 2. 安装依赖：Codex 完成

双击 `01-install.cmd` 或由 Codex 从普通 PowerShell 运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

只为当前脚本进程设置执行策略，不修改全局策略；公司禁用脚本时联系 IT。脚本创建本项目的 `.venv`，从 Python 包源安装依赖，随后运行离线测试。通过后才进入邮箱凭据配置。

不需要激活虚拟环境。后面的 `& .\.venv\Scripts\python.exe` 都使用明确的本机解释器。

## 3. 确认 263 账户设置：她配合查看

请她打开平时使用的网页版邮箱，自己登录。在实际界面找到设置中的“客户端/POP/IMAP/安全”等相关项；菜单名字可能不同，Codex 不能编造统一按钮路线。找不到就请公司的邮箱管理员确认以下四项：

| 信息 | 说明 |
|---|---|
| 登录名 | 本人完整企业邮箱地址，不只是用户名 |
| IMAP 主机 | 官方通用地址 `imap.263.net`，仍以本公司账户设置为准 |
| 加密端口 | 通常为 TLS 993，不能为了接通改为明文或跳过证书 |
| 验证方式 | 邮箱密码或单独的客户端授权码，依公司当前策略确定 |

[263 官方客户端配置表](https://download.263.net/263/helpcenter/client/20160603/970.html)列出了主机与端口，但不能证明某个账号已经开通服务，也不能证明她应使用哪种凭据。

## 4. 设置账户与凭据：她自己输入

请她自己双击 `02-configure.cmd`。Codex 一次引导一个提示：完整邮箱地址、主机、端口。接着在遮蔽输入框/终端提示中输入密码或客户端授权码。输入时不显示字符是正常行为，不需要截图证明。

密码仅存当前用户的 **Windows 凭据管理器 → Windows 凭据 → 普通凭据**，服务名称以 `Mail263Codex:` 开头。密码不进聊天、命令参数、配置文件或环境变量。Codex 不应录屏、复制或读取密码。

配置是 `%LOCALAPPDATA%\Mail263Codex\config.json`，只有非秘密设置。更换邮箱/服务器后需要重新录入匹配的凭据；每个账号有独立缓存命名空间。

## 5. 在线检查和选择范围：Codex 完成

```powershell
& .\.venv\Scripts\python.exe -m mail263 doctor
& .\.venv\Scripts\python.exe -m mail263 folders
```

doctor 应显示 TLS 登录和列目录成功。失败就按故障表定位，不继续猜密码。

Codex 根据真实列表修改 `config.json` 的 `sync_folders`，通常先选 `INBOX`，再加她确有需要的已发送目录，绝不猜中文目录名。`sync_since` 是固定的起始日期，默认当年 1 月 1 日；她要查跨年账期时再往前扩展。`sync_max_messages` 默认每目录每轮 100 封。建议先用 30 天小范围验证，再设正式账期分批补全。

范围改变后，应通过相同范围的刷新完成新覆盖，不要把旧范围的成功时间当成新范围已覆盖。减少目录或日期不会自动删除留存原件；历史留存清理需要单独处理。当前索引会按最近一次成功获得的目录清单标记有效范围，报告中须展示范围。

## 6. 首次同步：分批补齐

```powershell
& .\.venv\Scripts\python.exe -m mail263 refresh
& .\.venv\Scripts\python.exe -m mail263 status
```

刷新每个目录先取 UID 清单，再下载本地缺少的原邮件，已存在的原件不重复下载。首次原件包含附件，因此初次用时和空间取决于邮件体积；不是只取标题。程序限制单封 25 MiB，每目录每轮最多 100 MiB 新传输，且有时间/数量边界。

`partial` 表示尚有待下载或跳过项，不能说“同步完成”。Codex 继续相同入口分批同步并观察 pending 下降；连续无进展时检查原因，不无限循环。超大邮件保持明确缺口，由网页版查看。`success` 才能说明该目录本轮指定范围已完整覆盖。登录失败仍可查已有缓存，但须报告更新时间。

## 7. 接入 Codex：Codex 完成

先生成使用本机实际绝对路径的配置：

```powershell
& .\.venv\Scripts\python.exe -m mail263 mcp-config
```

由 Codex 读取现有的 Codex `config.toml`，备份后只合并 `[mcp_servers.mail263]`，保留其他设置。默认位置是当前 Codex 主机的 `~/.codex/config.toml`；若设置了 `CODEX_HOME`，使用它的实际位置。不要在另一账户或 WSL 的配置里误添加。

如果 CLI 在 PATH 中，也可使用：

```powershell
$mailPython = (Resolve-Path .\.venv\Scripts\python.exe).Path
codex mcp add mail263 -- $mailPython -m mail263 server
codex mcp get mail263
```

不要同时重复添加同名服务器。较大初次刷新可在 CLI 完成；MCP 的生成配置含较长工具超时。使用 CLI 添加时，按生成片段补足超时。图形界面路线是设置中的 MCP servers → Add server → STDIO，使用生成片段中的 command/args。实际界面可能变化，以 [OpenAI MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)和当前应用为准。

保存后重启该 MCP 或 Codex。必须实际调用 `system_status` 和 `mail_search`，再调用 `mail_refresh`，不能仅凭配置写入就说接入完成。

## 8. 设置每 5 分钟增量同步

现场验收通过后，Codex 说明这个任务只做邮件缓存更新，在用户登录且电脑唤醒时运行，再执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\scheduled-sync.ps1 -Action Install
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\scheduled-sync.ps1 -Action Show
```

任务名 `Mail263Codex-IncrementalSync`，固定同名更新，不创建重复任务；使用当前登录用户和普通权限，不保存 Windows 密码，不设置唤醒、不发通知。后台和手动刷新共用一个跨进程锁，避免同时写入。休眠、关机或退出登录期间没有同步，恢复后检查一次状态；想查“现在最新”仍应主动刷新。

`%LOCALAPPDATA%\Mail263Codex\last-scheduled-result.json` 是最近一次任务状态，不含正文。Windows 任务已注册不代表实际同步成功；按验收步骤等待至少一次任务运行，并检查每目录截止时间。公司禁止计划任务时交 IT，可先保留本地查询与按需刷新，不为接通而提升长期权限。

## 9. 停用、升级和卸载

- 暂停自动同步：用任务计划程序禁用这个任务，或运行 `scheduled-sync.ps1 -Action Remove`。
- 停用 Codex 工具：在同一 Codex 主机移除/禁用 `mail263` MCP（CLI 可用 `codex mcp remove mail263`）。
- 升级：停用同步、备份本地数据，更新代码，再运行 `01-install.cmd`；检查数据库版本和测试后恢复。项目目录移动后必须重新生成 MCP 配置和注册计划任务。
- 卸载程序不会自动删除邮箱原件或凭据。由用户确认后，删除本项目目录、`%LOCALAPPDATA%\Mail263Codex` 和凭据管理器内对应条目；不要误删其他凭据。
- 本地缓存不是企业邮件备份系统，企业归档和财务原件保管继续遵守公司制度。

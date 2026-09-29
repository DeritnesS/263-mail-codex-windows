# 资料来源与版本边界

整理日期：2026-09-29。下列是实现和教程的主要依据；界面和企业策略会变化，对方 Codex 部署时仍须核对当前机器。

| 来源 | 用途 |
|---|---|
| [263 官方客户端配置地址](https://download.263.net/263/helpcenter/client/20160603/970.html) | 官方列出 imap.263.net 和 SSL 993；具体账户是否开通 IMAP、密码或授权码要求仍以本人账户/管理员为准 |
| [OpenAI：MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) | 本机 STDIO、`mcp_servers` 配置、CLI 添加和重启；没有必要上传一个远程 MCP 服务 |
| [OpenAI：Windows 原生沙箱](https://learn.chatgpt.com/docs/windows/windows-sandbox) | Windows 原生 PowerShell 路线可用，WSL 不是必要条件；权限和运行身份需要现场检查 |
| [Python：Windows 下载](https://www.python.org/downloads/windows/) | Python 获取入口，应使用公司允许的受支持版本 |
| [Python：imaplib](https://docs.python.org/3/library/imaplib.html) | SSL 连接、只读选择文件夹、UID 读取和状态处理 |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | 本机 FastMCP/STDIO 服务和客户端测试 |
| [keyring 文档](https://keyring.readthedocs.io/en/latest/) | Windows Credential Locker 后端；本项目只允许指定原生后端，不回退明文 |

原邮件工作知识库的当前 README、IMAP 实现、同步器、SQLite 表结构、知识蒸馏与 MCP 源码也经过只读核对。公开说明只引用模块名称和设计结论，不发布原私有源码、历史数据或个人环境。本项目沿用“只读增量同步 + 本地检索”，并增加显式刷新工具和同步覆盖状态；直连查询仅作为诊断和未读状态读取的补充。

原系统历史 README 的工具数量、处理计数和维护时刻可能早于当前运行状态，不能作为对方部署参数，也不能当作 Windows 版本的性能/效果承诺。

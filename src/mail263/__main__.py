"""Interactive setup and JSON CLI. Never accept a password as an argument."""

import argparse
import getpass
import json
import sys
import warnings

from .config import Config, load_config, save_config, set_credential
from .errors import MailError, error_result, safe_call
from . import reader
from . import sync


def configure() -> dict:
    email = input("完整企业邮箱地址：").strip()
    host = input("IMAP 服务器 [imap.263.net]：").strip() or "imap.263.net"
    port = int(input("TLS 端口 [993]：").strip() or "993")
    save_config(Config(email=email, host=host, port=port))
    return {"ok": True, "next_step": "非秘密配置已保存；接着运行 python -m mail263 set-credential。默认同步 INBOX 和当年邮件，可按需调整。"}


def credential_prompt() -> dict:
    config = load_config()
    if not sys.stdin.isatty():
        raise MailError("CREDENTIAL_INTERACTIVE_REQUIRED", "请本人在交互式 PowerShell 窗口运行 set-credential；不要向 Codex、命令参数或聊天粘贴密码。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("请输入邮箱客户端密码或授权码（输入不显示）：")
    except getpass.GetPassWarning:
        raise MailError("CREDENTIAL_NO_ECHO", "当前终端无法隐藏输入，请本人改用独立 PowerShell 窗口。") from None
    try:
        set_credential(config, password)
    finally:
        password = None
    return {"ok": True, "next_step": "凭据已写入 Windows 凭据管理器；接着运行 python -m mail263 doctor。"}


def mcp_config() -> str:
    # JSON escaping is also valid for the basic TOML strings used here.
    return "[mcp_servers.mail263]\ncommand = " + json.dumps(sys.executable, ensure_ascii=False) + '\nargs = ["-m", "mail263", "server"]\nstartup_timeout_sec = 30\ntool_timeout_sec = 600\n'


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Windows 11 263 企业邮箱只读工具")
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("configure", "set-credential", "doctor", "folders", "server", "mcp-config", "status"):
        commands.add_parser(name)
    local = commands.add_parser("search")
    local.add_argument("--folder", default="")
    local.add_argument("--query", default="")
    local.add_argument("--since", default="")
    local.add_argument("--before", default="")
    local.add_argument("--limit", type=int, default=10)
    local.add_argument("--offset", type=int, default=0)
    refresh = commands.add_parser("refresh")
    refresh.add_argument("--folder", default="")
    refresh.add_argument("--since", default="")
    refresh.add_argument("--max-messages", type=int, default=0)
    search = commands.add_parser("live-search")
    search.add_argument("--folder", default="INBOX")
    search.add_argument("--query", default="")
    search.add_argument("--since", default="")
    search.add_argument("--before", default="")
    search.add_argument("--unread-only", action="store_true")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--max-scan", type=int, default=100)
    search.add_argument("--before-uid", type=int, default=0)
    read = commands.add_parser("read")
    read.add_argument("--folder", required=True)
    read.add_argument("--uid", required=True, type=int)
    read.add_argument("--uidvalidity", required=True)
    read.add_argument("--max-chars", type=int, default=12000)
    download = commands.add_parser("download")
    download.add_argument("--folder", required=True)
    download.add_argument("--uid", required=True, type=int)
    download.add_argument("--uidvalidity", required=True)
    download.add_argument("--attachment-index", required=True, type=int)
    return result


def main() -> int:
    args = vars(parser().parse_args())
    command = args.pop("command")
    if command == "server":
        from .server import run
        try:
            run()
            return 0
        except Exception as exc:
            print(json.dumps(error_result(exc), ensure_ascii=False), file=sys.stderr)
            return 1
    if command == "mcp-config":
        print(mcp_config(), end="")
        return 0
    from .server import status
    actions = {"configure": configure, "set-credential": credential_prompt, "doctor": reader.doctor, "folders": reader.list_folders, "status": status, "search": sync.local_search, "refresh": sync.refresh, "live-search": reader.search, "read": sync.local_message, "download": sync.local_attachment}
    result = safe_call(actions[command], **args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

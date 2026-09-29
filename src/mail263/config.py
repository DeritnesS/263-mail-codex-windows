"""Non-secret configuration and Windows Credential Manager only."""

from dataclasses import dataclass, field
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import platform
import re

from .errors import MailError


def data_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        raise MailError("WINDOWS_REQUIRED", "请在 Windows 11 当前用户账户内运行。")
    path = Path(root)
    if not path.is_absolute():
        raise MailError("CONFIG_PATH", "LOCALAPPDATA 必须是当前 Windows 用户的绝对路径。")
    return path / "Mail263Codex"


@dataclass(frozen=True)
class Config:
    email: str
    host: str = "imap.263.net"
    port: int = 993
    timeout: int = 20
    sync_folders: list[str] = field(default_factory=lambda: ["INBOX"])
    sync_since: str = field(default_factory=lambda: f"{date.today().year}-01-01")
    sync_max_messages: int = 100

    def __post_init__(self):
        if not self.email or "@" not in self.email or any(ord(c) < 32 for c in self.email):
            raise MailError("CONFIG_EMAIL", "重新运行 configure，输入完整企业邮箱地址。")
        if len(self.email) > 254 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", self.host):
            raise MailError("CONFIG_HOST", "服务器只填域名，例如 imap.263.net；不要填网址或端口。")
        if not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise MailError("CONFIG_PORT", "请输入管理员提供的 TLS 端口，通常为 993。")
        if not isinstance(self.timeout, int) or not 1 <= self.timeout <= 30:
            raise MailError("CONFIG_TIMEOUT", "连接超时必须在 1 到 30 秒之间。")
        if not isinstance(self.sync_folders, list) or not self.sync_folders or any(not isinstance(f, str) or not f or any(ord(c) < 32 for c in f) for f in self.sync_folders):
            raise MailError("CONFIG_SYNC_FOLDERS", "sync_folders 应为 mail_list_folders 返回的文件夹名称数组。")
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", self.sync_since):
                raise ValueError("date format")
            date.fromisoformat(self.sync_since)
        except (ValueError, TypeError):
            raise MailError("CONFIG_SYNC_SINCE", "sync_since 必须为有效 YYYY-MM-DD 日期。") from None
        if not isinstance(self.sync_max_messages, int) or not 1 <= self.sync_max_messages <= 500:
            raise MailError("CONFIG_SYNC_LIMIT", "sync_max_messages 必须在 1 到 500 之间。")

    @property
    def credential_service(self) -> str:
        identity = json.dumps([self.host.lower(), self.port, self.email], ensure_ascii=True)
        return "Mail263Codex:" + hashlib.sha256(identity.encode()).hexdigest()


def load_config() -> Config:
    path = data_dir() / "config.json"
    if not path.exists():
        raise MailError("NOT_CONFIGURED", "先在 PowerShell 运行 python -m mail263 configure。")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if set(raw) - {"email", "host", "port", "timeout", "sync_folders", "sync_since", "sync_max_messages"}:
            raise ValueError("unexpected configuration keys")
        return Config(**raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise MailError("CONFIG_INVALID", "配置文件格式不正确，请重新运行 configure。") from None


def save_config(config: Config) -> None:
    folder = data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / "config.json.tmp"
    tmp.write_text(json.dumps(config.__dict__, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(folder / "config.json")


def credential_backend():
    if platform.system() != "Windows":
        raise MailError("WINDOWS_REQUIRED", "凭据功能仅支持 Windows Credential Manager；不提供明文替代。")
    try:
        from keyring.backends.Windows import WinVaultKeyring
        return WinVaultKeyring()
    except Exception:
        raise MailError("CREDENTIAL_BACKEND", "请在当前 Windows 用户下重装项目依赖并检查 Windows 凭据管理器。") from None


def get_credential(config: Config) -> str:
    try:
        password = credential_backend().get_password(config.credential_service, config.email)
    except MailError:
        raise
    except Exception:
        raise MailError("CREDENTIAL_READ", "无法读取 Windows 凭据管理器，请重新运行 set-credential。") from None
    if not password:
        raise MailError("CREDENTIAL_MISSING", "运行 python -m mail263 set-credential；账号或服务器变更后需重新录入。")
    return password


def set_credential(config: Config, password: str) -> None:
    if not password:
        raise MailError("CREDENTIAL_EMPTY", "密码或客户端授权码不能为空。")
    try:
        credential_backend().set_password(config.credential_service, config.email, password)
    except MailError:
        raise
    except Exception:
        raise MailError("CREDENTIAL_WRITE", "无法保存到 Windows 凭据管理器，请检查当前用户权限。") from None

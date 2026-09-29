"""Sanitized errors: never return exception text, credentials or server replies."""

import imaplib
import socket
import ssl


class MailError(Exception):
    def __init__(self, code: str, next_step: str):
        self.code = code
        self.next_step = next_step
        super().__init__(code)


def error_result(exc: Exception) -> dict:
    from .cache import CacheError
    if isinstance(exc, MailError):
        code, step = exc.code, exc.next_step
    elif isinstance(exc, CacheError):
        code, step = exc.code, str(exc)
    elif isinstance(exc, ssl.SSLCertVerificationError):
        code, step = "TLS_CERTIFICATE", "检查电脑日期及邮件服务器地址；不要关闭证书验证。"
    elif isinstance(exc, ssl.SSLError):
        code, step = "TLS", "请管理员确认服务器支持 TLS 993，并检查网络代理。"
    elif isinstance(exc, (socket.timeout, TimeoutError)):
        code, step = "NETWORK_TIMEOUT", "检查网络后重试；缩小查询日期范围。"
    elif isinstance(exc, socket.gaierror):
        code, step = "DNS", "检查服务器地址及电脑网络。"
    elif isinstance(exc, imaplib.IMAP4.error):
        code, step = "IMAP_PROTOCOL", "请管理员确认 IMAP 可用；重新列出文件夹后重试。"
    elif isinstance(exc, OSError):
        code, step = "NETWORK_OR_LOCAL_IO", "检查网络和本地文件权限；不要粘贴密码或完整日志。"
    elif isinstance(exc, (ValueError, TypeError)):
        code, step = "INPUT", "检查参数；日期用 YYYY-MM-DD，UID 和 UIDVALIDITY 用正整数。"
    else:
        code, step = "INTERNAL", "停止本次操作，请 Codex 检查安装和版本；不要输出凭据或邮件原文日志。"
    return {"ok": False, "error": {"code": code, "next_step": step}}


def safe_call(fn, *args, **kwargs) -> dict:
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        return error_result(exc)

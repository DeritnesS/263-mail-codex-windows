"""Each operation opens a fresh TLS connection and never changes a mailbox."""

import base64
from datetime import date, datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
import hashlib
from html.parser import HTMLParser
import imaplib
import io
from pathlib import Path
import re
import ssl
import time
import zipfile

from .config import Config, data_dir, get_credential, load_config
from .errors import MailError

MAX_MESSAGE_BYTES = 25 * 1024 * 1024
MAX_SCAN_BYTES = 50 * 1024 * 1024
MAX_SCAN_SECONDS = 25
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
SAFE_EXTENSIONS = {".pdf", ".xlsx", ".docx", ".pptx", ".csv", ".txt", ".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".bmp", ".webp"}
OFFICE_MARKERS = {".xlsx": "xl/workbook.xml", ".docx": "word/document.xml", ".pptx": "ppt/presentation.xml"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode_folder(value: str) -> str:
    """IMAP modified UTF-7 (RFC 3501), including literal ampersands."""
    if not value or len(value) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise MailError("FOLDER_INPUT", "请使用 mail_list_folders 返回的文件夹名称。")
    chunks, pending = [], []

    def flush():
        if pending:
            encoded = base64.b64encode("".join(pending).encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
            chunks.append("&" + encoded + "-")
            pending.clear()

    for char in value:
        if 32 <= ord(char) <= 126:
            flush()
            chunks.append("&-" if char == "&" else char)
        else:
            pending.append(char)
    flush()
    return "".join(chunks)


def decode_folder(value: str) -> str:
    def decode(match):
        body = match.group(1)
        if not body:
            return "&"
        body = body.replace(",", "/")
        return base64.b64decode(body + "=" * (-len(body) % 4), validate=True).decode("utf-16-be")
    return re.sub(r"&([^-]*)-", decode, value)


def quote_folder(folder: str) -> str:
    return '"' + encode_folder(folder).replace("\\", "\\\\").replace('"', '\\"') + '"'


def unquote(value: bytes) -> str:
    if value.startswith(b'"') and value.endswith(b'"'):
        value = re.sub(rb"\\(.)", rb"\1", value[1:-1])
    return value.decode("ascii")


def parse_folders(rows) -> list[dict]:
    result = []
    for row in rows or []:
        # imaplib represents literal mailbox names as (prefix, literal).
        literal = None
        if isinstance(row, tuple):
            row, literal = row
        if not row:
            continue
        match = re.fullmatch(rb'\((.*?)\)\s+(NIL|"(?:\\.|[^"\\])*")\s+(.+)', row)
        if not match:
            raise MailError("FOLDER_FORMAT", "服务器返回了不支持的文件夹格式，请管理员检查 IMAP。")
        flags = match.group(1).decode("ascii").split()
        name = decode_folder(unquote(literal if literal is not None else match.group(3)))
        delimiter = None if match.group(2) == b"NIL" else unquote(match.group(2))
        result.append({"name": name, "delimiter": delimiter, "selectable": "\\noselect" not in [f.lower() for f in flags], "flags": flags})
    return result


def checked(typ, data):
    if typ != "OK":
        raise MailError("IMAP_REJECTED", "服务器未完成只读操作，请检查文件夹名称或缩小查询范围。")
    return data


class Session:
    def __init__(self, config: Config | None = None):
        self.config = config or load_config()
        self.client = None

    def __enter__(self):
        password = get_credential(self.config)
        self.client = imaplib.IMAP4_SSL(self.config.host, self.config.port, ssl_context=ssl.create_default_context(), timeout=self.config.timeout)
        try:
            checked(*self.client.login(self.config.email, password))
        except imaplib.IMAP4.error:
            self.__exit__(None, None, None)
            raise MailError("AUTHENTICATION", "请确认完整邮箱地址、客户端密码或授权码；让管理员确认已启用 IMAP。") from None
        except Exception:
            self.__exit__(None, None, None)
            raise
        finally:
            password = None
        return self

    def __exit__(self, *args):
        if self.client is not None:
            try:
                self.client.logout()  # Never CLOSE: that command can expunge deleted mail.
            except Exception:
                pass

    def select(self, folder: str, expected_uidvalidity: str | int | None = None) -> str:
        checked(*self.client.select(quote_folder(folder), readonly=True))  # EXAMINE
        _, data = self.client.response("UIDVALIDITY")
        if not data or not data[0] or not re.fullmatch(rb"[1-9][0-9]*", data[0]):
            raise MailError("UIDVALIDITY_MISSING", "服务器没有提供 UIDVALIDITY，不能安全定位邮件。")
        current = data[0].decode("ascii")
        if expected_uidvalidity is not None and current != str(expected_uidvalidity):
            raise MailError("UIDVALIDITY_CHANGED", "文件夹标识已变化；请重新搜索，再使用新的 UID 与 UIDVALIDITY。")
        return current

    def size(self, uid: int) -> int:
        rows = checked(*self.client.uid("FETCH", str(uid), "(RFC822.SIZE)"))
        for row in rows or []:
            if isinstance(row, bytes):
                match = re.search(rb"RFC822.SIZE\s+(\d+)", row)
                if match:
                    return int(match.group(1))
        raise MailError("MESSAGE_GONE", "邮件可能已移动或删除，请重新搜索。")

    def search_uids(self, criteria: list[str] | None = None) -> list[int]:
        rows = checked(*self.client.uid("SEARCH", None, *(criteria or ["ALL"])))
        identifiers = {int(n) for row in rows or [] if row for n in row.split()}
        if any(not 1 <= uid <= 4294967295 for uid in identifiers):
            raise MailError("UID_SERVER", "服务器返回了无效 UID，请管理员检查 IMAP。")
        return sorted(identifiers)

    def internaldate(self, uid: int) -> str:
        rows = checked(*self.client.uid("FETCH", str(uid), "(INTERNALDATE)"))
        for row in rows or []:
            if isinstance(row, bytes):
                match = re.search(rb'INTERNALDATE\s+"([^"\r\n]+)"', row)
                if match:
                    return parsedate_to_datetime(match.group(1).decode("ascii").replace("-", " ", 2)).isoformat()
        raise MailError("INTERNALDATE_MISSING", "服务器未返回收件日期，请重新刷新此邮件。")

    def fetch(self, uid: int, size: int | None = None) -> bytes:
        size = self.size(uid) if size is None else size
        if size > MAX_MESSAGE_BYTES:
            raise MailError("MESSAGE_TOO_LARGE", "邮件超过 25 MiB；请在企业邮箱网页中手动查看。")
        rows = checked(*self.client.uid("FETCH", str(uid), "(BODY.PEEK[])"))
        for row in rows or []:
            if isinstance(row, tuple) and isinstance(row[1], bytes):
                raw = row[1]
                if len(raw) > MAX_MESSAGE_BYTES:
                    raise MailError("MESSAGE_TOO_LARGE", "实际邮件超过 25 MiB；请在企业邮箱网页中手动查看。")
                return raw
        raise MailError("MESSAGE_GONE", "邮件可能已移动或删除，请重新搜索。")


class TextHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        elif not self.hidden and tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1
        elif not self.hidden and tag in ("p", "div", "li", "tr"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def part_bytes(part) -> bytes:
    payload = part.get_payload(decode=True)
    if payload is not None:
        return payload
    if part.get_content_type() == "message/rfc822":
        return b"\n".join(p.as_bytes(policy=policy.default) for p in part.get_payload())
    return b""


def decode_text(part) -> str:
    content = part_bytes(part)
    charset = part.get_content_charset() or "utf-8"
    try:
        return content.decode(charset, errors="replace")
    except LookupError:
        return content.decode("utf-8", errors="replace")


def parse_message(raw: bytes) -> tuple[dict, list[bytes]]:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    plain, html, attachments, payloads = [], [], [], []

    def visit(part):
        filename = part.get_filename()
        if filename or part.get_content_disposition() == "attachment" or part.get_content_type() == "message/rfc822":
            payload = part_bytes(part)
            attachments.append({"index": len(attachments), "filename": str(filename or "attachment")[:512], "content_type": part.get_content_type(), "size_bytes": len(payload)})
            payloads.append(payload)
        elif part.is_multipart():
            for child in part.iter_parts():
                visit(child)
        elif part.get_content_type() == "text/plain":
            plain.append(decode_text(part))
        elif part.get_content_type() == "text/html":
            parser = TextHTML()
            parser.feed(decode_text(part))
            html.append("".join(parser.parts))

    visit(message)
    body = "\n".join(plain if plain else html).strip()
    result = {key: str(message.get(header, ""))[:8192] for key, header in (("subject", "Subject"), ("from", "From"), ("to", "To"), ("cc", "Cc"), ("date_header", "Date"), ("message_id", "Message-ID"))}
    result.update(body=body, body_format="plain" if plain else "html_to_text", attachments=attachments, message_sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))
    return result, payloads


def validate_id(uid: int, uidvalidity: str | int):
    if not isinstance(uid, int) or isinstance(uid, bool) or not 1 <= uid <= 4294967295:
        raise MailError("UID_INPUT", "请使用搜索结果中的正整数 UID。")
    if not re.fullmatch(r"[1-9][0-9]{0,9}", str(uidvalidity)) or int(uidvalidity) > 4294967295:
        raise MailError("UIDVALIDITY_INPUT", "请使用原搜索结果中的 UIDVALIDITY。")


def imap_date(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("date format")
    parsed = date.fromisoformat(value)
    return f"{parsed.day:02d}-{MONTHS[parsed.month - 1]}-{parsed.year:04d}"


def list_folders() -> dict:
    query_time = now()
    with Session() as session:
        folders = parse_folders(checked(*session.client.list()))
    return {"ok": True, "query_time": query_time, "folders": folders}


def doctor() -> dict:
    result = list_folders()
    return {"ok": True, "query_time": result["query_time"], "tls_login_list": "passed", "folder_count": len(result["folders"]), "read_only": True, "next_step": "连接验证成功。可列出文件夹并执行小范围搜索。"}


def search(folder: str = "INBOX", query: str = "", since: str = "", before: str = "", unread_only: bool = False, limit: int = 10, max_scan: int = 100, before_uid: int = 0) -> dict:
    if not 1 <= limit <= 50 or not 1 <= max_scan <= 500 or not 0 <= before_uid <= 4294967296:
        raise MailError("SEARCH_INPUT", "limit 范围 1–50，max_scan 范围 1–500；before_uid 使用上次返回值。")
    if len(query) > 1000:
        raise MailError("SEARCH_INPUT", "请缩短关键词到 1000 字符以内。")
    criteria = ["ALL"]
    if since:
        criteria += ["SINCE", imap_date(since)]
    if before:
        criteria += ["BEFORE", imap_date(before)]
    if since and before and since >= before:
        raise MailError("SEARCH_DATES", "since 必须早于 before；before 当天不包含在查询中。")
    if unread_only:
        criteria += ["UNSEEN"]
    if before_uid > 1:
        criteria += ["UID", f"1:{before_uid - 1}"]
    query_time = now()
    matched, skipped = [], []
    scanned, scanned_bytes, attempted = 0, 0, 0
    stop_reason, last_consumed = "exhausted", None
    with Session() as session:
        uidvalidity = session.select(folder)
        started = time.monotonic()
        rows = checked(*session.client.uid("SEARCH", None, *criteria)) if before_uid != 1 else [b""]
        identifiers = sorted({int(n) for row in rows or [] if row for n in row.split()}, reverse=True)
        # Client filtering also guards a server returning UIDs outside its requested range.
        if before_uid:
            identifiers = [uid for uid in identifiers if uid < before_uid]
        candidates = identifiers[:max_scan]
        for uid in candidates:
            if len(matched) >= limit:
                stop_reason = "result_limit"
                break
            if time.monotonic() - started >= MAX_SCAN_SECONDS:
                stop_reason = "time_budget"
                break
            size = session.size(uid)
            if size > MAX_MESSAGE_BYTES:
                attempted += 1
                last_consumed = uid
                skipped.append({"uid": uid, "reason": "message_over_25_mib", "size_bytes": size})
                continue
            if scanned_bytes + size > MAX_SCAN_BYTES:
                stop_reason = "byte_budget"
                break
            if time.monotonic() - started >= MAX_SCAN_SECONDS:
                stop_reason = "time_budget"
                break
            raw = session.fetch(uid, size)
            attempted += 1
            last_consumed = uid
            scanned += 1
            scanned_bytes += len(raw)
            parsed, _ = parse_message(raw)
            haystack = "\n".join(parsed[key] for key in ("subject", "from", "to", "cc", "body"))
            if not query or query.casefold() in haystack.casefold():
                parsed["body_preview"] = parsed.pop("body")[:600]
                parsed.update(uid=uid, uidvalidity=uidvalidity, folder=folder)
                matched.append(parsed)
        remaining = len(identifiers) > attempted
        if remaining and stop_reason == "exhausted":
            stop_reason = "scan_limit"
        next_before = (last_consumed if last_consumed is not None else identifiers[0] + 1) if remaining else None
    return {
        "ok": True, "query_time": query_time, "folder": folder, "uidvalidity": uidvalidity,
        "scope": {"query": query, "since_inclusive": since or None, "before_exclusive": before or None, "date_basis": "IMAP INTERNALDATE calendar date (server delivery date), not Date header", "unread_only": unread_only, "before_uid_exclusive": before_uid or None, "query_fields": ["subject", "from", "to", "cc", "body"], "attachment_contents_searched": False, "order": "UID descending"},
        "server_candidate_count": len(identifiers), "attempted_count": attempted, "scanned_count": scanned,
        "matched_count": len(matched), "scanned_bytes": scanned_bytes, "skipped": skipped,
        "truncated": remaining, "next_before_uid": next_before, "stop_reason": stop_reason,
        "budget": {"max_scan": max_scan, "max_message_bytes": MAX_MESSAGE_BYTES, "max_scan_bytes": MAX_SCAN_BYTES, "scan_seconds": MAX_SCAN_SECONDS, "connection_timeout_seconds": session.config.timeout, "time_note": "Budget checked between messages; an in-flight network operation can add up to the connection timeout."},
        "coverage_note": "结果仅代表本次文件夹、筛选条件及成功扫描的正文范围；零命中不证明邮件不存在。", "messages": matched,
    }


def get_message(folder: str, uid: int, uidvalidity: str, max_chars: int = 12000) -> dict:
    validate_id(uid, uidvalidity)
    if not 1 <= max_chars <= 100000:
        raise MailError("BODY_LIMIT", "max_chars 范围为 1–100000。")
    query_time = now()
    with Session() as session:
        session.select(folder, uidvalidity)
        raw = session.fetch(uid)
    parsed, _ = parse_message(raw)
    full_length = len(parsed["body"])
    parsed["body"] = parsed["body"][:max_chars]
    parsed.update(body_chars=full_length, body_truncated=full_length > max_chars)
    return {"ok": True, "query_time": query_time, "folder": folder, "uid": uid, "uidvalidity": str(uidvalidity), "content_is_untrusted": True, **parsed}


def safe_extension(filename: str, payload: bytes) -> str:
    extension = Path(filename.replace("\\", "/")).suffix.lower()
    if extension not in SAFE_EXTENSIONS or payload.startswith(b"MZ"):
        raise MailError("ATTACHMENT_TYPE_BLOCKED", "只允许 PDF、现代 Office 文档、文本及常用图片；压缩包、程序和脚本请在企业邮箱中自行处理。")
    if payload.startswith(b"PK") or extension in OFFICE_MARKERS:
        if extension not in OFFICE_MARKERS:
            raise MailError("ATTACHMENT_ARCHIVE_BLOCKED", "该附件是压缩包，工具不会下载或解压。")
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                names = set(archive.namelist())
                if "[Content_Types].xml" not in names or OFFICE_MARKERS[extension] not in names or any(name.lower().endswith("vbaproject.bin") for name in names):
                    raise ValueError("unsupported office package")
        except (ValueError, zipfile.BadZipFile):
            raise MailError("ATTACHMENT_OFFICE_INVALID", "附件不是可识别的无宏 Office 文档；请在企业邮箱中手动确认。") from None
    return extension


def download_attachment(folder: str, uid: int, uidvalidity: str, attachment_index: int) -> dict:
    validate_id(uid, uidvalidity)
    with Session() as session:
        session.select(folder, uidvalidity)
        raw = session.fetch(uid)
    return save_attachment(raw, folder, uid, uidvalidity, attachment_index)


def save_attachment(raw: bytes, folder: str, uid: int, uidvalidity: str, attachment_index: int) -> dict:
    """Extract original attachment bytes from an already fetched MIME message; never connect."""
    validate_id(uid, uidvalidity)
    if not isinstance(attachment_index, int) or attachment_index < 0:
        raise MailError("ATTACHMENT_INDEX", "使用读取结果中从 0 开始的附件 index。")
    query_time = now()
    parsed, payloads = parse_message(raw)
    if attachment_index >= len(payloads):
        raise MailError("ATTACHMENT_INDEX", "附件编号不存在，请重新读取邮件。")
    metadata, payload = parsed["attachments"][attachment_index], payloads[attachment_index]
    extension = safe_extension(metadata["filename"], payload)
    digest = hashlib.sha256(payload).hexdigest()
    root = data_dir().resolve()
    destination = root / "downloads"
    if destination.is_symlink():
        raise MailError("DOWNLOAD_PATH", "downloads 不得是符号链接，请 Codex 检查本地目录。")
    destination.mkdir(parents=True, exist_ok=True)
    if destination.resolve().parent != root:
        raise MailError("DOWNLOAD_PATH", "附件路径超出允许目录。")
    path = destination / (digest + extension)
    if path.is_symlink():
        raise MailError("DOWNLOAD_PATH", "目标文件不得是符号链接。")
    try:
        with path.open("xb") as handle:
            handle.write(payload)
    except FileExistsError:
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise MailError("DOWNLOAD_CONFLICT", "同名文件内容不一致，请 Codex 检查下载目录。") from None
    return {"ok": True, "query_time": query_time, "folder": folder, "uid": uid, "uidvalidity": str(uidvalidity), "attachment_index": attachment_index, "original_filename": metadata["filename"], "path": str(path), "size_bytes": len(payload), "attachment_sha256": digest, "message_sha256": parsed["message_sha256"], "executed": False, "content_is_untrusted": True}

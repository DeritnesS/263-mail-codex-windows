import asyncio
from email.message import EmailMessage
from email import policy
import hashlib
import imaplib
import json
import os
from pathlib import Path
import ssl
import tempfile
import tomllib
import unittest
from unittest.mock import patch, Mock

from mail263 import reader
from mail263.config import Config, load_config, save_config, get_credential
from mail263.errors import MailError, error_result
from mail263.__main__ import mcp_config, credential_prompt


def mail(subject="测试发票", text="请核对九月发票。", attachment=None, filename="invoice.pdf"):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "sender@example.invalid"
    msg["To"] = "receiver@example.invalid"
    msg["Date"] = "Tue, 29 Sep 2026 09:00:00 +0800"
    msg["Message-ID"] = "<synthetic-test@example.invalid>"
    msg.set_content(text)
    msg.add_alternative("<p>此处为HTML副本</p><script>never execute</script>", subtype="html")
    if attachment is not None:
        msg.add_attachment(attachment, maintype="application", subtype="pdf", filename=filename)
    return msg.as_bytes(policy=policy.SMTP)


class FakeIMAP:
    def __init__(self, messages=None, uidvalidity=b"123"):
        self.messages = messages if messages is not None else {10: mail()}
        self.uidvalidity = uidvalidity
        self.calls = []
        self.sizes = {}

    def login(self, account, password):
        self.calls.append(("LOGIN",))
        return "OK", [b"logged in"]

    def logout(self):
        self.calls.append(("LOGOUT",))
        return "BYE", [b"bye"]

    def list(self):
        self.calls.append(("LIST",))
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', ('(\\HasNoChildren) "/" "' + reader.encode_folder("已发送") + '"').encode()]

    def select(self, folder, readonly=False):
        self.calls.append(("EXAMINE" if readonly else "SELECT", folder, readonly))
        return "OK", [str(len(self.messages)).encode()]

    def response(self, name):
        self.calls.append(("RESPONSE", name))
        return name, [self.uidvalidity]

    def uid(self, command, *args):
        self.calls.append(("UID", command, *args))
        if command == "SEARCH":
            return "OK", [b" ".join(str(uid).encode() for uid in sorted(self.messages))]
        if command == "FETCH":
            uid, request = int(args[0]), args[1]
            if uid not in self.messages:
                return "OK", [None]
            if request == "(RFC822.SIZE)":
                return "OK", [f"1 (UID {uid} RFC822.SIZE {self.sizes.get(uid, len(self.messages[uid]))})".encode()]
            if request == "(INTERNALDATE)":
                return "OK", [f'1 (UID {uid} INTERNALDATE "29-Sep-2026 09:00:00 +0800")'.encode()]
            if request == "(BODY.PEEK[])":
                return "OK", [(b"1 (BODY[] {123}", self.messages[uid]), b")"]
        raise AssertionError(f"Unexpected protocol command: {command}")


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        save_config(Config("account@example.invalid"))
        self.fake = FakeIMAP()
        self.factory = patch.object(reader.imaplib, "IMAP4_SSL", return_value=self.fake)
        self.connect = self.factory.start()
        self.addCleanup(self.factory.stop)
        self.credential = patch.object(reader, "get_credential", return_value="synthetic-password")
        self.credential.start()
        self.addCleanup(self.credential.stop)

    def test_config_persistence_and_host_bound_credentials(self):
        config = load_config()
        self.assertEqual(config.sync_folders, ["INBOX"])
        self.assertNotEqual(config.credential_service, Config(config.email, host="mail.example.invalid").credential_service)
        content = (Path(self.tmp.name) / "Mail263Codex/config.json").read_text()
        self.assertNotIn("password", content)
        self.assertNotIn("synthetic-password", content)

    def test_plaintext_backend_and_noninteractive_secret_input_refused(self):
        with patch("mail263.config.platform.system", return_value="Darwin"):
            with self.assertRaises(MailError) as caught:
                get_credential(load_config())
            self.assertEqual(caught.exception.code, "WINDOWS_REQUIRED")
        with patch("sys.stdin.isatty", return_value=False), patch("getpass.getpass") as prompt:
            with self.assertRaises(MailError):
                credential_prompt()
            prompt.assert_not_called()

    def test_tls_and_readonly_protocol_and_logout(self):
        result = reader.get_message("INBOX", 10, "123")
        self.assertTrue(result["ok"])
        self.assertEqual(result["message_sha256"], hashlib.sha256(self.fake.messages[10]).hexdigest())
        ctx = self.connect.call_args.kwargs["ssl_context"]
        self.assertTrue(ctx.check_hostname)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertLessEqual(self.connect.call_args.kwargs["timeout"], 30)
        self.assertIn(("EXAMINE", '"INBOX"', True), self.fake.calls)
        self.assertIn(("UID", "FETCH", "10", "(BODY.PEEK[])"), self.fake.calls)
        allowed = {"LOGIN", "LOGOUT", "RESPONSE", "EXAMINE", "UID", "LIST"}
        self.assertTrue(all(call[0] in allowed for call in self.fake.calls))
        self.assertEqual(self.fake.calls[-1], ("LOGOUT",))

    def test_stdlib_select_readonly_emits_examine(self):
        client = object.__new__(imaplib.IMAP4)
        client._simple_command = Mock(return_value=("OK", [b"done"]))
        client.untagged_responses = {"EXISTS": [b"1"]}
        client.state = "AUTH"
        client._encoding = "ascii"
        client.select('"INBOX"', readonly=True)
        client._simple_command.assert_called_once()
        command, mailbox = client._simple_command.call_args.args
        self.assertEqual(command, "EXAMINE")
        # Newer imaplib versions encode/astring-quote before this boundary.
        if isinstance(mailbox, str):
            mailbox = mailbox.encode("ascii")
        self.assertEqual(mailbox, b'"INBOX"')

    def test_uidvalidity_mismatch_never_fetches(self):
        with self.assertRaises(MailError) as caught:
            reader.get_message("INBOX", 10, "999")
        self.assertEqual(caught.exception.code, "UIDVALIDITY_CHANGED")
        self.assertFalse(any(c[:2] == ("UID", "FETCH") for c in self.fake.calls))
        self.assertEqual(self.fake.calls[-1], ("LOGOUT",))

    def test_missing_uidvalidity_refused(self):
        self.fake.uidvalidity = None
        with self.assertRaises(MailError) as caught:
            reader.get_message("INBOX", 10, "123")
        self.assertEqual(caught.exception.code, "UIDVALIDITY_MISSING")

    def test_modified_utf7_quote_and_list(self):
        folder = '已发送 & 财务/"Q"\\'
        self.assertEqual(reader.decode_folder(reader.encode_folder(folder)), folder)
        quoted = reader.quote_folder(folder)
        self.assertIn('\\"Q\\"', quoted)
        self.assertTrue(quoted.endswith('\\\\"'))
        rows = [b'(\\NoSelect) "/" "INBOX"', (b'(\\HasNoChildren) "/" {8}', reader.encode_folder("已发送").encode())]
        self.assertEqual(reader.parse_folders(rows)[1]["name"], "已发送")
        self.assertFalse(reader.parse_folders(rows)[0]["selectable"])
        with self.assertRaises(MailError):
            reader.quote_folder("INBOX\r\nSTORE")

    def test_mime_prefers_plain_and_decodes_chinese(self):
        parsed, payloads = reader.parse_message(mail(attachment=b"%PDF-test"))
        self.assertEqual(parsed["subject"], "测试发票")
        self.assertIn("九月发票", parsed["body"])
        self.assertNotIn("HTML", parsed["body"])
        self.assertEqual(parsed["attachments"][0]["index"], 0)
        self.assertEqual(payloads, [b"%PDF-test"])
        html = b"Content-Type: text/html; charset=utf-8\r\n\r\n<p>Hello &amp; hi</p><script>steal()</script>"
        parsed, _ = reader.parse_message(html)
        self.assertIn("Hello & hi", parsed["body"])
        self.assertNotIn("steal", parsed["body"])

    def test_chinese_search_and_server_date_filters(self):
        result = reader.search(folder="已发送", query="发票", since="2026-09-01", before="2026-10-01", unread_only=True)
        self.assertEqual(result["matched_count"], 1)
        search_call = next(c for c in self.fake.calls if c[:2] == ("UID", "SEARCH"))
        self.assertIn("01-Sep-2026", search_call)
        self.assertIn("01-Oct-2026", search_call)
        self.assertIn("UNSEEN", search_call)
        self.assertIn("INTERNALDATE", result["scope"]["date_basis"])
        self.assertFalse(result["scope"]["attachment_contents_searched"])

    def test_pagination_limit_and_casefold(self):
        self.fake.messages = {uid: mail(text="INVOICE") for uid in (10, 20, 30)}
        first = reader.search(query="invoice", limit=1)
        self.assertEqual(first["messages"][0]["uid"], 30)
        self.assertEqual(first["next_before_uid"], 30)
        second = reader.search(query="invoice", before_uid=30)
        self.assertEqual([m["uid"] for m in second["messages"]], [20, 10])
        self.assertFalse(second["truncated"])
        self.assertIsNone(second["next_before_uid"])
        self.assertTrue(any("1:29" in call for call in self.fake.calls))

    def test_scan_limit_zero_match_does_not_imply_absence(self):
        self.fake.messages = {uid: mail() for uid in (10, 20, 30)}
        result = reader.search(query="does not exist", max_scan=2)
        self.assertEqual(result["server_candidate_count"], 3)
        self.assertEqual(result["scanned_count"], 2)
        self.assertEqual(result["matched_count"], 0)
        self.assertEqual(result["next_before_uid"], 20)
        self.assertTrue(result["truncated"])
        self.assertIn("不证明", result["coverage_note"])

    def test_oversized_message_not_fetched_and_reported(self):
        self.fake.sizes[10] = reader.MAX_MESSAGE_BYTES + 1
        result = reader.search()
        self.assertEqual(result["skipped"][0]["uid"], 10)
        self.assertEqual(result["scanned_count"], 0)
        self.assertFalse(any("(BODY.PEEK[])" in c for c in self.fake.calls))
        with self.assertRaises(MailError) as caught:
            reader.get_message("INBOX", 10, "123")
        self.assertEqual(caught.exception.code, "MESSAGE_TOO_LARGE")

    def test_scan_byte_budget_keeps_unconsumed_uid_pageable(self):
        self.fake.messages = {uid: mail() for uid in (10, 20)}
        with patch.object(reader, "MAX_SCAN_BYTES", 1):
            result = reader.search()
        self.assertEqual(result["next_before_uid"], 21)
        self.assertEqual(result["stop_reason"], "byte_budget")
        self.assertEqual(result["scanned_count"], 0)

    def test_body_truncation_explicit(self):
        result = reader.get_message("INBOX", 10, "123", max_chars=2)
        self.assertEqual(len(result["body"]), 2)
        self.assertTrue(result["body_truncated"])

    def test_safe_attachment_path_hash_and_no_execution(self):
        payload = b"%PDF-1.4\nsynthetic data"
        self.fake.messages[10] = mail(attachment=payload, filename="../../secret.pdf")
        result = reader.download_attachment("INBOX", 10, "123", 0)
        path = Path(result["path"])
        self.assertEqual(path.parent, (Path(self.tmp.name) / "Mail263Codex/downloads").resolve())
        self.assertEqual(path.name, hashlib.sha256(payload).hexdigest() + ".pdf")
        self.assertEqual(path.read_bytes(), payload)
        self.assertFalse(result["executed"])
        self.assertEqual(reader.download_attachment("INBOX", 10, "123", 0)["path"], str(path))

    def test_dangerous_attachment_types_refused(self):
        for name, data in (("evil.exe", b"MZabc"), ("fake.pdf", b"MZabc"), ("x.zip", b"PKabc"), ("fake.pdf", b"PKabc"), ("run.ps1", b"echo test"), ("fake.xlsx", b"not office")):
            with self.subTest(name=name, data=data), self.assertRaises(MailError):
                reader.safe_extension(name, data)

    def test_download_directory_symlink_refused(self):
        self.fake.messages[10] = mail(attachment=b"%PDF-test")
        try:
            (Path(self.tmp.name) / "Mail263Codex/downloads").symlink_to(self.tmp.name, target_is_directory=True)
        except OSError:
            self.skipTest("Current Windows user is not allowed to create symlinks")
        with self.assertRaises(MailError) as caught:
            reader.download_attachment("INBOX", 10, "123", 0)
        self.assertEqual(caught.exception.code, "DOWNLOAD_PATH")

    def test_live_uid_and_internaldate_api(self):
        with reader.Session() as session:
            self.assertEqual(session.select("INBOX"), "123")
            self.assertEqual(session.search_uids(), [10])
            self.assertEqual(session.internaldate(10), "2026-09-29T09:00:00+08:00")

    def test_errors_do_not_expose_server_reply(self):
        result = error_result(imaplib.IMAP4.error("PRIVATE_ACCOUNT_PASSWORD_BODY"))
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.fake.login = Mock(side_effect=imaplib.IMAP4.error("PRIVATE_LOGIN"))
        with self.assertRaises(MailError) as caught:
            reader.doctor()
        self.assertEqual(caught.exception.code, "AUTHENTICATION")
        self.assertEqual(self.fake.calls[-1], ("LOGOUT",))

    def test_doctor_really_connects_status_does_not(self):
        from mail263.server import status
        self.assertFalse(status()["online_checked"])
        self.connect.assert_not_called()
        self.assertEqual(reader.doctor()["tls_login_list"], "passed")
        self.connect.assert_called_once()
        self.assertNotIn("account", json.dumps(reader.doctor()))

    def test_toml_uses_current_python_and_mcp_schema(self):
        import sys
        from mail263.server import make_server
        config = tomllib.loads(mcp_config())
        self.assertEqual(config["mcp_servers"]["mail263"]["command"], sys.executable)
        tools = asyncio.run(make_server().list_tools())
        by_name = {tool.name: tool for tool in tools}
        self.assertEqual(set(by_name), {"system_status", "mail_list_folders", "mail_search", "mail_get_message", "mail_download_attachment", "mail_refresh", "mail_live_search"})
        self.assertIn("uidvalidity", by_name["mail_get_message"].inputSchema["required"])

    def test_real_mcp_stdio_startup_and_local_status(self):
        import sys
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def smoke():
            params = StdioServerParameters(command=sys.executable, args=["-m", "mail263", "server"], env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"), "LOCALAPPDATA": self.tmp.name})
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    tools = await client.list_tools()
                    self.assertEqual(len(tools.tools), 7)
                    result = await client.call_tool("system_status", {})
                    self.assertFalse(result.isError)
                    payload = result.structuredContent or json.loads(result.content[0].text)
                    self.assertFalse(payload["online_checked"])

        asyncio.run(smoke())

    def test_cli_json_is_utf8_even_with_legacy_redirected_encoding(self):
        import os
        import subprocess
        import sys
        env = dict(os.environ, PYTHONIOENCODING="ascii", LOCALAPPDATA=self.tmp.name,
                   PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        result = subprocess.run([sys.executable, "-m", "mail263", "status"],
                                env=env, capture_output=True, check=True)
        self.assertTrue(json.loads(result.stdout.decode("utf-8"))["ok"])


if __name__ == "__main__":
    unittest.main()

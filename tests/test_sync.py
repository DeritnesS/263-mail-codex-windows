"""Synthetic integration tests: local caching, bounded sync, and native WinVault.

No real mailbox or credentials are used. The one Windows-only test creates and
removes a UUID-named synthetic entry in the current user's credential vault.
"""
import asyncio
from email import policy
from email.message import EmailMessage
import hashlib
import json
import os
from pathlib import Path
import platform
import tempfile
import unittest
from unittest.mock import patch
import uuid

from mail263 import reader, sync
from mail263.cache import CacheError
from mail263.config import Config, credential_backend, save_config
from mail263.server import make_server


def message(uid, text="九月发票需要对账", attachment=False):
    msg = EmailMessage()
    msg["Subject"] = "测试对账 " + str(uid)
    msg["From"] = "sender@example.invalid"
    msg["To"] = "receiver@example.invalid"
    # Deliberately unrelated to INTERNALDATE: filtering must use server receipt.
    msg["Date"] = "Tue, 01 Jan 2019 09:00:00 +0800"
    msg["Message-ID"] = f"<synthetic-{uid}@example.invalid>"
    msg.set_content(text)
    if attachment:
        msg.add_attachment(b"%PDF-1.4\nsynthetic-test", maintype="application", subtype="pdf", filename="虚构发票.pdf")
    return msg.as_bytes(policy=policy.SMTP)


class FakeSession:
    def __init__(self):
        self.messages = {uid: message(uid, attachment=True) for uid in (1, 2, 3)}
        self.validity = "123"
        self.dates = {}
        self.calls = []
        self.fail_search = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.calls.append(("exit",))

    def select(self, folder):
        self.calls.append(("select", folder))
        return self.validity

    def search_uids(self, criteria):
        self.calls.append(("search", tuple(criteria)))
        if self.fail_search:
            raise TimeoutError("synthetic-private-error-do-not-echo")
        return sorted(self.messages)

    def size(self, uid):
        self.calls.append(("size", uid))
        return len(self.messages[uid])

    def internaldate(self, uid):
        self.calls.append(("internaldate", uid))
        return self.dates.get(uid, "2026-09-01T10:00:00+08:00")

    def fetch(self, uid, size=None):
        self.calls.append(("fetch", uid))
        return self.messages[uid]


class SyncIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        environment = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        environment.start()
        self.addCleanup(environment.stop)
        save_config(Config("synthetic@example.invalid", sync_since="2026-01-01", sync_max_messages=2))
        self.session = FakeSession()
        session_patch = patch.object(sync, "Session", return_value=self.session)
        self.session_factory = session_patch.start()
        self.addCleanup(session_patch.stop)

    def test_bounded_partial_resume_then_known_uids_never_fetch(self):
        first = sync.refresh()
        self.assertFalse(first["ok"])
        self.assertEqual(first["folders"][0]["status"], "partial")
        self.assertEqual(first["folders"][0]["downloaded"], 2)
        self.assertEqual(first["folders"][0]["pending"], 1)
        self.assertIsNone(first["freshness"]["folders"][0]["last_success_at"])
        self.assertEqual([c[1] for c in self.session.calls if c[0] == "fetch"], [3, 2])
        self.assertEqual(sync.local_search("发票")["total"], 2)
        self.session.calls.clear()
        second = sync.refresh()
        self.assertTrue(second["ok"])
        self.assertEqual(second["folders"][0]["downloaded"], 1)
        self.assertEqual([c[1] for c in self.session.calls if c[0] == "fetch"], [1])
        self.assertIsNotNone(second["freshness"]["folders"][0]["last_success_at"])
        self.session.calls.clear()
        third = sync.refresh()
        self.assertTrue(third["ok"])
        self.assertEqual(third["folders"][0]["downloaded"], 0)
        self.assertEqual(third["folders"][0]["transferred_bytes"], 0)
        self.assertFalse(any(c[0] in {"size", "internaldate", "fetch"} for c in self.session.calls))
        self.assertTrue(any(c[0] == "search" for c in self.session.calls))

    def test_network_failure_preserves_success_scope_and_local_queries(self):
        baseline = sync.refresh(max_messages=10)["freshness"]["folders"][0]
        self.session_factory.side_effect = TimeoutError("do-not-echo-this")
        failed = sync.refresh(since="2024-01-01")
        self.assertFalse(failed["ok"])
        self.assertTrue(failed["local_query_available"])
        state = failed["freshness"]["folders"][0]
        self.assertEqual(state["last_success_at"], baseline["last_success_at"])
        self.assertEqual(state["coverage_since"], "2026-01-01")
        self.assertEqual(state["attempted_scope_since"], "2024-01-01")
        self.assertEqual(state["status"], "failed")
        self.assertEqual(sync.local_search("九月发票")["total"], 3)
        self.assertEqual(sync.local_message("INBOX", 1, "123")["subject"], "测试对账 1")
        self.assertNotIn("do-not-echo-this", str(failed))

    def test_first_connection_failure_records_attempt_without_generation(self):
        self.session_factory.side_effect = TimeoutError("synthetic")
        failed = sync.refresh()
        state = failed["freshness"]["folders"][0]
        self.assertEqual(state["status"], "failed")
        self.assertIsNone(state["uidvalidity"])
        self.assertIsNone(state["last_success_at"])
        self.assertIsNotNone(state["last_attempt_at"])
        self.assertEqual(sync.local_search()["total"], 0)

    def test_new_generation_search_failure_hides_old_mail_and_preserves_object(self):
        sync.refresh(max_messages=10)
        raw = self.session.messages[1]
        obj = sync.cache_for().objects / (hashlib.sha256(raw).hexdigest() + ".eml")
        self.session.validity = "456"
        self.session.fail_search = True
        failed = sync.refresh()
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["freshness"]["folders"][0]["uidvalidity"], "456")
        self.assertIsNone(failed["freshness"]["folders"][0]["last_success_at"])
        self.assertEqual(sync.local_search()["total"], 0)
        with self.assertRaises(CacheError) as caught:
            sync.local_message("INBOX", 1, "123")
        self.assertEqual(caught.exception.code, "UIDVALIDITY_CHANGED")
        self.assertEqual(obj.read_bytes(), raw)

    def test_internaldate_midnight_filter_ignores_utc_day_and_header(self):
        self.session.messages = {uid: message(uid) for uid in (1, 2, 3)}
        self.session.dates = {1: "2026-09-01T00:05:00+08:00", 2: "2026-08-31T23:55:00-08:00", 3: "2026-09-02T00:01:00+08:00"}
        sync.refresh(since="2026-01-01", max_messages=10)
        result = sync.local_search(since="2026-09-01", before="2026-09-02")
        self.assertEqual([r["uid"] for r in result["results"]], [1])
        self.assertEqual(result["query_scope"]["date_basis"], "IMAP_INTERNALDATE_calendar_day")

    def test_corrupt_raw_fails_read_then_refresh_repairs_only_that_uid(self):
        sync.refresh(max_messages=10)
        raw = self.session.messages[2]
        obj = sync.cache_for().objects / (hashlib.sha256(raw).hexdigest() + ".eml")
        obj.write_bytes(b"synthetic corruption")
        with self.assertRaises(CacheError) as caught:
            sync.local_message("INBOX", 2, "123")
        self.assertEqual(caught.exception.code, "CACHE_INTEGRITY")
        self.session.calls.clear()
        self.assertTrue(sync.refresh()["ok"])
        self.assertEqual([c[1] for c in self.session.calls if c[0] == "fetch"], [2])
        self.assertEqual(obj.read_bytes(), raw)
        self.assertEqual(sync.local_message("INBOX", 2, "123")["subject"], "测试对账 2")

    def test_default_mcp_search_get_attachment_never_connect_or_read_credential(self):
        sync.refresh(max_messages=10)
        self.session_factory.side_effect = AssertionError("local tool tried network")
        self.session_factory.reset_mock()
        server = make_server()

        async def use_local_tools():
            for name, arguments in (
                ("mail_search", {"query": "发票"}),
                ("mail_get_message", {"folder": "INBOX", "uid": 1, "uidvalidity": "123"}),
                ("mail_download_attachment", {"folder": "INBOX", "uid": 1, "uidvalidity": "123", "attachment_index": 0}),
            ):
                result = await server.call_tool(name, arguments)
                # FastMCP versions may return text blocks or structured output.
                payload = result[1] if isinstance(result, tuple) else json.loads(result[0].text)
                self.assertTrue(payload["ok"], result)
                if name == "mail_download_attachment":
                    self.assertEqual(Path(payload["path"]).read_bytes(), b"%PDF-1.4\nsynthetic-test")

        with patch.object(reader, "Session", side_effect=AssertionError("network")) as live_session, patch.object(reader, "get_credential", side_effect=AssertionError("credential")) as credential, patch("mail263.config.credential_backend", side_effect=AssertionError("vault")) as vault:
            asyncio.run(use_local_tools())
            live_session.assert_not_called()
            credential.assert_not_called()
            vault.assert_not_called()
        self.session_factory.assert_not_called()

    def test_fts_long_and_short_chinese_match_literal_fallback(self):
        self.session.messages = {1: message(1, '九月发票 100% A_B "quote"'), 2: message(2, "仅一字税")}
        sync.refresh(max_messages=10)
        cache = sync.cache_for()
        for query in ("九月发票", "税", "发票", "100%", "A_B", '"quote"', "' OR 1=1 --"):
            with self.subTest(query=query):
                indexed = [r["uid"] for r in cache.search(query)["results"]]
                original = cache.fts
                cache.fts = False
                literal = [r["uid"] for r in cache.search(query)["results"]]
                cache.fts = original
                self.assertEqual(indexed, literal)


@unittest.skipUnless(platform.system() == "Windows", "Native Windows Credential Manager test")
class NativeWindowsCredentialTests(unittest.TestCase):
    def test_native_winvault_synthetic_roundtrip_and_scoped_cleanup(self):
        # UUID isolation: never reference an existing mailbox service or account.
        marker = str(uuid.uuid4())
        service = "Mail263Codex-CI-Synthetic-" + marker
        username = "synthetic-" + marker + "@example.invalid"
        secret = "synthetic-only-" + str(uuid.uuid4())
        backend = credential_backend()
        self.assertEqual(type(backend).__name__, "WinVaultKeyring")
        try:
            self.assertIsNone(backend.get_password(service, username))
            backend.set_password(service, username, secret)
            self.assertEqual(backend.get_password(service, username), secret)
        finally:
            # Delete only this test's exact random service + username pair.
            if backend.get_password(service, username) is not None:
                backend.delete_password(service, username)
        self.assertIsNone(backend.get_password(service, username))


if __name__ == "__main__":
    unittest.main()

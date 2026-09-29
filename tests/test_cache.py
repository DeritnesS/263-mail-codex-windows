import hashlib
from pathlib import Path
import tempfile
import unittest

from mail263.cache import Cache, CacheError


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = Cache(self.root, "account-a@example.invalid")
        self.cache.begin_folder("收件箱", "123", "2026-01-01")

    def tearDown(self):
        self.temp.cleanup()

    def add(self, uid=1, body="九月发票 待对账", when="2026-09-01T10:00:00+08:00", cache=None, validity="123"):
        raw = (str(uid) + body).encode()
        (cache or self.cache).put("收件箱", validity, uid, raw, {"subject": "付款通知", "body": body, "attachments": [{"filename": "测试发票.pdf"}], "message_sha256": hashlib.sha256(raw).hexdigest()}, when)
        return raw

    def test_chinese_short_terms_literal_and_pagination(self):
        self.add(1, "税 100% A_B")
        self.add(2, "税 普通发票")
        self.assertEqual(self.cache.search("税", limit=1, offset=1)["results"][0]["uid"], 1)
        self.assertEqual(self.cache.search("%") ["total"], 1)
        self.assertEqual(self.cache.search("A_B")["total"], 1)
        self.assertEqual(self.cache.search("' OR 1=1 --")["total"], 0)
        self.assertEqual(self.cache.search("测试发票")["total"], 2)
        self.assertEqual(self.cache.search("税", before="2026-09-01")["total"], 0)
        with self.assertRaises(CacheError):
            self.cache.search("", limit=101)

    def test_accounts_never_share_results_or_get(self):
        self.add()
        other = Cache(self.root, "account-b@example.invalid")
        other.begin_folder("收件箱", "123", "2026-01-01")
        self.assertEqual(other.search("")["total"], 0)
        self.assertEqual(other.known_uids("收件箱", "123"), set())
        with self.assertRaises(CacheError):
            other.get("收件箱", 1, "123")

    def test_generation_change_preserves_but_hides_original(self):
        raw = self.add()
        self.cache.record_refresh("收件箱", status="success", coverage_since="2026-01-01", attempted_at="2026-09-01T00:00:00Z")
        self.cache.begin_folder("收件箱", "456", "2026-01-01")
        self.assertEqual(self.cache.search("")["total"], 0)
        self.assertIsNone(self.cache.status()["folders"][0]["last_success_at"])
        self.assertEqual((self.root / "objects" / (hashlib.sha256(raw).hexdigest() + ".eml")).read_bytes(), raw)
        with self.assertRaises(CacheError):
            self.cache.get("收件箱", 1, "123")

    def test_reconcile_requires_complete_same_scope_and_retains_raw(self):
        raw = self.add()
        self.add(2, when="2025-01-01T00:00:00Z")
        with self.assertRaises(CacheError):
            self.cache.reconcile("收件箱", "123", [], coverage_since="2026-01-01", inventory_complete=False)
        with self.assertRaises(CacheError):
            self.cache.reconcile("收件箱", "123", [], coverage_since="2026-08-01", inventory_complete=True)
        result = self.cache.reconcile("收件箱", "123", [], coverage_since="2026-01-01", inventory_complete=True)
        self.assertEqual(result["marked_remote_deleted"], 1)
        self.assertEqual(self.cache.search("")["results"][0]["uid"], 2)
        self.assertTrue((self.root / "objects" / (hashlib.sha256(raw).hexdigest() + ".eml")).exists())

    def test_failed_partial_refresh_never_advances_success(self):
        self.add()
        self.cache.record_refresh("收件箱", status="success", coverage_since="2026-01-01", attempted_at="2026-09-01T00:00:00Z")
        self.cache.begin_folder("收件箱", "123", "2025-01-01")
        self.cache.record_refresh("收件箱", status="partial", coverage_since="2025-01-01", pending=12, skipped=[{"uid": 4, "reason": "too_large"}], attempted_at="2026-09-02T00:00:00Z")
        status = self.cache.status()["folders"][0]
        self.assertEqual(status["last_success_at"], "2026-09-01T00:00:00Z")
        self.assertEqual(status["coverage_since"], "2026-01-01")
        self.assertEqual(status["attempted_scope_since"], "2025-01-01")
        self.assertEqual(status["pending"], 12)
        self.assertTrue(status["partial"])

    def test_raw_integrity_and_conflicting_uid(self):
        raw = self.add()
        self.assertEqual(self.cache.get("收件箱", 1, "123"), raw)
        with self.assertRaises(CacheError):
            self.add(1, "不同的邮件")
        obj = self.root / "objects" / (hashlib.sha256(raw).hexdigest() + ".eml")
        obj.write_bytes(b"corrupted")
        with self.assertRaises(CacheError):
            self.cache.get("收件箱", 1, "123")
        obj.unlink()
        self.assertEqual(self.cache.known_uids("收件箱", "123"), set())
        self.add()
        self.assertEqual(self.cache.get("收件箱", 1, "123"), raw)

    def test_known_uid_set_supports_older_backfill_holes(self):
        self.add(3)
        self.add(100)
        self.assertEqual({1, 3, 50, 100} - self.cache.known_uids("收件箱", "123"), {1, 50})

    def test_reconcile_uses_imap_calendar_day_not_utc_or_date_header(self):
        self.add(1, when="2026-01-01T00:05:00+08:00")
        self.add(2, when="2025-12-31T23:55:00-08:00")
        result = self.cache.reconcile("收件箱", "123", [], coverage_since="2026-01-01", inventory_complete=True)
        self.assertEqual(result["marked_remote_deleted"], 1)
        self.assertEqual([r["uid"] for r in self.cache.search("")["results"]], [2])

    def test_success_label_with_skipped_mail_remains_partial(self):
        self.cache.record_refresh("收件箱", status="success", coverage_since="2026-01-01", skipped=[{"uid": 99, "reason": "too_large"}])
        state = self.cache.status()["folders"][0]
        self.assertEqual(state["status"], "partial")
        self.assertIsNone(state["last_success_at"])

    def test_wrong_parsed_hash_never_creates_object(self):
        with self.assertRaises(CacheError):
            self.cache.put("收件箱", "123", 1, b"raw", {"message_sha256": "wrong"})
        self.assertEqual(list((self.root / "objects").iterdir()), [])

    def test_connection_failure_is_persistent_without_fake_generation(self):
        self.cache.record_refresh("新目录", status="failed", coverage_since="2026-07-01", errors=["NETWORK"], attempted_at="2026-09-03T00:00:00Z")
        reopened = Cache(self.root, "account-a@example.invalid")
        state = next(f for f in reopened.status()["folders"] if f["folder"] == "新目录")
        self.assertIsNone(state["uidvalidity"])
        self.assertIsNone(state["last_success_at"])
        self.assertIsNone(state["coverage_since"])
        self.assertEqual(state["attempted_scope_since"], "2026-07-01")
        self.assertEqual(state["last_attempt_at"], "2026-09-03T00:00:00Z")
        self.assertEqual(state["errors"], ["NETWORK"])
        with self.assertRaises(CacheError):
            self.cache.get("新目录", 1, "123")

    def test_failure_before_new_scope_inventory_keeps_old_success(self):
        self.cache.record_refresh("收件箱", status="success", coverage_since="2026-01-01", attempted_at="2026-09-01T00:00:00Z")
        # Failure before begin_folder / SELECT with a newly requested date range.
        self.cache.record_refresh("收件箱", status="failed", coverage_since="2024-01-01", errors="LOGIN", attempted_at="2026-09-03T00:00:00Z")
        state = self.cache.status()["folders"][0]
        self.assertEqual(state["uidvalidity"], "123")
        self.assertEqual(state["coverage_since"], "2026-01-01")
        self.assertEqual(state["last_success_at"], "2026-09-01T00:00:00Z")
        self.assertEqual(state["attempted_scope_since"], "2024-01-01")
        self.assertEqual(state["status"], "failed")


if __name__ == "__main__":
    unittest.main()

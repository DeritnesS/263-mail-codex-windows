from contextlib import nullcontext
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from mail263.cache import Cache
from mail263.config import Config
from mail263.reader import MAX_MESSAGE_BYTES

from mail263 import sync
RAW = b"Subject: synthetic invoice\r\n\r\ninvoice"


class Fake:
    def __init__(self):
        self.failures = {2, 3}
        self.attempts = []
        self.fetched = []

    def __enter__(self): return self
    def __exit__(self, *args): pass
    def select(self, folder): return "1"
    def search_uids(self, criteria): return [1, 2, 3]
    def size(self, uid):
        self.attempts.append(uid)
        return MAX_MESSAGE_BYTES + 1 if uid in self.failures else len(RAW)
    def internaldate(self, uid): return "2026-09-29T09:00:00+08:00"
    def fetch(self, uid, size):
        self.fetched.append(uid)
        return RAW


class Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Cache(Path(tmp.name), "synthetic-account")
        self.fake = Fake()
        for patcher in (patch.object(sync, "load_config", return_value=Config("test@example.invalid")), patch.object(sync, "cache_for", return_value=self.cache), patch.object(sync, "writer_lock", side_effect=lambda: nullcontext()), patch.object(sync, "Session", return_value=self.fake)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_all_untried_uids_progress_and_failures_remain_reported(self):
        for _ in range(3):
            result = sync.refresh(max_messages=1)
        self.assertEqual(self.fake.attempts, [3, 2, 1])
        self.assertEqual(self.fake.fetched, [1])
        self.assertEqual(result["folders"][0]["pending"], 2)
        self.assertEqual({x["uid"] for x in result["folders"][0]["skipped"]}, {2, 3})
        self.assertFalse(result["ok"])

    def test_failed_metadata_retries_rotate_and_recovery_removes_issue(self):
        for _ in range(5):
            sync.refresh(max_messages=1)
        self.assertEqual(self.fake.attempts, [3, 2, 1, 3, 2])
        self.fake.failures.clear()
        sync.refresh(max_messages=1)
        result = sync.refresh(max_messages=1)
        self.assertEqual(self.cache.known_uids("INBOX", "1"), {1, 2, 3})
        self.assertTrue(result["ok"])
        self.assertEqual(result["folders"][0]["skipped"], [])


if __name__ == "__main__": unittest.main()

"""Account-isolated local evidence cache; no network or credential access.

Original MIME objects are immutable by hash. Remote removals and UIDVALIDITY
changes affect visibility, never erase originals. Refresh success is recorded
separately from an attempted/incomplete inventory.
"""
from contextlib import contextmanager
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile


class CacheError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _day(value):
    if not value:
        return ""
    try:
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise CacheError("DATE_INPUT", "日期须使用 YYYY-MM-DD。") from None


def _uid(value):
    try:
        n = int(value)
        if isinstance(value, bool) or str(n) != str(value) or not 1 <= n <= 4294967295:
            raise ValueError
        return n
    except (ValueError, TypeError):
        raise CacheError("UID_INPUT", "UID 和 UIDVALIDITY 须为有效正整数。") from None


def _timestamp(value):
    if not value:
        return ""
    try:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            dt = parsedate_to_datetime(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError):
        return ""


def _internal_day(value):
    """IMAP SINCE ignores time and timezone: use the server's calendar day."""
    if not value:
        return ""
    try:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            dt = parsedate_to_datetime(str(value))
        return dt.date().isoformat()
    except (ValueError, TypeError, OverflowError):
        return ""


SCHEMA = """
CREATE TABLE IF NOT EXISTS folder (
 account TEXT NOT NULL, name TEXT NOT NULL, uidvalidity TEXT NOT NULL,
 pending_scope TEXT NOT NULL DEFAULT '', coverage_since TEXT,
 last_attempt_at TEXT, last_success_at TEXT, status TEXT NOT NULL DEFAULT 'never',
 pending INTEGER NOT NULL DEFAULT 0, skipped_json TEXT NOT NULL DEFAULT '0',
 errors_json TEXT NOT NULL DEFAULT '[]', PRIMARY KEY(account,name)
);
CREATE TABLE IF NOT EXISTS message (
 account TEXT NOT NULL, folder TEXT NOT NULL, uidvalidity TEXT NOT NULL,
 uid INTEGER NOT NULL, sha256 TEXT NOT NULL, parsed_json TEXT NOT NULL,
 subject TEXT NOT NULL, sender TEXT NOT NULL, recipients TEXT NOT NULL,
 body TEXT NOT NULL, searchable TEXT NOT NULL, date_utc TEXT NOT NULL,
 internal_date TEXT NOT NULL, fetched_at TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1, remote_deleted INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(account,folder,uidvalidity,uid)
);
CREATE INDEX IF NOT EXISTS message_current ON message(account,folder,uidvalidity,active,date_utc);
CREATE TABLE IF NOT EXISTS refresh_attempt (
 account TEXT NOT NULL, folder TEXT NOT NULL, attempted_scope_since TEXT NOT NULL,
 last_attempt_at TEXT NOT NULL, status TEXT NOT NULL, pending INTEGER NOT NULL,
 skipped_json TEXT NOT NULL, errors_json TEXT NOT NULL,
 PRIMARY KEY(account,folder)
);
"""


class Cache:
    def __init__(self, root: Path, account_key: str):
        if not account_key or not str(account_key).strip():
            raise CacheError("ACCOUNT_INPUT", "本地索引需要邮箱账号标识。")
        self.root = Path(root)
        self.account = hashlib.sha256(str(account_key).strip().casefold().encode()).hexdigest()
        self.objects = self.root / "objects"
        self.db_path = self.root / "index.sqlite3"
        self.objects.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(SCHEMA)
            columns = {r[1] for r in db.execute("PRAGMA table_info(message)")}
            if "internal_day" not in columns:
                db.execute("ALTER TABLE message ADD COLUMN internal_day TEXT NOT NULL DEFAULT ''")
                for row in db.execute("SELECT rowid,internal_date FROM message").fetchall():
                    db.execute("UPDATE message SET internal_day=? WHERE rowid=?", (_internal_day(row["internal_date"]), row["rowid"]))
            if "needs_refetch" not in columns:
                db.execute("ALTER TABLE message ADD COLUMN needs_refetch INTEGER NOT NULL DEFAULT 0")
            self.fts = bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='message_fts'").fetchone())
            if not self.fts:
                try:
                    db.execute("CREATE VIRTUAL TABLE message_fts USING fts5(searchable,content='message',content_rowid='rowid',tokenize='trigram')")
                    db.execute("INSERT INTO message_fts(message_fts) VALUES('rebuild')")
                    self.fts = True
                except sqlite3.OperationalError:
                    # Older/custom SQLite builds still support literal matching.
                    self.fts = False
            if self.fts:
                db.executescript("""
                CREATE TRIGGER IF NOT EXISTS message_ai AFTER INSERT ON message BEGIN
                  INSERT INTO message_fts(rowid,searchable) VALUES(new.rowid,new.searchable);
                END;
                CREATE TRIGGER IF NOT EXISTS message_ad AFTER DELETE ON message BEGIN
                  INSERT INTO message_fts(message_fts,rowid,searchable) VALUES('delete',old.rowid,old.searchable);
                END;
                CREATE TRIGGER IF NOT EXISTS message_au AFTER UPDATE OF searchable ON message BEGIN
                  INSERT INTO message_fts(message_fts,rowid,searchable) VALUES('delete',old.rowid,old.searchable);
                  INSERT INTO message_fts(rowid,searchable) VALUES(new.rowid,new.searchable);
                END;
                """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _folder(self, db, folder, validity=None):
        row = db.execute("SELECT * FROM folder WHERE account=? AND name=?", (self.account, folder)).fetchone()
        if row is None:
            raise CacheError("CACHE_FOLDER_UNKNOWN", "此文件夹尚未建立本地索引，请先刷新。")
        if validity is not None and row["uidvalidity"] != str(_uid(validity)):
            raise CacheError("UIDVALIDITY_CHANGED", "文件夹标识已变化，请重新搜索本地索引。")
        return row

    def begin_folder(self, folder: str, uidvalidity, coverage_since="") -> dict:
        validity, scope = str(_uid(uidvalidity)), _day(coverage_since)
        if not isinstance(folder, str) or not folder:
            raise CacheError("FOLDER_INPUT", "文件夹名称不能为空。")
        with self._db() as db:
            old = db.execute("SELECT * FROM folder WHERE account=? AND name=?", (self.account, folder)).fetchone()
            changed = old is not None and old["uidvalidity"] != validity
            if changed:
                db.execute("UPDATE message SET active=0 WHERE account=? AND folder=?", (self.account, folder))
                db.execute("DELETE FROM folder WHERE account=? AND name=?", (self.account, folder))
            db.execute("""INSERT INTO folder(account,name,uidvalidity,pending_scope,status)
              VALUES(?,?,?,?, 'running') ON CONFLICT(account,name) DO UPDATE SET
              pending_scope=excluded.pending_scope,status='running'""", (self.account, folder, validity, scope))
            self._attempt(db, folder, scope, _now(), "running", 0, 0, [])
        return {"folder": folder, "uidvalidity": validity, "generation_changed": changed, "coverage_since": scope}

    def known_uids(self, folder: str, uidvalidity) -> set[int]:
        with self._db() as db:
            self._folder(db, folder, uidvalidity)
            rows = db.execute("SELECT uid,sha256 FROM message WHERE account=? AND folder=? AND uidvalidity=? AND needs_refetch=0", (self.account, folder, str(uidvalidity))).fetchall()
        # A lost object is fetched again on the next refresh, not silently skipped.
        return {r["uid"] for r in rows if (self.objects / (r["sha256"] + ".eml")).is_file()}

    def put(self, folder: str, uidvalidity, uid, raw: bytes, parsed: dict, internal_date="") -> dict:
        validity, uid = str(_uid(uidvalidity)), _uid(uid)
        if not isinstance(raw, bytes) or not isinstance(parsed, dict):
            raise CacheError("CACHE_INPUT", "原始邮件必须是 bytes，解析结果必须是对象。")
        sha = hashlib.sha256(raw).hexdigest()
        if parsed.get("message_sha256") not in (None, sha):
            raise CacheError("CACHE_INTEGRITY", "解析结果与原始邮件的 SHA-256 不一致。")
        payload = json.dumps(parsed, ensure_ascii=False)
        body = str(parsed.get("body", parsed.get("body_text", "")))
        subject, sender = str(parsed.get("subject", "")), str(parsed.get("from", ""))
        recipients = str(parsed.get("to", "")) + " " + str(parsed.get("cc", ""))
        attachment_names = " ".join(str(a.get("filename", "")) for a in parsed.get("attachments", []) if isinstance(a, dict))
        searchable = "\n".join((subject, sender, recipients, body, attachment_names, str(parsed.get("message_id", ""))))
        internal_date = str(internal_date or "")
        date_utc = _timestamp(internal_date) or _timestamp(parsed.get("date_header", parsed.get("date", "")))
        target = self.objects / (sha + ".eml")
        with self._db() as db:
            self._folder(db, folder, validity)
            existing = db.execute("SELECT sha256 FROM message WHERE account=? AND folder=? AND uidvalidity=? AND uid=?", (self.account, folder, validity, uid)).fetchone()
            if existing and existing["sha256"] != sha:
                raise CacheError("CACHE_IDENTITY_CONFLICT", "同一 UID 的原件发生变化；请检查服务器标识，原记录已保留。")
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != sha:
                name = None
                try:
                    with tempfile.NamedTemporaryFile(dir=self.objects, prefix=".mail-", delete=False) as handle:
                        name = handle.name
                        handle.write(raw)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(name, target)
                finally:
                    if name and os.path.exists(name):
                        os.unlink(name)
            db.execute("""INSERT INTO message(account,folder,uidvalidity,uid,sha256,parsed_json,subject,sender,recipients,body,searchable,date_utc,internal_date,fetched_at,internal_day)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account,folder,uidvalidity,uid) DO UPDATE SET active=1,remote_deleted=0,needs_refetch=0""",
              (self.account, folder, validity, uid, sha, payload, subject, sender, recipients, body, searchable, date_utc, internal_date, _now(), _internal_day(internal_date)))
        return {"folder": folder, "uidvalidity": validity, "uid": uid, "message_sha256": sha, "active": True}

    def reconcile(self, folder: str, uidvalidity, remote_uids, *, coverage_since="", inventory_complete: bool) -> dict:
        validity, scope = str(_uid(uidvalidity)), _day(coverage_since)
        if inventory_complete is not True:
            raise CacheError("INCOMPLETE_INVENTORY", "远端 UID 清单未完成，不能判断邮件已删除。")
        remote = {_uid(uid) for uid in remote_uids}
        with self._db() as db:
            state = self._folder(db, folder, validity)
            if state["pending_scope"] != scope:
                raise CacheError("SCOPE_MISMATCH", "远端 UID 清单与本轮同步日期范围不同。")
            # Compare with IMAP INTERNALDATE, not the sender-controlled Date header.
            rows = db.execute("SELECT uid,internal_date,remote_deleted FROM message WHERE account=? AND folder=? AND uidvalidity=?", (self.account, folder, validity)).fetchall()
            removed = 0
            for row in rows:
                internal = _internal_day(row["internal_date"])
                if scope and (not internal or internal < scope):
                    continue
                present = row["uid"] in remote
                db.execute("UPDATE message SET active=?,remote_deleted=? WHERE account=? AND folder=? AND uidvalidity=? AND uid=?", (int(present), int(not present), self.account, folder, validity, row["uid"]))
                removed += int(not present and not row["remote_deleted"])
        return {"marked_remote_deleted": removed, "remote_count": len(remote), "coverage_since": scope}

    def _attempt(self, db, folder, scope, timestamp, status, pending, skipped, errors):
        db.execute("""INSERT INTO refresh_attempt(account,folder,attempted_scope_since,last_attempt_at,status,pending,skipped_json,errors_json)
          VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(account,folder) DO UPDATE SET
          attempted_scope_since=excluded.attempted_scope_since,last_attempt_at=excluded.last_attempt_at,
          status=excluded.status,pending=excluded.pending,skipped_json=excluded.skipped_json,errors_json=excluded.errors_json""",
          (self.account, folder, scope, timestamp, status, int(pending), json.dumps(skipped, ensure_ascii=False), json.dumps(errors, ensure_ascii=False)))

    def record_refresh(self, folder: str, *, status: str, coverage_since="", uidvalidity=None,
                       skipped=0, errors=None, pending=0, attempted_at=None) -> None:
        if status not in {"success", "partial", "failed", "running"}:
            raise CacheError("STATUS_INPUT", "同步状态须为 success、partial、failed 或 running。")
        scope = _day(coverage_since)
        if pending < 0 or (isinstance(skipped, int) and skipped < 0):
            raise CacheError("STATUS_INPUT", "待处理或跳过数量不能为负数。")
        errors = errors or []
        successful = status == "success" and not pending and not skipped and not errors
        if status == "success" and not successful:
            status = "partial"
        timestamp = attempted_at or _now()
        with self._db() as db:
            state = db.execute("SELECT * FROM folder WHERE account=? AND name=?", (self.account, folder)).fetchone()
            if uidvalidity is not None:
                self._folder(db, folder, uidvalidity)
            if state is None and status != "failed":
                raise CacheError("CACHE_FOLDER_UNKNOWN", "成功或部分完成的同步须先取得真实 UIDVALIDITY。")
            if state is not None and status != "failed" and state["pending_scope"] != scope:
                raise CacheError("SCOPE_MISMATCH", "刷新状态与本轮同步日期范围不同。")
            # Authentication/network failure has no reliable generation. Persist
            # the attempt independently without inventing one or erasing coverage.
            self._attempt(db, folder, scope, timestamp, status, pending, skipped, errors)
            if state is None:
                return
            db.execute("""UPDATE folder SET last_attempt_at=?,status=?,pending=?,skipped_json=?,errors_json=?,
               last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END,
               coverage_since=CASE WHEN ? THEN ? ELSE coverage_since END WHERE account=? AND name=?""",
               (timestamp, status, int(pending), json.dumps(skipped, ensure_ascii=False), json.dumps(errors, ensure_ascii=False), successful, timestamp, successful, scope, self.account, folder))

    def status(self) -> dict:
        with self._db() as db:
            rows = db.execute("SELECT * FROM folder WHERE account=? ORDER BY name", (self.account,)).fetchall()
            attempts = {r["folder"]: dict(r) for r in db.execute("SELECT * FROM refresh_attempt WHERE account=?", (self.account,))}
            count = db.execute("SELECT COUNT(*) FROM message m JOIN folder f ON m.account=f.account AND m.folder=f.name AND m.uidvalidity=f.uidvalidity WHERE m.account=? AND m.active=1", (self.account,)).fetchone()[0]
        folders = []
        for row in rows:
            item = {k: row[k] for k in ("uidvalidity", "coverage_since", "last_attempt_at", "last_success_at", "status", "pending")}
            item.update(folder=row["name"], attempted_scope_since=row["pending_scope"], skipped=json.loads(row["skipped_json"]), errors=json.loads(row["errors_json"]), partial=row["status"] != "success", bounded_scope=row["coverage_since"] not in (None, ""))
            attempt = attempts.pop(row["name"], None)
            if attempt:
                item.update({k: attempt[k] for k in ("attempted_scope_since", "last_attempt_at", "status", "pending")})
                item.update(skipped=json.loads(attempt["skipped_json"]), errors=json.loads(attempt["errors_json"]), partial=attempt["status"] != "success")
            folders.append(item)
        for name, attempt in attempts.items():
            item = {k: attempt[k] for k in ("attempted_scope_since", "last_attempt_at", "status", "pending")}
            item.update(folder=name, uidvalidity=None, coverage_since=None, last_success_at=None,
                        skipped=json.loads(attempt["skipped_json"]), errors=json.loads(attempt["errors_json"]), partial=True, bounded_scope=False)
            folders.append(item)
        folders.sort(key=lambda f: f["folder"])
        return {"source": "local_cache", "network_accessed": False, "search_engine": "fts5_trigram_with_short_query_fallback" if self.fts else "literal_scan_fallback", "active_messages": count, "folders": folders, "partial": not folders or any(f["partial"] for f in folders)}

    def search(self, query: str, folder="", since="", before="", limit=10, offset=0) -> dict:
        since, before = _day(since), _day(before)
        if not isinstance(query, str) or len(query) > 1000:
            raise CacheError("QUERY_INPUT", "关键词须为不超过 1000 字的文本。")
        if since and before and since >= before:
            raise CacheError("DATE_INPUT", "开始日期须早于结束日期。")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100 or isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise CacheError("PAGE_INPUT", "每页须为 1 至 100 条，offset 须为非负整数。")
        conditions, params = ["m.account=?", "m.active=1"], [self.account]
        if query:
            if self.fts and len(query) >= 3:
                conditions.append("m.rowid IN (SELECT rowid FROM message_fts WHERE message_fts MATCH ?)")
                params.append('"' + query.replace('"', '""') + '"')
            literal = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append("m.searchable LIKE ? ESCAPE '\\'")
            params.append("%" + literal + "%")
        if folder:
            conditions.append("m.folder=?")
            params.append(folder)
        if since:
            conditions.append("m.internal_day>=?")
            params.append(since)
        if before:
            conditions.append("m.internal_day<?")
            params.append(before)
        base = " FROM message m JOIN folder f ON m.account=f.account AND m.folder=f.name AND m.uidvalidity=f.uidvalidity WHERE " + " AND ".join(conditions)
        with self._db() as db:
            total = db.execute("SELECT COUNT(*)" + base, params).fetchone()[0]
            rows = db.execute("SELECT m.*" + base + " ORDER BY m.date_utc DESC,m.folder,m.uid DESC LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
        results = []
        for row in rows:
            parsed = json.loads(row["parsed_json"])
            result = {k: parsed.get(k, "") for k in ("subject", "from", "to", "cc", "date_header", "message_id")}
            result.update(folder=row["folder"], uid=row["uid"], uidvalidity=row["uidvalidity"], internal_date=row["internal_date"], body_excerpt=row["body"][:1000], attachments=parsed.get("attachments", []), message_sha256=row["sha256"], active=True, remote_deleted=False, source="local_cache")
            results.append(result)
        freshness = self.status()
        if folder:
            freshness["folders"] = [f for f in freshness["folders"] if f["folder"] == folder]
            freshness["partial"] = not freshness["folders"] or any(f["partial"] for f in freshness["folders"])
        scope_gaps = [f["folder"] for f in freshness["folders"] if not f["coverage_since"] or (since and since < f["coverage_since"])]
        return {"results": results, "total": total, "limit": limit, "offset": offset,
                "next_offset": offset + limit if offset + limit < total else None,
                "query_scope": {"folder": folder or "all_cached_folders", "since": since, "before": before,
                                "date_basis": "IMAP_INTERNALDATE_calendar_day", "older_than_covered_folders": scope_gaps,
                                "all_mailbox_history_claimed": False}, "freshness": freshness}

    def get(self, folder: str, uid, uidvalidity) -> bytes:
        uid, validity = _uid(uid), str(_uid(uidvalidity))
        with self._db() as db:
            self._folder(db, folder, validity)
            row = db.execute("SELECT sha256 FROM message WHERE account=? AND folder=? AND uidvalidity=? AND uid=? AND active=1", (self.account, folder, validity, uid)).fetchone()
        if row is None:
            raise CacheError("CACHE_NOT_FOUND", "当前索引中没有这封邮件；它可能未同步或已从远端移除。")
        try:
            raw = (self.objects / (row["sha256"] + ".eml")).read_bytes()
        except FileNotFoundError:
            raise CacheError("CACHE_NOT_FOUND", "本地原件缺失，请重新刷新此文件夹。") from None
        if hashlib.sha256(raw).hexdigest() != row["sha256"]:
            with self._db() as db:
                db.execute("UPDATE message SET needs_refetch=1 WHERE account=? AND folder=? AND uidvalidity=? AND uid=?", (self.account, folder, validity, uid))
            raise CacheError("CACHE_INTEGRITY", "本地原件 SHA-256 校验失败，请重新获取原件。")
        return raw

"""One bounded incremental writer; readers remain available from SQLite."""
from contextlib import contextmanager
import os
import time

from .cache import Cache
from .config import data_dir, load_config
from .errors import MailError, error_result
from .reader import Session, parse_message, save_attachment, imap_date, now, MAX_MESSAGE_BYTES


def cache_for(config=None):
    config = config or load_config()
    return Cache(data_dir() / "cache", config.credential_service)


@contextmanager
def writer_lock():
    root = data_dir()
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / "refresh.lock").open("a+b")
    locked = False
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise MailError("REFRESH_BUSY", "已有同步正在运行；可继续查本地邮件，稍后检查同步状态。") from None
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def refresh(folder: str = "", since: str = "", max_messages: int = 0) -> dict:
    """Refresh configured folders. A complete UID inventory precedes reconciliation.

    Full MIME is retained (including attachment payloads); there is no repeated
    transfer of known UIDs. Limits bound new content per folder and per call.
    """
    config = load_config()
    folders = [folder] if folder else config.sync_folders
    since = since or config.sync_since
    imap_date(since)
    max_messages = max_messages or config.sync_max_messages
    if not 1 <= max_messages <= 500:
        raise MailError("SYNC_LIMIT", "每个目录每轮新增邮件上限为 1 到 500。")
    results = []
    with writer_lock():
        cache = cache_for(config)
        previous = {item["folder"]: item for item in cache.status()["folders"]}
        try:
            with Session(config) as session:
                for name in folders:
                    validity = None
                    attempted_at = now()
                    try:
                        validity = session.select(name)
                        cache.begin_folder(name, validity, since)
                        remote = session.search_uids(["SINCE", imap_date(since)])
                        cache.reconcile(name, validity, remote, coverage_since=since, inventory_complete=True)
                        known = cache.known_uids(name, validity)
                        pending_set = set(remote) - known
                        prior = previous.get(name, {})
                        old_issues = prior.get("skipped", []) if prior.get("uidvalidity") == validity else []
                        # Retain every unresolved issue, including UIDs not attempted
                        # this round. Otherwise two failing pages can alternate forever.
                        issues_by_uid = {
                            issue["uid"]: issue for issue in old_issues
                            if isinstance(issue, dict) and issue.get("uid") in pending_set
                        } if isinstance(old_issues, list) else {}
                        retry_position = {uid: index for index, uid in enumerate(issues_by_uid)}
                        pending_uids = sorted(pending_set, key=lambda uid: (uid in retry_position, retry_position.get(uid, -uid)))
                        downloaded, transferred = 0, 0
                        start = time.monotonic()
                        # Each folder has a 100 MiB transfer budget and a soft
                        # 45-second work budget, checked between network operations.
                        for uid in pending_uids[:max_messages]:
                            if time.monotonic() - start >= 45 or transferred >= 100 * 1024 * 1024:
                                break
                            try:
                                size = session.size(uid)
                                if size > MAX_MESSAGE_BYTES:
                                    issues_by_uid.pop(uid, None)
                                    issues_by_uid[uid] = {"uid": uid, "code": "MESSAGE_TOO_LARGE"}
                                    continue
                                if transferred + size > 100 * 1024 * 1024:
                                    break
                                if time.monotonic() - start >= 45:
                                    break
                                internal_date = session.internaldate(uid)
                                if time.monotonic() - start >= 45:
                                    break
                                raw = session.fetch(uid, size)
                                transferred += len(raw)  # Count transfer even if parsing/storage fails.
                                parsed, _ = parse_message(raw)
                                cache.put(name, validity, uid, raw, parsed, internal_date)
                                downloaded += 1
                                issues_by_uid.pop(uid, None)
                            except Exception as exc:
                                issues_by_uid.pop(uid, None)
                                issues_by_uid[uid] = {"uid": uid, "code": error_result(exc)["error"]["code"]}
                        # Preserve retry order: failed attempts move to the back.
                        issues = list(issues_by_uid.values())
                        pending = len(pending_uids) - downloaded
                        state = "success" if not pending and not issues else "partial"
                        cache.record_refresh(name, status=state, coverage_since=since, uidvalidity=validity,
                                             pending=pending, skipped=issues, attempted_at=attempted_at)
                        results.append({"folder": name, "status": state, "coverage_since": since,
                                        "uidvalidity": validity, "remote_in_scope": len(remote),
                                        "downloaded": downloaded, "pending": pending, "skipped": issues,
                                        "transferred_bytes": transferred})
                    except Exception as exc:
                        issue = error_result(exc)["error"]
                        cache.record_refresh(name, status="failed", coverage_since=since,
                                             uidvalidity=validity, errors=[issue], attempted_at=attempted_at)
                        results.append({"folder": name, "status": "failed", "error": issue})
        except Exception as exc:
            issue = error_result(exc)["error"]
            for name in folders:
                cache.record_refresh(name, status="failed", coverage_since=since, errors=[issue])
            return {"ok": False, "query_time": now(), "error": issue, "local_query_available": True,
                    "freshness": cache.status()}
    return {"ok": all(r["status"] == "success" for r in results), "query_time": now(),
            "folders": results, "local_query_available": True, "freshness": cache.status()}


def local_search(query: str = "", folder: str = "", since: str = "", before: str = "",
                 limit: int = 10, offset: int = 0) -> dict:
    result = cache_for().search(query=query, folder=folder, since=since, before=before, limit=limit, offset=offset)
    return {"ok": True, "source": "local_index", "query_time": now(), **result}


def local_message(folder: str, uid: int, uidvalidity: str, max_chars: int = 12000) -> dict:
    if not 100 <= max_chars <= 50000:
        raise MailError("READ_LIMIT", "max_chars 范围为 100 到 50000。")
    cache = cache_for()
    raw = cache.get(folder, uid, uidvalidity)
    parsed, _ = parse_message(raw)
    body = parsed["body"]
    parsed["body"] = body[:max_chars]
    return {"ok": True, "source": "local_original", "query_time": now(),
            "folder": folder, "uid": uid, "uidvalidity": str(uidvalidity), **parsed,
            "body_truncated": len(body) > max_chars, "freshness": cache.status()}


def local_attachment(folder: str, uid: int, uidvalidity: str, attachment_index: int) -> dict:
    cache = cache_for()
    raw = cache.get(folder, uid, uidvalidity)
    return {**save_attachment(raw, folder, uid, uidvalidity, attachment_index),
            "source": "local_original", "freshness": cache.status()}

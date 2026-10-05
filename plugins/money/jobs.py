"""Durable, per-user work queue. No request-owned threads or external broker."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
import json
import logging
import time
import uuid

from . import ledger, store

log = logging.getLogger(__name__)
ACTIVE = ("queued", "running", "retry")
LOOKBACKS = ("0d", "7d", "1m", "3m", "6m", "12m")
INTERVAL = 1800
LEASE = 120

SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_jobs (
 id TEXT PRIMARY KEY, provider TEXT NOT NULL, account TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued', options TEXT NOT NULL DEFAULT '{}',
 progress TEXT NOT NULL DEFAULT '{}', attempts INTEGER NOT NULL DEFAULT 0,
 available_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
 lease_token TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
 created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_sync_active ON sync_jobs(provider,account)
 WHERE status IN ('queued','running','retry');
CREATE TABLE IF NOT EXISTS sync_settings (
 provider TEXT NOT NULL, account TEXT NOT NULL,
 automatic INTEGER NOT NULL DEFAULT 0, lookback TEXT NOT NULL DEFAULT '7d',
 auto_approve INTEGER NOT NULL DEFAULT 0,
 next_run REAL NOT NULL DEFAULT 0, last_success REAL,
 PRIMARY KEY(provider,account)
);
"""


def lookback_start(value, today=None):
    today = today or date.today()
    if value not in LOOKBACKS:
        raise ledger.ValidationError("Choose 0d, 7d, 1m, 3m, 6m or 12m")
    if value == "0d":
        return today
    if value == "7d":
        return today - timedelta(days=7)
    months = today.year * 12 + today.month - 1 - int(value[:-1])
    year, month = divmod(months, 12)
    return date(year, month + 1, min(today.day, monthrange(year, month + 1)[1]))


def _decode(row):
    if row:
        for key in ("options", "progress"):
            row[key] = json.loads(row[key])
    return row


def get(conn, job_id):
    row = _decode(store.one(conn, "SELECT * FROM sync_jobs WHERE id=?", (job_id,)))
    if not row:
        raise ledger.ValidationError("Job not found")
    return row


def public(row):
    # Cursors, OAuth data and the fencing token never reach the browser/tools.
    return {
        k: row[k]
        for k in (
            "id",
            "provider",
            "account",
            "status",
            "attempts",
            "error",
            "created_at",
            "updated_at",
            "available_at",
        )
    } | {
        "progress": {
            k: row["progress"].get(k, 0)
            for k in (
                "done",
                "total",
                "discovered",
                "candidates",
                "approved",
                "duplicates",
                "skipped",
            )
        },
        "lookback": row["options"].get("lookback", "7d"),
    }


def recent(conn):
    return [
        public(_decode(r))
        for r in store.rows(
            conn, "SELECT * FROM sync_jobs ORDER BY created_at DESC LIMIT 30"
        )
    ]


def enqueue(conn, provider, account="", options=None):
    if provider not in ("gmail", "sheets", "calendar"):
        raise ledger.ValidationError("Unknown connected app")
    options = dict(options or {})
    if provider == "gmail":
        value = options.get("lookback", "7d")
        options["after"] = lookback_start(value).isoformat()
        options["lookback"] = value
    now = time.time()
    job_id = uuid.uuid4().hex
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO sync_jobs "
            "(id,provider,account,options,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (job_id, provider, account, json.dumps(options), now, now),
        )
    return _decode(
        store.one(
            conn,
            "SELECT * FROM sync_jobs WHERE provider=? AND account=? "
            "AND status IN ('queued','running','retry')",
            (provider, account),
        )
    )


def settings(conn, provider, account=""):
    return store.one(
        conn,
        "SELECT * FROM sync_settings WHERE provider=? AND account=?",
        (provider, account),
    ) or dict(
        provider=provider,
        account=account,
        automatic=0,
        lookback="7d",
        auto_approve=0,
        next_run=0,
        last_success=None,
    )


def configure(conn, provider, account, automatic, lookback="7d", auto_approve=False):
    if (
        provider not in ("gmail", "sheets", "calendar")
        or type(automatic) is not bool
        or type(auto_approve) is not bool
    ):
        raise ledger.ValidationError(
            "A supported app and boolean preferences are required"
        )
    lookback_start(lookback)
    with conn:
        conn.execute(
            "INSERT INTO sync_settings(provider,account,automatic,lookback,auto_approve,next_run) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(provider,account) DO UPDATE SET "
            "automatic=excluded.automatic,lookback=excluded.lookback,"
            "auto_approve=excluded.auto_approve,next_run=excluded.next_run",
            (provider, account, int(automatic), lookback, int(auto_approve),
             time.time() + INTERVAL),
        )
    return settings(conn, provider, account)


def claim(conn, now=None):
    now = time.time() if now is None else now
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = store.one(
            conn,
            "SELECT * FROM sync_jobs WHERE "
            "(status IN ('queued','retry') AND available_at<=?) OR "
            "(status='running' AND lease_until<?) ORDER BY updated_at LIMIT 1",
            (now, now),
        )
        if not row:
            return None
        if row["status"] == "running" and row["attempts"] >= 4:
            conn.execute(
                "UPDATE sync_jobs SET status='failed',error='Worker repeatedly interrupted',lease_token='' WHERE id=?",
                (row["id"],),
            )
            conn.execute(
                "UPDATE sync_settings SET automatic=0 WHERE provider=? AND account=?",
                (row["provider"], row["account"]),
            )
            return None
        token = uuid.uuid4().hex
        conn.execute(
            "UPDATE sync_jobs SET status='running',lease_token=?,lease_until=?,updated_at=?,attempts=? WHERE id=?",
            (
                token,
                now + LEASE,
                now,
                row["attempts"] + int(row["status"] == "running"),
                row["id"],
            ),
        )
    return get(conn, row["id"])


def owns(conn, job):
    return bool(
        store.one(
            conn,
            "SELECT id FROM sync_jobs WHERE id=? AND status='running' AND lease_token=?",
            (job["id"], job["lease_token"]),
        )
    )


def checkpoint(conn, job, progress, finished=False):
    now = time.time()
    with conn:
        cur = conn.execute(
            "UPDATE sync_jobs SET progress=?,status=?,lease_token='',lease_until=0,"
            "attempts=0,error='',updated_at=? WHERE id=? AND status='running' AND lease_token=?",
            (
                json.dumps(progress),
                "succeeded" if finished else "queued",
                now,
                job["id"],
                job["lease_token"],
            ),
        )
        if cur.rowcount and finished:
            conn.execute(
                "INSERT INTO sync_settings(provider,account,last_success,next_run) VALUES(?,?,?,?) "
                "ON CONFLICT(provider,account) DO UPDATE SET last_success=excluded.last_success, "
                "next_run=excluded.next_run",
                (job["provider"], job["account"], now, now + INTERVAL),
            )
    return bool(cur.rowcount)


def cancel(conn, job_id):
    get(conn, job_id)
    with conn:
        conn.execute(
            "UPDATE sync_jobs SET status='cancelled',lease_token='',updated_at=? "
            "WHERE id=? AND status IN ('queued','running','retry')",
            (time.time(), job_id),
        )
    return public(get(conn, job_id))


def cancel_provider(conn, provider, account=None):
    query = "provider=?" + (" AND account=?" if account is not None else "")
    args = (provider, account) if account is not None else (provider,)
    with conn:
        conn.execute(
            "UPDATE sync_jobs SET status='cancelled',lease_token='',updated_at=? WHERE "
            + query
            + " AND status IN ('queued','running','retry')",
            (time.time(), *args),
        )
        conn.execute("DELETE FROM sync_settings WHERE " + query, args)


def fail(conn, job, message, now=None, permanent=False):
    now = time.time() if now is None else now
    attempts = job["attempts"] + 1
    terminal = permanent or attempts >= 5
    with conn:
        cur = conn.execute(
            "UPDATE sync_jobs SET status=?,attempts=?,error=?,available_at=?,lease_token='',"
            "updated_at=? WHERE id=? AND status='running' AND lease_token=?",
            (
                "failed" if terminal else "retry",
                attempts,
                message[:250],
                now + min(1800, 30 * 2 ** (attempts - 1)),
                now,
                job["id"],
                job["lease_token"],
            ),
        )
        if cur.rowcount and terminal:
            # A broken grant must not be retried forever by the automatic schedule.
            conn.execute(
                "UPDATE sync_settings SET automatic=0 WHERE provider=? AND account=?",
                (job["provider"], job["account"]),
            )


def retry(conn, job_id):
    old = get(conn, job_id)
    if old["status"] not in ("failed", "cancelled"):
        raise ledger.ValidationError("Only failed or cancelled jobs can be retried")
    return enqueue(conn, old["provider"], old["account"], old["options"])


def schedule(conn):
    now = time.time()
    for row in store.rows(
        conn, "SELECT * FROM sync_settings WHERE automatic=1 AND next_run<=?", (now,)
    ):
        options = {"lookback": row["lookback"]}
        if row["provider"] == "gmail" and row["last_success"]:
            # One-day overlap covers delays; durable dedupe makes overlap safe.
            options["since"] = int(row["last_success"] - 86400)
        enqueue(conn, row["provider"], row["account"], options)
        with conn:
            conn.execute(
                "UPDATE sync_settings SET next_run=? WHERE provider=? AND account=?",
                (now + INTERVAL, row["provider"], row["account"]),
            )
    with conn:
        conn.execute(
            "DELETE FROM sync_jobs WHERE status IN ('succeeded','cancelled') AND updated_at<?",
            (now - 30 * 86400,),
        )


def run_one(conn, stop):
    if stop.is_set():
        return
    job = claim(conn)
    if not job:
        return
    try:
        if job["provider"] == "gmail":
            from .gmail import job_step
        else:
            from .google_apps import job_step
        job_step(conn, job, stop)
    except Exception as exc:
        conn.rollback()
        # Provider errors intentionally exclude tokens, URLs and response bodies.
        from .gmail import GmailError

        known = isinstance(exc, (ledger.ValidationError, GmailError))
        fail(
            conn,
            job,
            str(exc) if known else "Background operation failed; retry or reconnect.",
            permanent=isinstance(exc, ledger.ValidationError)
            or getattr(exc, "permanent", False),
        )
        if not known:
            log.warning("Money %s job failed (%s)", job["provider"], type(exc).__name__)


def tick(api, stop):
    from app.plugins.services import active_user
    from app.runtime.tools.user_ctx import bind_user, reset_user

    for path in (api.data_dir / "users").glob("*/ledger.db"):
        if stop.is_set():
            return
        conn = store.connect(path.parent)
        token = None
        try:
            owner = store.get_setting(conn, "jobs_owner")
            if not owner:
                continue
            try:
                active_user(owner)
            except PermissionError:
                continue
            if api.user_data_dir(owner).resolve() != path.parent.resolve():
                continue
            token = bind_user(owner)
            schedule(conn)
            run_one(conn, stop)
        finally:
            if token is not None:
                reset_user(token)
            conn.close()

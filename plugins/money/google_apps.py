"""Google companions: separate grants, typed sheet exports, idempotent reminders."""

from __future__ import annotations

import base64
from datetime import date, timedelta
import hashlib
import html
import re
import secrets
import time
import uuid
from urllib.parse import urlencode, quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from app.core.secrets import encrypt_secret, decrypt_secret
from . import gmail, jobs, ledger, store

SCOPES = {
    "sheets": "https://www.googleapis.com/auth/spreadsheets",
    "calendar": "https://www.googleapis.com/auth/calendar.events.owned",
}
ROOTS = {
    "sheets": "https://sheets.googleapis.com/v4/spreadsheets/",
    "calendar": "https://www.googleapis.com/calendar/v3/calendars/",
}
TIMEOUT = httpx.Timeout(10)
SCHEMA = """
CREATE TABLE IF NOT EXISTS google_connections (
 provider TEXT PRIMARY KEY, email TEXT NOT NULL, access_token TEXT NOT NULL,
 refresh_token TEXT NOT NULL, expires_at REAL NOT NULL, target TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS google_oauth_states (
 state TEXT PRIMARY KEY, provider TEXT NOT NULL, redirect_uri TEXT NOT NULL,
 verifier TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS recurring_bills (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, amount_minor INTEGER NOT NULL,
 currency TEXT NOT NULL, due TEXT NOT NULL, recurrence TEXT NOT NULL,
 timezone TEXT NOT NULL, reminder_days INTEGER NOT NULL DEFAULT 1,
 active INTEGER NOT NULL DEFAULT 1
);
"""


class GoogleError(gmail.GmailError):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status
        self.permanent = status in (400, 401, 403, 404)


def provider_check(provider):
    if provider not in SCOPES:
        raise ledger.ValidationError("Unknown Google integration")


def connections(conn):
    return {
        r["provider"]: r
        for r in store.rows(
            conn, "SELECT provider,email,target FROM google_connections"
        )
    }


def begin_oauth(conn, provider, redirect_uri):
    provider_check(provider)
    client, secret = gmail.credentials(conn)
    if not client or not secret:
        raise GoogleError("Save your Google OAuth client credentials first")
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    with conn:
        conn.execute(
            "DELETE FROM google_oauth_states WHERE created_at<?", (time.time() - 3600,)
        )
        conn.execute(
            "INSERT INTO google_oauth_states VALUES(?,?,?,?,?)",
            (state, provider, redirect_uri, encrypt_secret(verifier), time.time()),
        )
    return (
        gmail.AUTH_URL
        + "?"
        + urlencode(
            dict(
                client_id=client,
                redirect_uri=redirect_uri,
                response_type="code",
                scope=SCOPES[provider]
                + " https://www.googleapis.com/auth/userinfo.email",
                access_type="offline",
                prompt="consent",
                state=state,
                code_challenge=challenge,
                code_challenge_method="S256",
            )
        )
    )


def _response(resp):
    if not 200 <= resp.status_code < 300:
        status = resp.status_code
        advice = (
            " Reconnect or check access to the destination."
            if status in (400, 401, 403, 404)
            else " Will retry."
        )
        exc = GoogleError(f"Google returned HTTP {status}.{advice}", status=status)
        if gmail.rate_limited(resp):
            exc.permanent = False
            exc.args = ("Google rate limit reached. Will retry.",)
        raise exc
    return resp.json() if resp.content else {}


def token_post(data):
    try:
        return _response(httpx.post(gmail.TOKEN_URL, data=data, timeout=TIMEOUT))
    except httpx.HTTPError:
        raise GoogleError("Google token service unavailable; retry.") from None


def finish_oauth(conn, provider, code, state):
    provider_check(provider)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = store.one(
            conn,
            "SELECT * FROM google_oauth_states WHERE state=? AND provider=?",
            (state, provider),
        )
        conn.execute(
            "DELETE FROM google_oauth_states WHERE state=? AND provider=?",
            (state, provider),
        )
    if not row or row["created_at"] < time.time() - 3600 or not code:
        raise GoogleError("OAuth state expired or invalid; connect again", status=400)
    client, secret = gmail.credentials(conn)
    tokens = token_post(
        dict(
            client_id=client,
            client_secret=secret,
            code=code,
            redirect_uri=row["redirect_uri"],
            grant_type="authorization_code",
            code_verifier=decrypt_secret(row["verifier"]),
        )
    )
    if "scope" in tokens and SCOPES[provider] not in tokens["scope"].split():
        raise GoogleError("Required permission was not granted; reconnect", status=403)
    access = tokens.get("access_token", "")
    if not access:
        raise GoogleError("Google returned no access token", status=400)
    try:
        profile = _response(
            httpx.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access}"},
                timeout=TIMEOUT,
            )
        )
    except httpx.HTTPError:
        raise GoogleError("Could not read Google account identity; reconnect") from None
    email = profile.get("email", "")
    if not email:
        raise GoogleError("Google returned no account email", status=400)
    old = store.one(
        conn, "SELECT * FROM google_connections WHERE provider=?", (provider,)
    )
    refresh = tokens.get("refresh_token") or (
        decrypt_secret(old["refresh_token"]) if old and old["email"] == email else ""
    )
    if not refresh:
        raise GoogleError("Offline access missing; reconnect with consent", status=400)
    jobs.cancel_provider(conn, provider)
    with conn:
        conn.execute(
            "INSERT INTO google_connections(provider,email,access_token,refresh_token,expires_at,target) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET email=excluded.email,"
            "access_token=excluded.access_token,refresh_token=excluded.refresh_token,"
            "expires_at=excluded.expires_at,target=excluded.target",
            (
                provider,
                email,
                encrypt_secret(access),
                encrypt_secret(refresh),
                time.time() + int(tokens.get("expires_in", 3600)) - 60,
                old["target"] if old and old["email"] == email else "",
            ),
        )
    return connections(conn)[provider]


def disconnect(conn, provider):
    provider_check(provider)
    # Revoking a Google grant may invalidate other integrations using the client.
    jobs.cancel_provider(conn, provider)
    with conn:
        conn.execute("DELETE FROM google_connections WHERE provider=?", (provider,))
        conn.execute("DELETE FROM google_oauth_states WHERE provider=?", (provider,))
    return {"disconnected": True}


def configure(conn, provider, target, automatic):
    provider_check(provider)
    if provider not in connections(conn):
        raise ledger.ValidationError("Connect this app first")
    target = str(target or "").strip()
    if provider == "sheets":
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", target):
            raise ledger.ValidationError("Enter the spreadsheet ID from its URL")
    elif not target or len(target) > 254 or any(ch.isspace() for ch in target):
        raise ledger.ValidationError("Enter a calendar ID or primary")
    if type(automatic) is not bool:
        raise ledger.ValidationError("automatic must be a boolean")
    jobs.cancel_provider(conn, provider)
    with conn:
        conn.execute(
            "UPDATE google_connections SET target=? WHERE provider=?",
            (target, provider),
        )
    return jobs.configure(conn, provider, "", automatic)


def request(
    conn, provider, method, path, *, json=None, params=None, guard=lambda: True
):
    """Only fixed Google origins; user destinations are URL-quoted path segments."""
    if not guard():
        raise ledger.ValidationError("Job cancelled")
    row = store.one(
        conn, "SELECT * FROM google_connections WHERE provider=?", (provider,)
    )
    if not row:
        raise ledger.ValidationError("App disconnected")
    access = decrypt_secret(row["access_token"])

    def refresh():
        client, secret = gmail.credentials(conn)
        tokens = token_post(
            dict(
                client_id=client,
                client_secret=secret,
                refresh_token=decrypt_secret(row["refresh_token"]),
                grant_type="refresh_token",
            )
        )
        new = tokens.get("access_token")
        if not new:
            raise GoogleError("Refresh failed; reconnect", status=401)
        if not guard():
            raise ledger.ValidationError("Job cancelled")
        with conn:
            cur = conn.execute(
                "UPDATE google_connections SET access_token=?,expires_at=? WHERE provider=? AND refresh_token=?",
                (
                    encrypt_secret(new),
                    time.time() + int(tokens.get("expires_in", 3600)) - 60,
                    provider,
                    row["refresh_token"],
                ),
            )
            if not cur.rowcount:
                raise ledger.ValidationError(
                    "Google connection changed; retry the current job"
                )
        return new

    if row["expires_at"] <= time.time() + 30 or not access:
        access = refresh()
    for attempt in range(2):
        if not guard():
            raise ledger.ValidationError("Job cancelled")
        try:
            resp = httpx.request(
                method,
                ROOTS[provider] + path,
                headers={"Authorization": f"Bearer {access}"},
                json=json,
                params=params,
                timeout=TIMEOUT,
            )
        except httpx.HTTPError:
            raise GoogleError(
                "Google request timed out or is unavailable; retry."
            ) from None
        if resp.status_code == 401 and attempt == 0:
            access = refresh()
            continue
        return _response(resp)


def bills(conn):
    return store.rows(
        conn, "SELECT * FROM recurring_bills WHERE active=1 ORDER BY due,title"
    )


def save_bill(conn, fields):
    title = str(fields.get("title", "")).strip()
    if not title or len(title) > 120:
        raise ledger.ValidationError("Bill title must contain 1–120 characters")
    amount = ledger.parse_amount_minor(fields.get("amount"))
    if amount <= 0:
        raise ledger.ValidationError("Bill amount must be positive")
    raw_due = str(fields.get("due", ""))
    try:
        due = date.fromisoformat(raw_due).isoformat()
    except (ValueError, TypeError):
        raise ledger.ValidationError("A valid bill due date is required") from None
    recurrence = fields.get("recurrence", "once")
    if recurrence not in ("once", "weekly", "monthly", "yearly"):
        raise ledger.ValidationError("Choose once, weekly, monthly or yearly")
    timezone = str(fields.get("timezone", "UTC"))
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ledger.ValidationError(
            "Use an IANA timezone such as Asia/Jakarta"
        ) from None
    try:
        reminder = int(fields.get("reminder_days", 1))
    except (ValueError, TypeError):
        raise ledger.ValidationError(
            "Reminder days must be an integer from 0 to 28"
        ) from None
    if not 0 <= reminder <= 28:
        raise ledger.ValidationError("Reminder days must be 0–28")
    currency = str(fields.get("currency", "IDR")).upper()
    if not re.fullmatch("[A-Z]{3}", currency):
        raise ledger.ValidationError("Use a three-letter currency code")
    bill_id = fields.get("id") or uuid.uuid4().hex
    if fields.get("id") and not store.one(
        conn, "SELECT id FROM recurring_bills WHERE id=? AND active=1", (bill_id,)
    ):
        raise ledger.ValidationError("Bill not found")
    with conn:
        conn.execute(
            "INSERT INTO recurring_bills(id,title,amount_minor,currency,due,recurrence,timezone,reminder_days) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,"
            "amount_minor=excluded.amount_minor,currency=excluded.currency,due=excluded.due,"
            "recurrence=excluded.recurrence,timezone=excluded.timezone,reminder_days=excluded.reminder_days",
            (bill_id, title, amount, currency, due, recurrence, timezone, reminder),
        )
    return store.one(conn, "SELECT * FROM recurring_bills WHERE id=?", (bill_id,))


def delete_bill(conn, bill_id):
    with conn:
        cur = conn.execute(
            "UPDATE recurring_bills SET active=0 WHERE id=? AND active=1", (bill_id,)
        )
    if not cur.rowcount:
        raise ledger.ValidationError("Bill not found")
    return {"deleted": True}


def calendar_event(bill):
    event = {
        "id": "tomo" + bill["id"],
        "summary": bill["title"],
        "description": html.escape(
            f"Bill reminder: {bill['currency']} {bill['amount_minor'] / 100:.2f}. Managed by Tomo Money."
        ),
        "start": {"date": bill["due"], "timeZone": bill["timezone"]},
        "end": {
            "date": (date.fromisoformat(bill["due"]) + timedelta(days=1)).isoformat(),
            "timeZone": bill["timezone"],
        },
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": bill["reminder_days"] * 1440}],
        },
        "extendedProperties": {"private": {"tomoMoneyBill": bill["id"]}},
    }
    if bill["recurrence"] != "once":
        event["recurrence"] = ["RRULE:FREQ=" + bill["recurrence"].upper()]
    return event


def _sheet_rows(conn):
    rows = store.rows(
        conn,
        "SELECT t.*,s.name AS source FROM transactions t LEFT JOIN sources s ON s.id=t.source_id ORDER BY t.day,t.id LIMIT 10001",
    )
    if len(rows) > 10000:
        raise ledger.ValidationError(
            "Sheet export supports up to 10,000 transactions; no data was replaced"
        )
    values = [
        [
            "ID",
            "Date",
            "Type",
            "Amount",
            "Currency",
            "Category",
            "Source",
            "Method",
            "Note",
        ]
    ]
    values += [
        [
            str(r["id"]),
            r["day"],
            r["kind"],
            r["amount_minor"] / 100,
            r["currency"],
            r["category"],
            r["source"] or "",
            r["method"],
            r["note"],
        ]
        for r in rows
    ]
    return [
        {
            "values": [
                {
                    "userEnteredValue": {"numberValue": v}
                    if isinstance(v, (int, float))
                    else {"stringValue": str(v)}
                }
                for v in row
            ]
        }
        for row in values
    ]


def job_step(conn, job, stop):
    provider = job["provider"]

    def live():
        return not stop.is_set() and jobs.owns(conn, job)

    if not live():
        return
    connection = connections(conn).get(provider)
    if not connection or not connection["target"]:
        raise ledger.ValidationError("Connect the app and save a destination first")
    target = quote(connection["target"], safe="")

    def remote(method, path, **kwargs):
        if not live():
            raise ledger.ValidationError("Job cancelled")
        return request(conn, provider, method, path, guard=live, **kwargs)

    if provider == "sheets":
        rows = _sheet_rows(conn)
        sheet = remote("GET", target, params={"fields": "sheets.properties"})
        tab = next(
            (
                s["properties"]
                for s in sheet.get("sheets", [])
                if s["properties"]["title"] == "Tomo Money"
            ),
            None,
        )
        if not tab:
            # If a response is lost, the next retry discovers the created tab.
            remote(
                "POST",
                target + ":batchUpdate",
                json={
                    "requests": [{"addSheet": {"properties": {"title": "Tomo Money"}}}]
                },
            )
            sheet = remote("GET", target, params={"fields": "sheets.properties"})
            tab = next(
                s["properties"]
                for s in sheet["sheets"]
                if s["properties"]["title"] == "Tomo Money"
            )
        reqs = []
        grid = tab.get("gridProperties", {})
        if grid and (
            grid.get("rowCount", 0) < len(rows) or grid.get("columnCount", 0) < 9
        ):
            reqs.append(
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": tab["sheetId"],
                            "gridProperties": {
                                "rowCount": max(len(rows), grid.get("rowCount", 0)),
                                "columnCount": max(9, grid.get("columnCount", 0)),
                            },
                        },
                        "fields": "gridProperties(rowCount,columnCount)",
                    }
                }
            )
        # A range without an end replaces all values, including obsolete trailing rows.
        reqs.append(
            {
                "updateCells": {
                    "range": {"sheetId": tab["sheetId"]},
                    "rows": rows,
                    "fields": "userEnteredValue",
                }
            }
        )
        remote("POST", target + ":batchUpdate", json={"requests": reqs})
        if live():
            jobs.checkpoint(
                conn,
                job,
                {"done": len(rows) - 1, "total": len(rows) - 1, "discovered": True},
                finished=True,
            )
        return
    progress = dict(job["progress"])
    if "pending" not in progress:
        progress = {
            "pending": [
                r["id"]
                for r in store.rows(conn, "SELECT id FROM recurring_bills ORDER BY id")
            ],
            "done": 0,
            "total": conn.execute("SELECT COUNT(*) FROM recurring_bills").fetchone()[0],
            "discovered": True,
        }
    if progress["pending"]:
        bill = store.one(
            conn, "SELECT * FROM recurring_bills WHERE id=?", (progress["pending"][0],)
        )
        event = calendar_event(bill)
        path = target + "/events/" + event["id"]
        if bill["active"]:
            try:
                remote("PUT", path, json=event)
            except GoogleError as exc:
                if exc.status != 404:
                    raise
                try:
                    remote("POST", target + "/events", json=event)
                except GoogleError as conflict:
                    if conflict.status != 409:
                        raise
                    remote("PUT", path, json=event)
        else:
            try:
                remote("DELETE", path)
            except GoogleError as exc:
                if exc.status not in (404, 410):
                    raise
        progress["pending"] = progress["pending"][1:]
        progress["done"] += 1
    if live():
        jobs.checkpoint(conn, job, progress, finished=not progress["pending"])

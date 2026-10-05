"""Gmail connection: OAuth2 flow and receipt-mail sync into the Money inbox.

Users paste their own Google OAuth client credentials on the Connected apps
page (Google Cloud console → OAuth client, redirect URI shown in the UI).
Tokens live in the per-user plugin database; scope is gmail.readonly only.
"""

from __future__ import annotations

import secrets
import time
from urllib.parse import urlencode

import httpx

from app.core.secrets import decrypt_secret, encrypt_secret

from . import capture, ledger, store

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

_SEARCH_QUERY = (
    'receipt OR "e-receipt" OR "order confirmation" OR "payment receipt" OR '
    '"transaction alert" OR invoice OR struk OR pembayaran OR tagihan '
    "newer_than:90d"
)
_MAX_MESSAGES = 60
_TIMEOUT = httpx.Timeout(20.0)
# OAuth callback work must stay well under typical reverse-proxy budgets —
# the exchange + profile fetch run sequentially inside one request.
_OAUTH_TIMEOUT = httpx.Timeout(10.0)


class GmailError(RuntimeError):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status
        self.permanent = status in (400, 401, 403)


def rate_limited(response):
    if response.status_code != 403:
        return False
    try:
        errors = response.json().get("error", {}).get("errors", [])
        return any(
            e.get("reason")
            in (
                "rateLimitExceeded",
                "userRateLimitExceeded",
                "dailyLimitExceeded",
                "quotaExceeded",
            )
            for e in errors
        )
    except (ValueError, AttributeError, TypeError):
        return False


def credentials(conn) -> tuple[str, str]:
    return (
        store.get_setting(conn, "gmail_client_id"),
        decrypt_secret(store.get_setting(conn, "gmail_client_secret")),
    )


def _raw_account(conn, where: str = "", params: tuple = ()) -> list[dict]:
    """Account rows with tokens decrypted for API use."""
    rows = store.rows(conn, "SELECT * FROM gmail_accounts" + where, params)
    for row in rows:
        row["_refresh_cipher"] = row["refresh_token"]
        row["access_token"] = decrypt_secret(row["access_token"]) or row["access_token"]
        row["refresh_token"] = (
            decrypt_secret(row["refresh_token"]) or row["refresh_token"]
        )
    return rows


def configured(conn) -> bool:
    client_id, secret = credentials(conn)
    return bool(client_id and secret)


def accounts(conn) -> list[dict]:
    rows = store.rows(conn, "SELECT * FROM gmail_accounts ORDER BY email")
    for row in rows:
        row["access_token"] = "•••" if row["access_token"] else ""
        row["refresh_token"] = "•••" if row["refresh_token"] else ""
    return rows


def begin_oauth(conn, redirect_uri: str) -> str:
    client_id, _ = credentials(conn)
    if not client_id:
        raise GmailError("Save your Google OAuth client credentials first")
    state = secrets.token_urlsafe(24)
    conn.execute(
        "INSERT INTO oauth_states (state, redirect_uri, created_at) VALUES (?,?,?)",
        (state, redirect_uri, int(time.time())),
    )
    conn.execute(
        "DELETE FROM oauth_states WHERE created_at < ?", (int(time.time()) - 3600,)
    )
    conn.commit()
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _check_state(conn, state: str) -> str:
    row = store.one(conn, "SELECT * FROM oauth_states WHERE state=?", (state or "",))
    conn.execute("DELETE FROM oauth_states WHERE state=?", (state or "",))
    conn.commit()
    if not row or int(row["created_at"] or 0) < int(time.time()) - 3600:
        raise GmailError("OAuth state expired — start the connection again")
    return row["redirect_uri"]


def finish_oauth(conn, code: str, state: str) -> dict:
    redirect_uri = _check_state(conn, state)
    client_id, secret = credentials(conn)
    try:
        resp = httpx.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            timeout=_OAUTH_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise GmailError(f"Token exchange failed: {exc}") from None
    if resp.status_code != 200:
        raise GmailError(f"Google rejected the code (HTTP {resp.status_code})")
    tokens = resp.json()
    access = tokens.get("access_token") or ""
    refresh = tokens.get("refresh_token") or ""
    expires_at = int(time.time()) + int(tokens.get("expires_in") or 3600) - 60
    if not access:
        raise GmailError("Google returned no access token")
    profile = _api_get(access, f"{API}/profile", timeout=_OAUTH_TIMEOUT)
    email = profile.get("emailAddress") or "gmail"
    existing = _raw_account(conn, " WHERE email=?", (email,))
    if not refresh and existing:
        refresh = existing[0]["refresh_token"]
    if not refresh:
        raise GmailError(
            "Google did not return a refresh token — reconnect with consent"
        )
    from . import jobs

    jobs.cancel_provider(conn, "gmail", email)
    conn.execute(
        "INSERT INTO gmail_accounts (email, access_token, refresh_token, expires_at,"
        " history_id) VALUES (?,?,?,?,?)"
        " ON CONFLICT(email) DO UPDATE SET access_token=excluded.access_token,"
        " refresh_token=excluded.refresh_token, expires_at=excluded.expires_at,"
        " history_id=excluded.history_id",
        (
            email,
            encrypt_secret(access),
            encrypt_secret(refresh),
            expires_at,
            str(profile.get("historyId") or ""),
        ),
    )
    conn.commit()
    return {"email": email, "connected": True}


def disconnect(conn, email: str | None = None) -> dict:
    from . import jobs

    jobs.cancel_provider(conn, "gmail", email)
    with conn:
        if email:
            conn.execute("DELETE FROM gmail_accounts WHERE email=?", (email,))
        else:
            conn.execute("DELETE FROM gmail_accounts")
        conn.execute("DELETE FROM oauth_states")
    return {"disconnected": True}


def _api_get(
    access_token: str,
    url: str,
    params: dict | None = None,
    timeout: httpx.Timeout | None = None,
) -> dict:
    try:
        resp = httpx.get(
            url,
            params=params or {},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=timeout or _TIMEOUT,
        )
    except httpx.HTTPError:
        raise GmailError("Gmail request unavailable; retry.") from None
    if resp.status_code == 401:
        raise GmailError("token_expired", status=401)
    if resp.status_code != 200:
        exc = GmailError(
            f"Gmail returned HTTP {resp.status_code}", status=resp.status_code
        )
        if rate_limited(resp):
            exc.permanent = False
        raise exc
    return resp.json()


def _refresh(conn, account: dict) -> dict:
    client_id, secret = credentials(conn)
    if not account.get("refresh_token"):
        raise GmailError("No refresh token — reconnect the account")
    try:
        resp = httpx.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": secret,
                "refresh_token": account["refresh_token"],
                "grant_type": "refresh_token",
            },
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise GmailError(f"Token refresh failed: {exc}") from None
    if resp.status_code != 200:
        raise GmailError("Google refresh failed — reconnect the account")
    tokens = resp.json()
    account["access_token"] = tokens.get("access_token") or account["access_token"]
    account["expires_at"] = (
        int(time.time()) + int(tokens.get("expires_in") or 3600) - 60
    )
    cur = conn.execute(
        "UPDATE gmail_accounts SET access_token=?, expires_at=? WHERE id=? AND refresh_token=?",
        (
            encrypt_secret(account["access_token"]),
            account["expires_at"],
            account["id"],
            account["_refresh_cipher"],
        ),
    )
    conn.commit()
    if not cur.rowcount:
        raise ledger.ValidationError("Gmail connection changed; retry the current job")
    return account


def _valid_token(conn, account: dict) -> dict:
    if int(account.get("expires_at") or 0) <= int(time.time()) + 30:
        return _refresh(conn, account)
    return account


def _list_ids(access: str, query: str, limit: int) -> list[str]:
    listing = _api_get(
        access,
        f"{API}/messages",
        {"q": query, "maxResults": str(limit)},
    )
    return [m["id"] for m in listing.get("messages") or []]


def _get_full(access: str, message_id: str) -> dict:
    return _api_get(access, f"{API}/messages/{message_id}", {"format": "full"})


def search(
    conn,
    query: str = "",
    limit: int = 10,
    email: str | None = None,
) -> dict:
    """Search connected Gmail and return readable messages for the agent.

    Read-only: each result flags whether its external_key is already recorded
    or queued so the agent can skip or record it deliberately.
    """
    rows = _raw_account(
        conn,
        " WHERE email=?" if email else "",
        (email,) if email else (),
    )
    if not rows:
        raise GmailError("No Gmail account connected")
    query = (query or "").strip() or _SEARCH_QUERY
    limit = min(max(int(limit or 10), 1), 25)
    recorded = {
        row[0]
        for row in conn.execute(
            "SELECT external_key FROM transactions WHERE external_key IS NOT NULL"
        )
        if row[0]
    }
    queued = {
        row[0]
        for row in conn.execute(
            "SELECT json_extract(payload,'$.external_key') FROM inbox"
        )
        if row[0]
    }
    result = {"query": query, "accounts": [], "messages": []}
    for account in rows:
        account = _valid_token(conn, account)
        try:
            ids = _list_ids(account["access_token"], query, limit)
        except GmailError as exc:
            if str(exc) == "token_expired":
                account = _refresh(conn, account)
                ids = _list_ids(account["access_token"], query, limit)
            else:
                raise
        for message_id in ids:
            try:
                message = _get_full(account["access_token"], message_id)
            except GmailError:
                continue
            content = capture.gmail_message_content(message)
            key = f"gmail:{account['email'].lower()}:{content['id']}"
            legacy = f"gmail:{content['id']}"
            result["messages"].append(
                {
                    "id": content["id"],
                    "external_key": key,
                    "from": content["from"][:200],
                    "subject": content["subject"][:200],
                    "day": content["when"].isoformat() if content["when"] else "",
                    "snippet": content["snippet"][:240],
                    "body": content["body"][:3000],
                    "recorded": key in recorded or legacy in recorded,
                    "queued": key in queued or legacy in queued,
                    "account": account["email"],
                }
            )
        result["accounts"].append(account["email"])
    return result


def sync(
    conn,
    email: str | None = None,
    query: str | None = None,
) -> dict:
    """Fetch receipt-ish mail and enqueue parsed candidates. Idempotent."""
    rows = _raw_account(
        conn,
        " WHERE email=?" if email else "",
        (email,) if email else (),
    )
    if not rows:
        raise GmailError("No Gmail account connected")
    query = (query or "").strip() or _SEARCH_QUERY
    cats = [c["name"] for c in ledger.list_categories(conn)]
    report = {"scanned": 0, "candidates": 0, "duplicates": 0, "accounts": []}
    for account in rows:
        account = _valid_token(conn, account)
        try:
            ids = _list_ids(account["access_token"], query, _MAX_MESSAGES)
        except GmailError as exc:
            if str(exc) == "token_expired":
                account = _refresh(conn, account)
                ids = _list_ids(account["access_token"], query, _MAX_MESSAGES)
            else:
                raise
        report["scanned"] += len(ids)
        seen: set[str] = {
            row[0]
            for row in conn.execute(
                "SELECT json_extract(payload,'$.external_key') FROM inbox"
            )
            if row[0]
        }
        seen |= {
            row[0]
            for row in conn.execute(
                "SELECT external_key FROM transactions"
                " WHERE external_key LIKE 'gmail:%'"
            )
            if row[0]
        }
        for message_id in ids:
            key = f"gmail:{message_id}"
            if key in seen:
                report["duplicates"] += 1
                continue
            try:
                message = _get_full(account["access_token"], message_id)
            except GmailError:
                continue
            parsed = capture.parse_gmail_message(message, cats)
            if not parsed:
                continue
            result = ledger.add_candidate(
                conn, "gmail", parsed["payload"], parsed["provenance"]
            )
            if result.get("duplicate"):
                report["duplicates"] += 1
            else:
                report["candidates"] += 1
                seen.add(key)
        conn.execute(
            "UPDATE gmail_accounts SET last_sync_at=datetime('now') WHERE id=?",
            (account["id"],),
        )
        conn.commit()
        report["accounts"].append(account["email"])
    return report


def job_step(conn, job, stop):
    """One listing page or one message. Persist only IDs and parsed fields."""
    from . import jobs
    import json

    def live():
        return not stop.is_set() and jobs.owns(conn, job)

    if not live():
        return
    rows = _raw_account(conn, " WHERE email=?", (job["account"],))
    if not rows:
        raise ledger.ValidationError("Gmail disconnected; connect it before syncing")
    account = _valid_token(conn, rows[0])
    auto_approve = bool(
        jobs.settings(conn, "gmail", job["account"]).get("auto_approve")
    )
    progress = dict(job["progress"])
    progress.setdefault("done", 0)
    progress.setdefault("total", 0)
    progress.setdefault("candidates", 0)
    progress.setdefault("approved", 0)
    progress.setdefault("duplicates", 0)

    def fetch(url, params):
        nonlocal account
        try:
            return _api_get(
                account["access_token"], url, params, timeout=httpx.Timeout(10)
            )
        except GmailError as exc:
            if str(exc) != "token_expired":
                raise
            if not live():
                raise ledger.ValidationError("Job cancelled")
            account = _refresh(conn, account)
            return _api_get(
                account["access_token"], url, params, timeout=httpx.Timeout(10)
            )

    pending = progress.get("pending", [])
    if not pending and not progress.get("discovered"):
        after = job["options"].get("since") or job["options"]["after"].replace("-", "/")
        # Group OR terms so the date applies to every payment phrase.
        terms = _SEARCH_QUERY.rsplit(" newer_than:", 1)[0]
        params = {"q": f"({terms}) after:{after}", "maxResults": "100"}
        if progress.get("page_token"):
            params["pageToken"] = progress["page_token"]
        listing = fetch(f"{API}/messages", params)
        pending = [m["id"] for m in listing.get("messages", [])]
        progress["pending"] = pending
        progress["total"] += len(pending)
        progress["page_token"] = listing.get("nextPageToken", "")
        progress["discovered"] = not bool(progress["page_token"])
    elif pending:
        message_id = pending[0]
        key = f"gmail:{account['email'].lower()}:{message_id}"
        legacy = f"gmail:{message_id}"
        seen = conn.execute(
            "SELECT 1 FROM inbox WHERE json_extract(payload,'$.external_key') IN (?,?) "
            "UNION ALL SELECT 1 FROM transactions WHERE external_key IN (?,?) LIMIT 1",
            (key, legacy, key, legacy),
        ).fetchone()
        parsed = None
        skipped = False
        if not seen:
            try:
                message = fetch(f"{API}/messages/{message_id}", {"format": "full"})
            except GmailError as exc:
                if exc.status != 404:
                    raise
                skipped = True
            else:
                cats = [c["name"] for c in ledger.list_categories(conn)]
                parsed = capture.parse_gmail_message(message, cats)
        if not live():
            return
        # Serialize the final ownership check and candidate write against cancel.
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if not live():
                return
            if seen:
                progress["duplicates"] += 1
            elif skipped:
                progress["skipped"] = progress.get("skipped", 0) + 1
            elif parsed:
                parsed["provenance"]["external_key"] = key
                parsed["provenance"]["account"] = account["email"]
                parsed["provenance"].pop("snippet", None)
                payload = {**parsed["payload"], "external_key": key}
                cur = conn.execute(
                    "INSERT INTO inbox(origin,payload,provenance) VALUES(?,?,?)",
                    ("gmail", json.dumps(payload), json.dumps(parsed["provenance"])),
                )
                approved = False
                if auto_approve:
                    try:
                        ledger.add_transaction(conn, payload)
                    except ledger.ValidationError:
                        pass
                    else:
                        conn.execute(
                            "UPDATE inbox SET status='confirmed',"
                            " resolved_at=datetime('now') WHERE id=?",
                            (cur.lastrowid,),
                        )
                        approved = True
                if approved:
                    progress["approved"] += 1
                else:
                    progress["candidates"] += 1
            progress["pending"] = pending[1:]
            progress["done"] += 1
            # Same transaction: a crash cannot insert the candidate without its cursor.
            jobs.checkpoint(conn, job, progress)
        return
    finished = bool(progress.get("discovered")) and not progress.get("pending")
    if live():
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if not live():
                return
            if finished:
                conn.execute(
                    "UPDATE gmail_accounts SET last_sync_at=datetime('now') WHERE email=?",
                    (job["account"],),
                )
            jobs.checkpoint(conn, job, progress, finished=finished)

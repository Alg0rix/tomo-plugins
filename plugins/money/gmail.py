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
    pass


def credentials(conn) -> tuple[str, str]:
    return (
        store.get_setting(conn, "gmail_client_id"),
        decrypt_secret(store.get_setting(conn, "gmail_client_secret")),
    )


def _raw_account(conn, where: str = "", params: tuple = ()) -> list[dict]:
    """Account rows with tokens decrypted for API use."""
    rows = store.rows(conn, "SELECT * FROM gmail_accounts" + where, params)
    for row in rows:
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
    conn.execute("DELETE FROM oauth_states WHERE created_at < ?", (int(time.time()) - 3600,))
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
    row = store.one(
        conn, "SELECT * FROM oauth_states WHERE state=?", (state or "",)
    )
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
    existing = _raw_account(
        conn, " WHERE email=?", (email,)
    )
    if not refresh and existing:
        refresh = existing[0]["refresh_token"]
    if not refresh:
        raise GmailError(
            "Google did not return a refresh token — reconnect with consent"
        )
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
    if email:
        rows = _raw_account(conn, " WHERE email=?", (email,))
        if rows and rows[0]["access_token"]:
            try:
                httpx.post(
                    "https://oauth2.googleapis.com/revoke",
                    params={"token": rows[0]["access_token"]},
                    timeout=10.0,
                )
            except httpx.HTTPError:
                pass
        conn.execute("DELETE FROM gmail_accounts WHERE email=?", (email,))
    else:
        conn.execute("DELETE FROM gmail_accounts")
    conn.commit()
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
    except httpx.HTTPError as exc:
        raise GmailError(f"Gmail request failed: {exc}") from None
    if resp.status_code == 401:
        raise GmailError("token_expired")
    if resp.status_code != 200:
        raise GmailError(f"Gmail returned HTTP {resp.status_code}")
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
    account["expires_at"] = int(time.time()) + int(tokens.get("expires_in") or 3600) - 60
    conn.execute(
        "UPDATE gmail_accounts SET access_token=?, expires_at=? WHERE id=?",
        (encrypt_secret(account["access_token"]), account["expires_at"], account["id"]),
    )
    conn.commit()
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
            key = f"gmail:{content['id']}"
            result["messages"].append(
                {
                    "id": content["id"],
                    "external_key": key,
                    "from": content["from"][:200],
                    "subject": content["subject"][:200],
                    "day": content["when"].isoformat() if content["when"] else "",
                    "snippet": content["snippet"][:240],
                    "body": content["body"][:3000],
                    "recorded": key in recorded,
                    "queued": key in queued,
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

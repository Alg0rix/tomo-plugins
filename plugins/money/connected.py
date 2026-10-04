"""Connected-app HTTP surface; all work stays in the authenticated user's DB."""

from __future__ import annotations

from functools import wraps

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from app.core.secrets import encrypt_secret
from . import gmail, google_apps, jobs, ledger, store


def snapshot(conn):
    accounts = gmail.accounts(conn)
    for account in accounts:
        account.pop("access_token", None)
        account.pop("refresh_token", None)
        account["settings"] = jobs.settings(conn, "gmail", account["email"])
    connections = google_apps.connections(conn)
    for provider, connection in connections.items():
        connection["settings"] = jobs.settings(conn, provider)
    return {
        "configured": gmail.configured(conn),
        "accounts": accounts,
        "connections": connections,
        "jobs": jobs.recent(conn),
        "bills": google_apps.bills(conn),
    }


def queue(conn, provider, fields=None):
    fields = fields or {}
    if provider == "gmail":
        accounts = gmail.accounts(conn)
        email = str(fields.get("email") or "")
        if email:
            accounts = [a for a in accounts if a["email"] == email]
        if not accounts:
            raise ledger.ValidationError("Connect a Gmail account first")
        result = []
        for account in accounts:
            lookback = (
                fields.get("lookback")
                or jobs.settings(conn, "gmail", account["email"])["lookback"]
            )
            result.append(
                jobs.public(
                    jobs.enqueue(
                        conn, "gmail", account["email"], {"lookback": lookback}
                    )
                )
            )
        return {"jobs": result}
    google_apps.provider_check(provider)
    connection = google_apps.connections(conn).get(provider)
    if not connection or not connection["target"]:
        raise ledger.ValidationError("Connect the app and save a destination first")
    return {"jobs": [jobs.public(jobs.enqueue(conn, provider))]}


def register(api, db, uid, guard, render):
    def errors(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except ledger.ValidationError as exc:
                raise HTTPException(422, str(exc)) from None
            except gmail.GmailError as exc:
                raise HTTPException(502, str(exc)) from None

        return wrapped

    async def fields(request):
        guard(request)
        if request.headers.get("content-type", "").startswith("application/json"):
            try:
                body = await request.json()
            except ValueError:
                raise HTTPException(422, "Invalid JSON") from None
            if not isinstance(body, dict):
                raise HTTPException(422, "JSON body must be an object")
            return body
        return dict(await request.form())

    def automatic(body):
        value = body.get("automatic", False)
        if type(value) is bool:
            return value
        if value in ("on", "off"):
            return value == "on"
        raise ledger.ValidationError("automatic must be a boolean")

    @api.router.get("/apps")
    def apps(request: Request):
        with db(uid(request)) as conn:
            return render(
                request,
                "apps.html",
                apps=snapshot(conn),
                oauth_base=str(request.base_url).rstrip("/") + api.base_url,
            )

    @api.router.get("/api/apps")
    def state(request: Request):
        with db(uid(request)) as conn:
            return snapshot(conn)

    @api.router.post("/apps/gmail/credentials")
    async def credentials(request: Request):
        body = await fields(request)
        client, secret = (
            str(body.get("client_id") or "").strip(),
            str(body.get("client_secret") or "").strip(),
        )
        if not client or not secret or len(client) > 500 or len(secret) > 500:
            raise HTTPException(422, "Valid client ID and secret are required")
        with db(uid(request)) as conn:
            # Changing an OAuth client while connected invalidates refresh credentials.
            if gmail.accounts(conn) or google_apps.connections(conn):
                raise HTTPException(
                    422, "Disconnect apps before replacing OAuth credentials"
                )
            store.set_setting(conn, "gmail_client_id", client)
            store.set_setting(conn, "gmail_client_secret", encrypt_secret(secret))
        return RedirectResponse(api.base_url + "/apps", status_code=303)

    @api.router.get("/apps/{provider}/connect")
    @errors
    def connect(request: Request, provider: str):
        with db(uid(request)) as conn:
            redirect = (
                str(request.base_url).rstrip("/")
                + api.base_url
                + "/apps/"
                + provider
                + "/callback"
            )
            url = (
                gmail.begin_oauth(conn, redirect)
                if provider == "gmail"
                else google_apps.begin_oauth(conn, provider, redirect)
            )
        return RedirectResponse(url, status_code=302)

    @api.router.get("/apps/{provider}/callback")
    @errors
    def callback(
        request: Request,
        provider: str,
        code: str = "",
        state: str = "",
        error: str = "",
    ):
        if error:
            raise HTTPException(
                400,
                "Google authorization was declined. Return to Connected apps to try again.",
            )
        with db(uid(request)) as conn:
            if provider == "gmail":
                gmail.finish_oauth(conn, code, state)
            else:
                google_apps.finish_oauth(conn, provider, code, state)
        return RedirectResponse(api.base_url + "/apps", status_code=303)

    @errors
    def mutate(request, provider, action, body):
        with db(uid(request)) as conn:
            if action == "sync":
                return JSONResponse(queue(conn, provider, body), status_code=202)
            if action == "disconnect":
                if provider == "gmail":
                    email = str(body.get("email") or "")
                    if not email:
                        raise ledger.ValidationError(
                            "Choose the Gmail account to disconnect"
                        )
                    return gmail.disconnect(conn, email)
                return google_apps.disconnect(conn, provider)
            if provider == "gmail":
                email = str(body.get("email") or "")
                if not any(a["email"] == email for a in gmail.accounts(conn)):
                    raise ledger.ValidationError("Gmail account not connected")
                return jobs.configure(
                    conn, provider, email, automatic(body), body.get("lookback", "7d")
                )
            return google_apps.configure(
                conn, provider, body.get("target"), automatic(body)
            )

    @api.router.post("/apps/{provider}/sync")
    async def sync(request: Request, provider: str):
        return mutate(request, provider, "sync", await fields(request))

    @api.router.post("/apps/{provider}/settings")
    async def settings(request: Request, provider: str):
        return mutate(request, provider, "settings", await fields(request))

    @api.router.post("/apps/{provider}/disconnect")
    async def disconnect(request: Request, provider: str):
        return mutate(request, provider, "disconnect", await fields(request))

    @api.router.post("/apps/jobs/{job_id}/{action}")
    async def job_action(request: Request, job_id: str, action: str):
        await fields(request)
        return change_job(request, job_id, action)

    @errors
    def change_job(request, job_id, action):
        with db(uid(request)) as conn:
            if action == "cancel":
                return jobs.cancel(conn, job_id)
            if action == "retry":
                old = jobs.get(conn, job_id)
                if old["status"] not in ("failed", "cancelled"):
                    raise ledger.ValidationError(
                        "Only failed or cancelled jobs can be retried"
                    )
                return queue(
                    conn,
                    old["provider"],
                    {
                        "email": old["account"],
                        "lookback": old["options"].get("lookback", "7d"),
                    },
                )
            raise ledger.ValidationError("Unknown job action")

    @api.router.post("/apps/calendar/bills")
    async def save_bill(request: Request):
        return bill_change(request, await fields(request))

    @api.router.post("/apps/calendar/bills/{bill_id}/delete")
    async def delete_bill(request: Request, bill_id: str):
        await fields(request)
        return bill_change(request, {}, bill_id)

    @errors
    def bill_change(request, body, bill_id=None):
        with db(uid(request)) as conn:
            # Replace in-flight snapshots so edits/deletes cannot be lost behind a cursor.
            result = (
                google_apps.delete_bill(conn, bill_id)
                if bill_id
                else google_apps.save_bill(conn, body)
            )
            connection = google_apps.connections(conn).get("calendar")
            if connection and connection["target"]:
                for job in store.rows(
                    conn,
                    "SELECT id FROM sync_jobs WHERE provider='calendar' AND status IN ('queued','running','retry')",
                ):
                    jobs.cancel(conn, job["id"])
                jobs.enqueue(conn, "calendar")
            return result

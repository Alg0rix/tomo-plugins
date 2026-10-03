"""Money — personal finance pages and a complete agent toolset on Tomo's core."""

from contextlib import contextmanager
from datetime import date
from urllib.parse import urlsplit
import json

from fastapi import Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

from app.core.deps import session_user_id
from app.core.secrets import encrypt_secret

from . import capture, gmail, ledger, store


def setup(api):
    @contextmanager
    def db(user_id=None):
        conn = store.connect(api.user_data_dir(user_id))
        try:
            yield conn
        finally:
            conn.close()

    def uid(request: Request) -> str:
        return session_user_id(request)

    def _guard_origin(request: Request) -> None:
        origin = request.headers.get("origin")
        if request.headers.get("sec-fetch-site") == "cross-site" or (
            origin and urlsplit(origin).netloc != request.headers.get("host")
        ):
            raise HTTPException(403, "Cross-origin submission forbidden")

    def _redirect(request: Request, path: str):
        if request.headers.get("accept", "").find("application/json") >= 0:
            return {"ok": True}
        return RedirectResponse(api.base_url + path, status_code=303)

    def _error(exc: Exception):
        if isinstance(exc, ledger.ValidationError):
            raise HTTPException(422, str(exc)) from None
        if isinstance(exc, gmail.GmailError):
            raise HTTPException(502, str(exc)) from None
        raise exc

    async def _json_body(request: Request) -> dict:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            raise HTTPException(422, "Invalid JSON body") from None
        if not isinstance(body, dict):
            raise HTTPException(422, "JSON body must be an object")
        return body

    def _fmt(minor) -> str:
        if minor is None:
            return "—"
        sign = "-" if minor < 0 else ""
        whole = abs(minor) / 100
        text = f"{whole:,.0f}" if abs(minor) % 100 == 0 else f"{whole:,.2f}"
        return f"{sign}Rp{text}"

    def _render(request: Request, template: str, **context):
        with db(uid(request)) as conn:
            context.setdefault("categories", ledger.list_categories(conn))
            context.setdefault("sources", ledger.list_sources(conn))
            context.setdefault(
                "inbox_count",
                store.one(
                    conn, "SELECT COUNT(*) AS n FROM inbox WHERE status='pending'"
                )["n"],
            )
        return api.render(request, template, fmt=_fmt, active=template, **context)

    # ------------------------------------------------------------------ pages
    api.page("/", "Overview")
    api.page("/transactions", "Transactions")
    api.page("/reports", "Reports")
    api.page("/budgets", "Budgets")
    api.page("/sources", "Spending sources")
    api.page("/inbox", "Inbox")
    api.page("/imports", "Imports")
    api.page("/apps", "Connected apps")

    @api.router.get("/")
    def overview(request: Request):
        with db(uid(request)) as conn:
            return _render(request, "overview.html", data=ledger.overview(conn))

    @api.router.get("/transactions")
    def transactions(request: Request):
        with db(uid(request)) as conn:
            return _render(
                request,
                "transactions.html",
                transactions=ledger.list_transactions(conn, limit=300),
                categories=ledger.list_categories(conn),
                sources=ledger.list_sources(conn),
                today=date.today().isoformat(),
            )

    @api.router.get("/reports")
    def reports(request: Request):
        with db(uid(request)) as conn:
            return _render(
                request,
                "reports.html",
                data=ledger.report(conn),
                api_url=api.base_url + "/api/reports",
            )

    @api.router.get("/budgets")
    def budgets(request: Request):
        with db(uid(request)) as conn:
            return _render(
                request,
                "budgets.html",
                budgets=ledger.list_budgets(conn),
                categories=ledger.list_categories(conn),
                month=date.today().isoformat()[:7],
            )

    @api.router.get("/sources")
    def sources(request: Request):
        with db(uid(request)) as conn:
            return _render(request, "sources.html", sources=ledger.list_sources(conn))

    @api.router.get("/inbox")
    def inbox(request: Request):
        with db(uid(request)) as conn:
            return _render(request, "inbox.html", pending=ledger.list_inbox(conn))

    @api.router.get("/imports")
    def imports(request: Request):
        with db(uid(request)) as conn:
            recent = [
                item
                for item in ledger.list_inbox(conn, "")
                if item["origin"] in {"csv", "receipt", "gmail"}
            ][:10]
            return _render(request, "imports.html", imported=recent)

    @api.router.get("/apps")
    def apps(request: Request):
        with db(uid(request)) as conn:
            accounts = gmail.accounts(conn)
            return _render(
                request,
                "apps.html",
                gmail={
                    "connected": bool(accounts),
                    "accounts": accounts,
                    "has_credentials": gmail.configured(conn),
                    "redirect_uri": str(request.base_url).rstrip("/")
                    + api.base_url
                    + "/apps/gmail/callback",
                },
            )

    # ------------------------------------------------------------- ledger API
    @api.router.get("/api/transactions")
    def api_transactions(
        request: Request,
        kind: str = "",
        category: str = "",
        source_id: str = "",
        q: str = "",
        month: str = "",
        start: str = "",
        end: str = "",
        limit: int = 300,
        offset: int = 0,
    ):
        with db(uid(request)) as conn:
            return {
                "transactions": ledger.list_transactions(
                    conn,
                    kind=kind,
                    category=category,
                    source_id=source_id or None,
                    q=q,
                    month=month,
                    start=start,
                    end=end,
                    limit=limit,
                    offset=offset,
                )
            }

    @api.router.get("/api/reports")
    def api_reports(request: Request, month: str = "", year: str = ""):
        with db(uid(request)) as conn:
            try:
                return ledger.report(conn, month=month, year=year)
            except ledger.ValidationError as exc:
                _error(exc)

    @api.router.post("/transactions")
    def save_transaction(
        request: Request,
        kind: str = Form(),
        amount: str = Form(),
        category: str = Form(""),
        note: str = Form(""),
        day: str = Form(""),
        source_id: str = Form(""),
        currency: str = Form("IDR"),
    ):
        _guard_origin(request)
        with db(uid(request)) as conn:
            try:
                ledger.add_transaction(
                    conn,
                    {
                        "kind": kind,
                        "amount": amount,
                        "category": category,
                        "note": note,
                        "day": day,
                        "source_id": source_id,
                        "currency": currency,
                    },
                )
            except (ledger.ValidationError, ValueError) as exc:
                _error(exc if isinstance(exc, ledger.ValidationError)
                       else ledger.ValidationError(
                           "Provide a positive amount, a category, and a valid date"))
        return _redirect(request, "/transactions")

    @api.router.post("/api/transactions")
    async def api_add_transaction(request: Request):
        _guard_origin(request)
        body = await _json_body(request)
        with db(uid(request)) as conn:
            try:
                return ledger.add_transaction(conn, body)
            except ledger.ValidationError as exc:
                _error(exc)

    @api.router.patch("/api/transactions/{txn_id}")
    async def api_update_transaction(request: Request, txn_id: int):
        _guard_origin(request)
        body = await _json_body(request)
        with db(uid(request)) as conn:
            try:
                return ledger.update_transaction(conn, txn_id, body)
            except ledger.ValidationError as exc:
                _error(exc)

    @api.router.delete("/api/transactions/{txn_id}")
    def api_delete_transaction(request: Request, txn_id: int):
        _guard_origin(request)
        with db(uid(request)) as conn:
            try:
                return ledger.delete_transaction(conn, txn_id)
            except ledger.ValidationError as exc:
                _error(exc)

    @api.router.get("/api/overview")
    def api_overview(request: Request):
        with db(uid(request)) as conn:
            return ledger.overview(conn)

    # ---------------------------------------------------------------- budgets
    @api.router.post("/budgets")
    async def save_budget(request: Request):
        _guard_origin(request)
        if request.headers.get("content-type", "").startswith("application/json"):
            fields = await _json_body(request)
        else:
            form = await request.form()
            fields = dict(form)
        with db(uid(request)) as conn:
            try:
                result = ledger.set_budget(conn, fields)
            except ledger.ValidationError as exc:
                _error(exc)
        if request.headers.get("accept", "").find("application/json") >= 0:
            return result
        return _redirect(request, "/budgets")

    @api.router.post("/budgets/{budget_id}/delete")
    def delete_budget(request: Request, budget_id: int):
        _guard_origin(request)
        with db(uid(request)) as conn:
            try:
                ledger.delete_budget(conn, budget_id)
            except ledger.ValidationError as exc:
                _error(exc)
        return _redirect(request, "/budgets")

    @api.router.get("/api/budgets")
    def api_budgets(request: Request, month: str = ""):
        with db(uid(request)) as conn:
            return {"budgets": ledger.list_budgets(conn, month)}

    # ---------------------------------------------------------------- sources
    @api.router.post("/sources")
    async def save_source(request: Request):
        _guard_origin(request)
        if request.headers.get("content-type", "").startswith("application/json"):
            fields = await _json_body(request)
        else:
            form = await request.form()
            fields = dict(form)
        with db(uid(request)) as conn:
            try:
                result = ledger.add_source(conn, fields)
            except ledger.ValidationError as exc:
                _error(exc)
        if request.headers.get("accept", "").find("application/json") >= 0:
            return result
        return _redirect(request, "/sources")

    @api.router.post("/sources/{source_id}")
    async def edit_source(request: Request, source_id: int):
        _guard_origin(request)
        if request.headers.get("content-type", "").startswith("application/json"):
            fields = await _json_body(request)
        else:
            form = await request.form()
            fields = dict(form)
        with db(uid(request)) as conn:
            try:
                result = ledger.update_source(conn, source_id, fields)
            except ledger.ValidationError as exc:
                _error(exc)
        if request.headers.get("accept", "").find("application/json") >= 0:
            return result
        return _redirect(request, "/sources")

    @api.router.post("/sources/{source_id}/delete")
    def delete_source(request: Request, source_id: int):
        _guard_origin(request)
        with db(uid(request)) as conn:
            try:
                ledger.update_source(conn, source_id, {"archived": True})
            except ledger.ValidationError as exc:
                _error(exc)
        return _redirect(request, "/sources")

    @api.router.get("/api/sources")
    def api_sources(request: Request):
        with db(uid(request)) as conn:
            return {"sources": ledger.list_sources(conn)}

    # ------------------------------------------------------------------ inbox
    @api.router.post("/inbox/{item_id}/confirm")
    async def inbox_confirm(request: Request, item_id: int):
        _guard_origin(request)
        overrides = {}
        if request.headers.get("content-type", "").startswith("application/json"):
            overrides = await _json_body(request)
        else:
            form = await request.form()
            overrides = {k: v for k, v in form.items() if v not in (None, "")}
        with db(uid(request)) as conn:
            try:
                result = ledger.confirm_candidate(conn, item_id, overrides)
            except ledger.ValidationError as exc:
                _error(exc)
        if request.headers.get("accept", "").find("application/json") >= 0:
            return result
        return _redirect(request, "/inbox")

    @api.router.post("/inbox/{item_id}/dismiss")
    def inbox_dismiss(request: Request, item_id: int):
        _guard_origin(request)
        with db(uid(request)) as conn:
            try:
                result = ledger.dismiss_candidate(conn, item_id)
            except ledger.ValidationError as exc:
                _error(exc)
        if request.headers.get("accept", "").find("application/json") >= 0:
            return result
        return _redirect(request, "/inbox")

    @api.router.post("/inbox/confirm-all")
    def inbox_confirm_all(request: Request):
        _guard_origin(request)
        confirmed = failed = 0
        with db(uid(request)) as conn:
            for item in ledger.list_inbox(conn):
                try:
                    ledger.confirm_candidate(conn, item["id"])
                    confirmed += 1
                except ledger.ValidationError:
                    failed += 1
        return {"confirmed": confirmed, "failed": failed}

    @api.router.get("/inbox/{item_id}/image")
    def inbox_image(request: Request, item_id: int):
        with db(uid(request)) as conn:
            row = store.one(conn, "SELECT provenance FROM inbox WHERE id=?", (item_id,))
            if not row:
                raise HTTPException(404, "Not found")
            provenance = json.loads(row["provenance"] or "{}")
            rel = provenance.get("image")
            if not rel:
                raise HTTPException(404, "No image")
            path = (api.user_data_dir(uid(request)) / rel).resolve()
            if not path.is_file() or api.user_data_dir(uid(request)).resolve() not in path.parents:
                raise HTTPException(404, "No image")
            return FileResponse(path)

    # ---------------------------------------------------------------- imports
    @api.router.post("/imports/csv")
    async def import_csv(request: Request, file: UploadFile):
        _guard_origin(request)
        raw = await file.read()
        if len(raw) > 8 * 1024 * 1024:
            raise HTTPException(413, "CSV too large (max 8 MB)")
        text = raw.decode("utf-8-sig", "replace")
        with db(uid(request)) as conn:
            try:
                result = capture.import_csv(conn, text)
            except ledger.ValidationError as exc:
                _error(exc)
        return result

    @api.router.post("/imports/receipt")
    async def import_receipt(request: Request, file: UploadFile):
        _guard_origin(request)
        raw = await file.read()
        if len(raw) > 12 * 1024 * 1024:
            raise HTTPException(413, "Image too large (max 12 MB)")
        data_url = capture.image_data_url(raw, file.content_type or "")
        if not data_url:
            raise HTTPException(422, "Only image files are supported")
        inbox_dir = api.user_data_dir(uid(request)) / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        import uuid

        ext = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(
            (file.content_type or "").split(";")[0], ".jpg"
        )
        name = f"{uuid.uuid4().hex}{ext}"
        (inbox_dir / name).write_bytes(raw)
        with db(uid(request)) as conn:
            cats = [c["name"] for c in ledger.list_categories(conn)]
            fields = capture.receipt_to_candidate(data_url, cats)
            if not fields:
                fields = {
                    "kind": "expense",
                    "category": "Uncategorized",
                    "note": file.filename or "Receipt",
                    "merchant": file.filename or "Receipt",
                    "day": date.today().isoformat(),
                }
            result = ledger.add_candidate(
                conn,
                "receipt",
                fields,
                {"image": f"inbox/{name}", "filename": file.filename or ""},
            )
        return {**result, "parsed": bool(fields.get("amount"))}

    # ------------------------------------------------------------- gmail apps
    @api.router.post("/apps/gmail/credentials")
    async def gmail_credentials(request: Request):
        _guard_origin(request)
        if request.headers.get("content-type", "").startswith("application/json"):
            fields = await _json_body(request)
        else:
            form = await request.form()
            fields = dict(form)
        client_id = str(fields.get("client_id") or "").strip()
        secret = str(fields.get("client_secret") or "").strip()
        if not client_id or not secret:
            raise HTTPException(422, "Both client ID and client secret are required")
        with db(uid(request)) as conn:
            store.set_setting(conn, "gmail_client_id", client_id)
            store.set_setting(conn, "gmail_client_secret", encrypt_secret(secret))
        return _redirect(request, "/apps")

    @api.router.get("/apps/gmail/connect")
    def gmail_connect(request: Request):
        with db(uid(request)) as conn:
            try:
                redirect_uri = (
                    str(request.base_url).rstrip("/")
                    + api.base_url
                    + "/apps/gmail/callback"
                )
                url = gmail.begin_oauth(conn, redirect_uri)
            except gmail.GmailError as exc:
                _error(exc)
        return RedirectResponse(url, status_code=302)

    @api.router.get("/apps/gmail/callback")
    def gmail_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        if error:
            raise HTTPException(400, f"Google sign-in failed: {error}")
        with db(uid(request)) as conn:
            try:
                gmail.finish_oauth(conn, code, state)
            except gmail.GmailError as exc:
                _error(exc)
        return RedirectResponse(api.base_url + "/apps", status_code=303)

    @api.router.post("/apps/gmail/disconnect")
    async def gmail_disconnect(request: Request):
        _guard_origin(request)
        email = ""
        if request.headers.get("content-type", "").startswith("application/json"):
            email = str((await _json_body(request)).get("email") or "")
        else:
            email = str((await request.form()).get("email") or "")
        with db(uid(request)) as conn:
            result = gmail.disconnect(conn, email or None)
        if request.headers.get("accept", "").find("application/json") >= 0:
            return result
        return _redirect(request, "/apps")

    @api.router.post("/apps/gmail/sync")
    def gmail_sync(request: Request):
        _guard_origin(request)
        with db(uid(request)) as conn:
            try:
                return gmail.sync(conn)
            except gmail.GmailError as exc:
                _error(exc)

    @api.router.get("/api/apps")
    def api_apps(request: Request):
        with db(uid(request)) as conn:
            return {
                "configured": gmail.configured(conn),
                "accounts": gmail.accounts(conn),
            }

    # ----------------------------------------------------------------- tools
    def _tool(name, description, parameters, handler):
        api.tool(name, description, parameters, handler)

    def _with_conn(fn):
        def run(arguments):
            with db() as conn:
                try:
                    return fn(conn, arguments or {})
                except ledger.ValidationError as exc:
                    return f"Error: {exc}"
                except gmail.GmailError as exc:
                    return f"Error: {exc}"

        return run

    _tool(
        "add_transaction",
        "Record income or an expense in the user's money ledger. Amount is a "
        "decimal in the transaction currency (IDR by default). Optional: "
        "category, note, day (YYYY-MM-DD), currency, source (name) or source_id.",
        {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["income", "expense"]},
                "amount": {"type": "string", "description": "Decimal amount, e.g. '25000' or '12.50'"},
                "category": {"type": "string"},
                "note": {"type": "string"},
                "day": {"type": "string", "description": "YYYY-MM-DD"},
                "currency": {"type": "string", "description": "ISO code, default IDR"},
                "source": {"type": "string", "description": "Spending source name"},
                "source_id": {"type": "integer"},
            },
            "required": ["kind", "amount"],
        },
        _with_conn(
            lambda conn, a: ledger.add_transaction(conn, _resolve_source_arg(conn, a))
        ),
    )

    def _resolve_source_arg(conn, arguments):
        arguments = dict(arguments)
        name = str(arguments.pop("source", "") or "").strip()
        if name and "source_id" not in arguments:
            match = next(
                (s for s in ledger.list_sources(conn)
                 if s["name"].lower() == name.lower()),
                None,
            )
            if match:
                arguments["source_id"] = match["id"]
        return arguments

    _tool(
        "update_transaction",
        "Update fields of an existing transaction by id. Any of kind, amount, "
        "category, note, day, currency, source/source_id may change.",
        {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "kind": {"type": "string", "enum": ["income", "expense"]},
                "amount": {"type": "string"},
                "category": {"type": "string"},
                "note": {"type": "string"},
                "day": {"type": "string"},
                "currency": {"type": "string"},
                "source": {"type": "string"},
                "source_id": {"type": "integer"},
            },
            "required": ["id"],
        },
        _with_conn(
            lambda conn, a: ledger.update_transaction(
                conn, int(a.get("id") or 0), _resolve_source_arg(conn, a)
            )
        ),
    )
    _tool(
        "delete_transaction",
        "Delete a transaction by id.",
        {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
        _with_conn(lambda conn, a: ledger.delete_transaction(conn, int(a.get("id") or 0))),
    )
    _tool(
        "list_transactions",
        "List the user's money transactions (amounts are minor units, /100). "
        "Optional filters: kind, category, source_id, q (note/category text), "
        "month (YYYY-MM), start/end day, limit, offset.",
        {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["income", "expense"]},
                "category": {"type": "string"},
                "source_id": {"type": "integer"},
                "q": {"type": "string"},
                "month": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "limit": {"type": "integer"},
                "offset": {"type": "integer"},
            },
        },
        _with_conn(
            lambda conn, a: ledger.list_transactions(
                conn,
                kind=a.get("kind", ""),
                category=a.get("category", ""),
                source_id=a.get("source_id"),
                q=a.get("q", ""),
                month=a.get("month", ""),
                start=a.get("start", ""),
                end=a.get("end", ""),
                limit=a.get("limit") or 200,
                offset=a.get("offset") or 0,
            )
        ),
    )
    _tool(
        "summary",
        "Money summary: this month's income/expense, all-time balance, and "
        "transaction count (minor units, /100; currency IDR).",
        {"type": "object", "properties": {}},
        _with_conn(lambda conn, a: ledger.summary(conn)),
    )
    _tool(
        "report",
        "Full spending report for a month (YYYY-MM) or year (YYYY): totals, "
        "previous-period comparison, per-category breakdown with shares, daily "
        "series, cumulative trend, and budget status.",
        {
            "type": "object",
            "properties": {
                "month": {"type": "string", "description": "YYYY-MM"},
                "year": {"type": "string", "description": "YYYY"},
            },
        },
        _with_conn(
            lambda conn, a: ledger.report(
                conn, month=a.get("month", ""), year=a.get("year", "")
            )
        ),
    )
    _tool(
        "list_categories",
        "List spending/income categories with icons and colors.",
        {"type": "object", "properties": {}},
        _with_conn(lambda conn, a: ledger.list_categories(conn)),
    )
    _tool(
        "add_category",
        "Create a category. kind: expense, income, or any. Optional emoji icon "
        "and hex color.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "kind": {"type": "string", "enum": ["expense", "income", "any"]},
                "icon": {"type": "string"},
                "color": {"type": "string"},
            },
            "required": ["name"],
        },
        _with_conn(lambda conn, a: ledger.add_category(conn, a)),
    )
    _tool(
        "list_sources",
        "List spending sources (cash, bank, e-wallet, card) with computed "
        "balances in minor units.",
        {"type": "object", "properties": {}},
        _with_conn(lambda conn, a: ledger.list_sources(conn)),
    )
    _tool(
        "add_source",
        "Create a spending source/account: name; type cash|bank|ewallet|card|"
        "savings|investment; optional opening_balance, currency, icon, color.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "type": {"type": "string"},
                "opening_balance": {"type": "string"},
                "currency": {"type": "string"},
                "icon": {"type": "string"},
                "color": {"type": "string"},
            },
            "required": ["name"],
        },
        _with_conn(lambda conn, a: ledger.add_source(conn, a)),
    )
    _tool(
        "set_budget",
        "Set or update a spending budget for a category. amount in major units. "
        "month YYYY-MM scopes it to one month; omit for a recurring monthly "
        "budget. alert_pct optional (default 80).",
        {
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "amount": {"type": "string"},
                "month": {"type": "string"},
                "alert_pct": {"type": "integer"},
            },
            "required": ["category", "amount"],
        },
        _with_conn(lambda conn, a: ledger.set_budget(conn, a)),
    )
    _tool(
        "list_budgets",
        "List budgets with current-month spend, remaining, and percent used.",
        {
            "type": "object",
            "properties": {"month": {"type": "string", "description": "YYYY-MM"}},
        },
        _with_conn(lambda conn, a: ledger.list_budgets(conn, a.get("month", ""))),
    )
    _tool(
        "delete_budget",
        "Delete a budget by id.",
        {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
        _with_conn(lambda conn, a: ledger.delete_budget(conn, int(a.get("id") or 0))),
    )
    _tool(
        "list_inbox",
        "List pending captured items (from Gmail, receipts, CSV) awaiting "
        "review, with proposed transaction fields and provenance.",
        {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["pending", "confirmed", "dismissed"]}
            },
        },
        _with_conn(lambda conn, a: ledger.list_inbox(conn, a.get("status") or "pending")),
    )
    _tool(
        "confirm_inbox",
        "Confirm an inbox item into the ledger, optionally overriding fields "
        "(amount, category, day, kind, note, source).",
        {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "kind": {"type": "string"},
                "amount": {"type": "string"},
                "category": {"type": "string"},
                "day": {"type": "string"},
                "note": {"type": "string"},
                "source": {"type": "string"},
            },
            "required": ["id"],
        },
        _with_conn(
            lambda conn, a: ledger.confirm_candidate(
                conn,
                int(a.get("id") or 0),
                {k: v for k, v in _resolve_source_arg(conn, a).items()
                 if k != "id" and v not in (None, "")},
            )
        ),
    )
    _tool(
        "dismiss_inbox",
        "Dismiss a pending inbox item without recording it.",
        {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
        _with_conn(lambda conn, a: ledger.dismiss_candidate(conn, int(a.get("id") or 0))),
    )
    _tool(
        "gmail_status",
        "Show Gmail connection state: whether OAuth credentials are configured "
        "and which accounts are linked, with last sync times.",
        {"type": "object", "properties": {}},
        _with_conn(
            lambda conn, a: {
                "configured": gmail.configured(conn),
                "accounts": gmail.accounts(conn),
            }
        ),
    )
    _tool(
        "sync_gmail",
        "Sync Gmail now: scan the mailbox for receipt/payment emails and queue "
        "parsed transactions into the Money inbox for review.",
        {"type": "object", "properties": {"email": {"type": "string"}}},
        _with_conn(lambda conn, a: gmail.sync(conn, a.get("email") or None)),
    )

    def _scan_receipt(conn, arguments):

        source = str(arguments.get("source") or "").strip()
        if not source:
            return "Error: source is required (workplace path, attachment:<id>, or URL)"
        try:
            from app.runtime.tools.vision_analyze import _resolve_source

            raw, mime, error = _resolve_source(source)
        except ImportError:
            return "Error: image sources are unavailable in this Tomo version"
        if error:
            return error
        data_url = capture.image_data_url(raw or b"", mime or "")
        if not data_url:
            return "Error: could not read the image"
        cats = [c["name"] for c in ledger.list_categories(conn)]
        fields = capture.receipt_to_candidate(data_url, cats)
        if not fields:
            return "Error: could not extract a transaction from this image"
        if arguments.get("save", True):
            saved = ledger.add_transaction(conn, fields)
            return {"saved": True, "transaction_id": saved["id"], **fields}
        result = ledger.add_candidate(conn, "receipt", fields, {"via": "agent"})
        return {"inbox_id": result["id"], **fields}

    _tool(
        "scan_receipt",
        "Read a receipt/payment screenshot and record it. source: a workplace "
        "file path, attachment:<id>, or https URL. Set save=false to queue it "
        "in the inbox for review instead of recording immediately.",
        {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "save": {"type": "boolean", "default": True},
            },
            "required": ["source"],
        },
        _with_conn(_scan_receipt),
    )
    _tool(
        "ask_extract",
        "Parse free text (e.g. a pasted receipt, bank SMS, or chat message) "
        "into a transaction and save it. Uses the configured AI model; falls "
        "back to amount heuristics. Confirm details with the user first.",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Raw text to parse"},
                "save": {"type": "boolean", "default": True},
            },
            "required": ["text"],
        },
        _with_conn(
            lambda conn, a: _extract_and_save(
                conn, str(a.get("text") or ""), bool(a.get("save", True))
            )
        ),
    )

    def _extract_and_save(conn, text, save):
        from . import ai

        cats = [c["name"] for c in ledger.list_categories(conn)]
        fields = ai.extract_transaction(text, cats) if text.strip() else None
        if not fields:
            found = capture.extract_amount(text)
            if not found:
                return "Error: could not find an amount in that text"
            minor, currency = found
            fields = {
                "kind": "expense",
                "amount": f"{minor / 100:.2f}",
                "currency": currency,
                "category": "Uncategorized",
                "note": text.strip()[:200],
                "day": date.today().isoformat(),
            }
        if save:
            saved = ledger.add_transaction(conn, fields)
            return {"saved": True, "transaction_id": saved["id"], **fields}
        result = ledger.add_candidate(conn, "text", fields, {"via": "agent"})
        return {"inbox_id": result["id"], **fields}

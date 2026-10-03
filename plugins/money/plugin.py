"""Money pages and user-scoped ledger tools using Tomo's agent core."""

from datetime import date
from decimal import Decimal
import sqlite3

from fastapi import Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, field_validator

from app.core.deps import session_user_id


class Transaction(BaseModel):
    kind: str
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    category: str = Field(min_length=1, max_length=80)
    note: str = Field(default="", max_length=500)
    day: date = Field(default_factory=date.today)

    @field_validator("kind")
    @classmethod
    def check_kind(cls, value):
        if value not in {"income", "expense"}:
            raise ValueError("kind must be income or expense")
        return value


def setup(api):
    def connection(user_id=None):
        conn = sqlite3.connect(api.user_data_dir(user_id) / "ledger.db")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE IF NOT EXISTS transactions (id INTEGER PRIMARY KEY, kind TEXT, amount_minor INTEGER, category TEXT, note TEXT, day TEXT)"
        )
        conn.commit()
        return conn

    def ledger(user_id=None):
        conn = connection(user_id)
        try:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM transactions ORDER BY day DESC, id DESC"
                )
            ]
        finally:
            conn.close()

    def add(arguments, user_id=None):
        entry = Transaction.model_validate(arguments)
        conn = connection(user_id)
        try:
            cursor = conn.execute(
                "INSERT INTO transactions (kind, amount_minor, category, note, day) VALUES (?,?,?,?,?)",
                (
                    entry.kind,
                    int(entry.amount * 100),
                    entry.category,
                    entry.note,
                    entry.day.isoformat(),
                ),
            )
            conn.commit()
            return {"id": cursor.lastrowid, "saved": True}
        finally:
            conn.close()

    def summary(user_id=None):
        rows = ledger(user_id)
        income = sum(row["amount_minor"] for row in rows if row["kind"] == "income")
        expense = sum(row["amount_minor"] for row in rows if row["kind"] == "expense")
        return {
            "income_minor": income,
            "expense_minor": expense,
            "balance_minor": income - expense,
            "currency": "IDR",
        }

    api.page("/", "Overview")
    api.page("/transactions", "Transactions")

    @api.router.get("/")
    def overview(request: Request):
        return api.render(
            request, "money_overview.html", summary=summary(session_user_id(request))
        )

    @api.router.get("/transactions")
    def transactions(request: Request):
        return api.render(
            request,
            "money_transactions.html",
            transactions=ledger(session_user_id(request)),
            today=date.today().isoformat(),
        )

    @api.router.post("/transactions")
    def save_transaction(
        request: Request,
        kind: str = Form(),
        amount: str = Form(),
        category: str = Form(),
        note: str = Form(""),
        day: str = Form(),
    ):
        from urllib.parse import urlsplit

        origin = request.headers.get("origin")
        if request.headers.get("sec-fetch-site") == "cross-site" or (
            origin and urlsplit(origin).netloc != request.headers.get("host")
        ):
            raise HTTPException(403, "Cross-origin submission forbidden")
        try:
            add(
                {
                    "kind": kind,
                    "amount": amount,
                    "category": category,
                    "note": note,
                    "day": day,
                },
                session_user_id(request),
            )
        except ValueError:
            raise HTTPException(
                422,
                "Provide a positive amount with at most two decimal places, a category, and a valid date",
            ) from None
        return RedirectResponse(api.base_url + "/transactions", status_code=303)

    api.tool(
        "summary",
        "Read the current user's income, expenses, and balance in IDR minor units (divide by 100).",
        {"type": "object", "properties": {}},
        lambda args: summary(),
    )
    api.tool(
        "list_transactions",
        "List the current user's money transactions. Amounts are IDR minor units (divide by 100).",
        {"type": "object", "properties": {}},
        lambda args: ledger(),
    )
    api.tool(
        "add_transaction",
        "Record income or an expense in the current user's money ledger. Amount is in IDR, not minor units. Confirm amount and category with the user when unclear.",
        Transaction.model_json_schema(),
        lambda args: add(args),
    )

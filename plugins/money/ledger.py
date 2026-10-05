"""Money domain logic: ledger, categories, sources, budgets, reports, inbox."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import re
import sqlite3

from . import store

VALID_KINDS = {"income", "expense"}
SOURCE_TYPES = {"cash", "bank", "ewallet", "card", "savings", "investment"}


class ValidationError(ValueError):
    pass


def parse_amount_minor(value) -> int:
    """Parse a positive decimal amount into minor units (×100)."""
    if isinstance(value, bool):
        raise ValidationError("amount must be a positive number")
    try:
        amount = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, AttributeError):
        raise ValidationError("amount must be a positive number") from None
    if amount <= 0:
        raise ValidationError("amount must be greater than zero")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_day(value) -> str:
    if value in (None, ""):
        return date.today().isoformat()
    try:
        return date.fromisoformat(str(value).strip()[:10]).isoformat()
    except ValueError:
        raise ValidationError("day must be a YYYY-MM-DD date") from None


def month_bounds(month: str | None) -> tuple[str, str]:
    """(first_day, day_after_last) for a YYYY-MM month, default current."""
    today = date.today()
    if month:
        try:
            year, mon = int(month[:4]), int(month[5:7])
            first = date(year, mon, 1)
        except (ValueError, IndexError):
            raise ValidationError("month must be YYYY-MM") from None
    else:
        first = today.replace(day=1)
    last_day = monthrange(first.year, first.month)[1]
    return first.isoformat(), (first + timedelta(days=last_day)).isoformat()


def year_bounds(year: str | None) -> tuple[str, str]:
    today = date.today()
    try:
        value = int(year) if year else today.year
    except ValueError:
        raise ValidationError("year must be YYYY") from None
    return f"{value:04d}-01-01", f"{value + 1:04d}-01-01"


def prev_month(month: str) -> str:
    year, mon = int(month[:4]), int(month[5:7])
    mon -= 1
    if mon == 0:
        year, mon = year - 1, 12
    return f"{year:04d}-{mon:02d}"


def _clean_category(conn: sqlite3.Connection, name: str, kind: str) -> str:
    name = (name or "").strip()[:80]
    if not name:
        return "Uncategorized"
    existing = store.one(
        conn, "SELECT name FROM categories WHERE name = ? COLLATE NOCASE", (name,)
    )
    return existing["name"] if existing else name


PAYMENT_METHODS = [
    "cash", "qris", "debit", "credit", "transfer", "e-wallet", "other",
]


def clean_method(value) -> str:
    """Normalize a payment-method label: lowercase, collapsed spaces, ≤40 chars."""
    return re.sub(r"\s+", " ", str(value or "").strip().lower())[:40]


def category_hints(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Recent merchant → category pairs, newest correction first.

    Confirmed inbox rows supply the merchant. A hand-entered note is the
    fallback so a category edit still teaches the next parse.
    """
    rows = store.rows(
        conn,
        """
        SELECT t.category AS category,
               COALESCE(
                 NULLIF(json_extract(i.payload, '$.merchant'), ''),
                 NULLIF(t.note, '')
               ) AS merchant
        FROM transactions t
        LEFT JOIN inbox i
          ON i.status = 'confirmed'
         AND json_extract(i.payload, '$.external_key') = t.external_key
        WHERE t.category != 'Uncategorized'
        ORDER BY t.updated_at DESC
        LIMIT 80
        """,
    )
    seen: set[str] = set()
    hints = []
    for row in rows:
        merchant = str(row["merchant"] or "").strip()[:80]
        category = str(row["category"] or "").strip()[:80]
        key = merchant.casefold()
        if not merchant or not category or key in seen:
            continue
        seen.add(key)
        hints.append({"merchant": merchant, "category": category})
        if len(hints) >= limit:
            break
    return hints


# --- Transactions -----------------------------------------------------------

def add_transaction(conn: sqlite3.Connection, fields: dict) -> dict:
    kind = str(fields.get("kind") or "").strip()
    if kind not in VALID_KINDS:
        raise ValidationError("kind must be income or expense")
    amount_minor = parse_amount_minor(fields.get("amount"))
    category = _clean_category(conn, str(fields.get("category") or ""), kind)
    day = parse_day(fields.get("day"))
    note = str(fields.get("note") or "")[:500]
    currency = (str(fields.get("currency") or "IDR").strip() or "IDR")[:8].upper()
    method = clean_method(fields.get("method"))
    source_id = fields.get("source_id")
    if source_id in ("", 0, "0", "none", "null"):
        source_id = None
    if source_id is not None:
        if not store.one(conn, "SELECT id FROM sources WHERE id=?", (source_id,)):
            raise ValidationError("unknown source_id")
        source_id = int(source_id)
    external_key = (str(fields.get("external_key") or "")[:200]) or None
    if external_key and store.one(
        conn, "SELECT id FROM transactions WHERE external_key=?", (external_key,)
    ):
        existing = store.one(
            conn, "SELECT id FROM transactions WHERE external_key=?", (external_key,)
        )
        return {"id": existing["id"], "saved": True, "duplicate": True}
    cur = conn.execute(
        "INSERT INTO transactions (kind, amount_minor, currency, category, note, day,"
        " source_id, method, external_key) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            kind, amount_minor, currency, category, note, day,
            source_id, method, external_key,
        ),
    )
    conn.commit()
    return {"id": cur.lastrowid, "saved": True, "duplicate": False}


def update_transaction(conn: sqlite3.Connection, txn_id: int, fields: dict) -> dict:
    row = store.one(conn, "SELECT * FROM transactions WHERE id=?", (txn_id,))
    if not row:
        raise ValidationError("transaction not found")
    merged = {**row, **{k: v for k, v in fields.items() if v is not None}}
    merged["amount"] = (
        fields.get("amount")
        if fields.get("amount") is not None
        else Decimal(row["amount_minor"]) / 100
    )
    kind = str(merged.get("kind") or row["kind"])
    if kind not in VALID_KINDS:
        raise ValidationError("kind must be income or expense")
    source_id = merged.get("source_id")
    if source_id in ("", 0, "0", "none", "null"):
        source_id = None
    conn.execute(
        "UPDATE transactions SET kind=?, amount_minor=?, currency=?, category=?,"
        " note=?, day=?, source_id=?, method=?, updated_at=datetime('now') WHERE id=?",
        (
            kind,
            parse_amount_minor(merged["amount"]),
            (str(merged.get("currency") or row["currency"])[:8]).upper(),
            _clean_category(conn, str(merged.get("category") or ""), kind),
            str(merged.get("note") or "")[:500],
            parse_day(merged.get("day")),
            source_id,
            clean_method(merged.get("method")),
            txn_id,
        ),
    )
    conn.commit()
    return {"id": txn_id, "saved": True}


def delete_transaction(conn: sqlite3.Connection, txn_id: int) -> dict:
    cur = conn.execute("DELETE FROM transactions WHERE id=?", (txn_id,))
    conn.commit()
    if not cur.rowcount:
        raise ValidationError("transaction not found")
    return {"id": txn_id, "deleted": True}


def list_transactions(
    conn: sqlite3.Connection,
    *,
    kind: str = "",
    category: str = "",
    source_id=None,
    method: str = "",
    q: str = "",
    month: str = "",
    start: str = "",
    end: str = "",
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    sql = (
        "SELECT t.*, s.name AS source_name, s.icon AS source_icon "
        "FROM transactions t LEFT JOIN sources s ON s.id = t.source_id WHERE 1=1"
    )
    params: list = []
    if kind in VALID_KINDS:
        sql += " AND t.kind=?"
        params.append(kind)
    if category:
        sql += " AND t.category=? COLLATE NOCASE"
        params.append(category.strip())
    if source_id not in (None, ""):
        try:
            params.append(int(source_id))
        except (TypeError, ValueError):
            params.append(-1)
        sql += " AND t.source_id=?"
    if method:
        sql += " AND t.method=?"
        params.append(clean_method(method))
    if q:
        sql += " AND (t.note LIKE ? OR t.category LIKE ?)"
        like = f"%{q.strip()}%"
        params += [like, like]
    if month:
        first, nxt = month_bounds(month)
        sql += " AND t.day>=? AND t.day<?"
        params += [first, nxt]
    else:
        if start:
            sql += " AND t.day>=?"
            params.append(parse_day(start))
        if end:
            sql += " AND t.day<=?"
            params.append(parse_day(end))
    sql += " ORDER BY t.day DESC, t.id DESC LIMIT ? OFFSET ?"
    params += [min(max(int(limit or 200), 1), 1000), max(int(offset or 0), 0)]
    return store.rows(conn, sql, tuple(params))


# --- Categories -------------------------------------------------------------

def list_categories(conn: sqlite3.Connection) -> list[dict]:
    cats = store.rows(conn, "SELECT * FROM categories ORDER BY sort, name")
    used = {
        row["category"]
        for row in conn.execute("SELECT DISTINCT category FROM transactions")
    }
    for cat in cats:
        cat["used"] = cat["name"] in used
    return cats


def add_category(conn: sqlite3.Connection, fields: dict) -> dict:
    name = (str(fields.get("name") or "").strip())[:80]
    if not name:
        raise ValidationError("category name is required")
    kind = str(fields.get("kind") or "expense")
    if kind not in {"expense", "income", "any"}:
        raise ValidationError("kind must be expense, income, or any")
    icon = str(fields.get("icon") or "")[:8]
    color = str(fields.get("color") or "#8b8b9e")[:9]
    if not re.fullmatch(r"#[0-9a-fA-F]{3,8}", color):
        color = "#8b8b9e"
    try:
        cur = conn.execute(
            "INSERT INTO categories (name, kind, icon, color, sort)"
            " VALUES (?,?,?,?, (SELECT COALESCE(MAX(sort),0)+10 FROM categories))",
            (name, kind, icon, color),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise ValidationError("category already exists") from None
    return {"id": cur.lastrowid, "name": name, "saved": True}


def delete_category(conn: sqlite3.Connection, category_id: int) -> dict:
    row = store.one(conn, "SELECT * FROM categories WHERE id=?", (category_id,))
    if not row:
        raise ValidationError("category not found")
    conn.execute("DELETE FROM categories WHERE id=?", (category_id,))
    conn.commit()
    return {"id": category_id, "deleted": True}


# --- Spending sources --------------------------------------------------------

def list_sources(conn: sqlite3.Connection) -> list[dict]:
    sources = store.rows(
        conn, "SELECT * FROM sources WHERE archived=0 ORDER BY name"
    )
    sums = {
        row["source_id"]: row
        for row in store.rows(
            conn,
            "SELECT source_id,"
            " SUM(CASE WHEN kind='income' THEN amount_minor ELSE 0 END) AS income,"
            " SUM(CASE WHEN kind='expense' THEN amount_minor ELSE 0 END) AS expense,"
            " COUNT(*) AS txns"
            " FROM transactions GROUP BY source_id",
        )
    }
    for src in sources:
        agg = sums.get(src["id"]) or {}
        src["income_minor"] = int(agg.get("income") or 0)
        src["expense_minor"] = int(agg.get("expense") or 0)
        src["txns"] = int(agg.get("txns") or 0)
        src["balance_minor"] = (
            src["opening_balance_minor"] + src["income_minor"] - src["expense_minor"]
        )
    unassigned = sums.get(None)
    if unassigned:
        sources.append(
            {
                "id": None,
                "name": "No source",
                "type": "none",
                "icon": "❔",
                "color": "#94a3b8",
                "currency": "IDR",
                "opening_balance_minor": 0,
                "income_minor": int(unassigned.get("income") or 0),
                "expense_minor": int(unassigned.get("expense") or 0),
                "txns": int(unassigned.get("txns") or 0),
                "balance_minor": None,
            }
        )
    return sources


def add_source(conn: sqlite3.Connection, fields: dict) -> dict:
    name = (str(fields.get("name") or "").strip())[:80]
    if not name:
        raise ValidationError("source name is required")
    stype = str(fields.get("type") or "cash")
    if stype not in SOURCE_TYPES:
        stype = "cash"
    opening = fields.get("opening_balance")
    opening_minor = parse_amount_minor(opening) if opening not in (None, "", 0) else 0
    icon = str(fields.get("icon") or "")[:8]
    color = str(fields.get("color") or "#8b8b9e")[:9]
    if not re.fullmatch(r"#[0-9a-fA-F]{3,8}", color):
        color = "#8b8b9e"
    currency = (str(fields.get("currency") or "IDR").strip() or "IDR")[:8].upper()
    try:
        cur = conn.execute(
            "INSERT INTO sources (name, type, currency, opening_balance_minor, icon, color)"
            " VALUES (?,?,?,?,?,?)",
            (name, stype, currency, opening_minor, icon, color),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise ValidationError("source already exists") from None
    return {"id": cur.lastrowid, "name": name, "saved": True}


def update_source(conn: sqlite3.Connection, source_id: int, fields: dict) -> dict:
    row = store.one(conn, "SELECT * FROM sources WHERE id=?", (source_id,))
    if not row:
        raise ValidationError("source not found")
    name = (str(fields.get("name") or row["name"]).strip())[:80]
    stype = str(fields.get("type") or row["type"])
    opening = fields.get("opening_balance")
    opening_minor = (
        parse_amount_minor(opening)
        if opening not in (None, "")
        else row["opening_balance_minor"]
    )
    conn.execute(
        "UPDATE sources SET name=?, type=?, opening_balance_minor=?,"
        " icon=?, color=?, archived=? WHERE id=?",
        (
            name,
            stype if stype in SOURCE_TYPES else row["type"],
            opening_minor,
            str(fields.get("icon") or row["icon"])[:8],
            str(fields.get("color") or row["color"])[:9],
            int(bool(fields.get("archived", row["archived"]))),
            source_id,
        ),
    )
    conn.commit()
    return {"id": source_id, "saved": True}


# --- Budgets -----------------------------------------------------------------

def list_budgets(conn: sqlite3.Connection, month: str = "") -> list[dict]:
    month = month or date.today().isoformat()[:7]
    first, nxt = month_bounds(month)
    budgets = store.rows(
        conn,
        "SELECT * FROM budgets WHERE month IN ('', ?) ORDER BY category",
        (month,),
    )
    spent = {
        row["category"]: row["total"]
        for row in conn.execute(
            "SELECT category, SUM(amount_minor) AS total FROM transactions"
            " WHERE kind='expense' AND day>=? AND day<? GROUP BY category",
            (first, nxt),
        )
    }
    cat_meta = {c["name"]: c for c in list_categories(conn)}
    result = []
    for budget in budgets:
        used = int(spent.get(budget["category"]) or 0)
        meta = cat_meta.get(budget["category"]) or {}
        pct = round(used * 100 / budget["amount_minor"]) if budget["amount_minor"] else 0
        result.append(
            {
                **budget,
                "icon": meta.get("icon", "🏷️"),
                "color": meta.get("color", "#8b8b9e"),
                "spent_minor": used,
                "remaining_minor": budget["amount_minor"] - used,
                "pct": pct,
                "over": used > budget["amount_minor"],
                "alert": pct >= budget["alert_pct"],
            }
        )
    return result


def set_budget(conn: sqlite3.Connection, fields: dict) -> dict:
    category = (str(fields.get("category") or "").strip())[:80]
    if not category:
        raise ValidationError("category is required")
    amount_minor = parse_amount_minor(fields.get("amount"))
    month = str(fields.get("month") or "").strip()
    if month:
        month_bounds(month)  # validates format
    alert = fields.get("alert_pct", 80)
    try:
        alert = min(max(int(alert), 1), 200)
    except (TypeError, ValueError):
        alert = 80
    conn.execute(
        "INSERT INTO budgets (category, month, amount_minor, alert_pct) VALUES (?,?,?,?)"
        " ON CONFLICT(category, month) DO UPDATE SET amount_minor=excluded.amount_minor,"
        " alert_pct=excluded.alert_pct",
        (category, month, amount_minor, alert),
    )
    conn.commit()
    return {"category": category, "month": month, "saved": True}


def delete_budget(conn: sqlite3.Connection, budget_id: int) -> dict:
    cur = conn.execute("DELETE FROM budgets WHERE id=?", (budget_id,))
    conn.commit()
    if not cur.rowcount:
        raise ValidationError("budget not found")
    return {"id": budget_id, "deleted": True}


# --- Reports -----------------------------------------------------------------

def totals(conn: sqlite3.Connection, start: str, end: str) -> dict:
    row = store.one(
        conn,
        "SELECT"
        " COALESCE(SUM(CASE WHEN kind='income' THEN amount_minor ELSE 0 END),0) AS income,"
        " COALESCE(SUM(CASE WHEN kind='expense' THEN amount_minor ELSE 0 END),0) AS expense,"
        " COUNT(*) AS txns"
        " FROM transactions WHERE day>=? AND day<?",
        (start, end),
    )
    income, expense = int(row["income"]), int(row["expense"])
    return {
        "income_minor": income,
        "expense_minor": expense,
        "balance_minor": income - expense,
        "txns": int(row["txns"]),
    }


def by_category(conn: sqlite3.Connection, start: str, end: str, kind="expense") -> list[dict]:
    meta = {c["name"]: c for c in list_categories(conn)}
    rows = store.rows(
        conn,
        "SELECT category, SUM(amount_minor) AS total, COUNT(*) AS txns,"
        " COUNT(DISTINCT day) AS days"
        " FROM transactions WHERE kind=? AND day>=? AND day<?"
        " GROUP BY category ORDER BY total DESC",
        (kind, start, end),
    )
    grand = sum(int(r["total"]) for r in rows) or 1
    for row in rows:
        info = meta.get(row["category"]) or {}
        row["total"] = int(row["total"])
        row["share"] = round(row["total"] * 100 / grand, 1)
        row["icon"] = info.get("icon", "🏷️")
        row["color"] = info.get("color", "#94a3b8")
    return rows


def by_method(conn: sqlite3.Connection, start: str, end: str, kind="expense") -> list[dict]:
    """Spending grouped by payment method; untagged rows fall under 'other'."""
    rows = store.rows(
        conn,
        "SELECT CASE WHEN method='' THEN 'other' ELSE method END AS method,"
        " SUM(amount_minor) AS total, COUNT(*) AS txns"
        " FROM transactions WHERE kind=? AND day>=? AND day<?"
        " GROUP BY 1 ORDER BY total DESC",
        (kind, start, end),
    )
    grand = sum(int(r["total"]) for r in rows) or 1
    for row in rows:
        row["total"] = int(row["total"])
        row["share"] = round(row["total"] * 100 / grand, 1)
    return rows


def list_methods(conn: sqlite3.Connection) -> list[str]:
    """Distinct payment methods actually used, for filter dropdowns."""
    return [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT method FROM transactions WHERE method<>'' ORDER BY 1"
        )
    ]


def daily_series(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    per_day = {
        row["day"]: row
        for row in store.rows(
            conn,
            "SELECT day,"
            " SUM(CASE WHEN kind='expense' THEN amount_minor ELSE 0 END) AS expense,"
            " SUM(CASE WHEN kind='income' THEN amount_minor ELSE 0 END) AS income"
            " FROM transactions WHERE day>=? AND day<? GROUP BY day",
            (start, end),
        )
    }
    series = []
    day = date.fromisoformat(start)
    last = date.fromisoformat(end)
    while day < last:
        key = day.isoformat()
        row = per_day.get(key) or {}
        series.append(
            {
                "day": key,
                "expense": int(row.get("expense") or 0),
                "income": int(row.get("income") or 0),
            }
        )
        day += timedelta(days=1)
    return series


def report(conn: sqlite3.Connection, *, month: str = "", year: str = "") -> dict:
    if year and not month:
        start, end = year_bounds(year)
        prev_start, prev_end = year_bounds(str(int(year) - 1))
        label, prev_label = f"{int(year):04d}", f"{int(year) - 1:04d}"
        days_elapsed = (min(date.today(), date.fromisoformat(end) - timedelta(days=1))
                        - date.fromisoformat(start)).days + 1
    else:
        month = month or date.today().isoformat()[:7]
        start, end = month_bounds(month)
        prev_start, prev_end = month_bounds(prev_month(month))
        label = month
        prev_label = prev_month(month)
        today = date.today()
        end_date = date.fromisoformat(end) - timedelta(days=1)
        days_elapsed = (min(today, end_date) - date.fromisoformat(start)).days + 1
    days_elapsed = max(days_elapsed, 1)

    current = totals(conn, start, end)
    previous = totals(conn, prev_start, prev_end)
    categories = by_category(conn, start, end)
    income_categories = by_category(conn, start, end, kind="income")
    daily = daily_series(conn, start, end)
    prev_daily = daily_series(conn, prev_start, prev_end)
    budgets = list_budgets(conn, start[:7])

    cumulative, running = [], 0
    for point in daily:
        running += point["expense"]
        cumulative.append({"day": point["day"], "expense": running})
    prev_cumulative, prev_running = [], 0
    for point in prev_daily:
        prev_running += point["expense"]
        prev_cumulative.append({"day": point["day"], "expense": prev_running})

    delta = current["expense_minor"] - previous["expense_minor"]
    return {
        "label": label,
        "prev_label": prev_label,
        "start": start,
        "end": end,
        "end_inclusive": (date.fromisoformat(end) - timedelta(days=1)).isoformat(),
        "days_elapsed": days_elapsed,
        "totals": current,
        "previous": previous,
        "delta_minor": delta,
        "avg_daily_minor": current["expense_minor"] // days_elapsed,
        "categories": categories,
        "income_categories": income_categories,
        "methods": by_method(conn, start, end),
        "daily": daily,
        "cumulative": cumulative,
        "prev_cumulative": prev_cumulative,
        "budgets": budgets,
    }


def overview(conn: sqlite3.Connection) -> dict:
    month = date.today().isoformat()[:7]
    start, end = month_bounds(month)
    recent = list_transactions(conn, limit=8)
    return {
        "month": month,
        "totals": totals(conn, start, end),
        "recent": recent,
        "budgets": list_budgets(conn, month),
        "sources": list_sources(conn),
        "inbox_pending": store.one(
            conn, "SELECT COUNT(*) AS n FROM inbox WHERE status='pending'"
        )["n"],
        "categories": list_categories(conn),
    }


# --- Inbox -------------------------------------------------------------------

def add_candidate(
    conn: sqlite3.Connection,
    origin: str,
    payload: dict,
    provenance: dict | None = None,
) -> dict:
    external_key = (provenance or {}).get("external_key")
    if external_key:
        dup = store.one(
            conn,
            "SELECT id, status FROM inbox WHERE json_extract(payload,'$.external_key')=?",
            (external_key,),
        )
        if dup:
            return {"id": dup["id"], "duplicate": True, "saved": False}
        payload = {**payload, "external_key": external_key}
    cur = conn.execute(
        "INSERT INTO inbox (origin, payload, provenance) VALUES (?,?,?)",
        (origin, json.dumps(payload), json.dumps(provenance or {})),
    )
    conn.commit()
    return {"id": cur.lastrowid, "saved": True, "duplicate": False}


def list_inbox(conn: sqlite3.Connection, status: str = "pending") -> list[dict]:
    sql = "SELECT * FROM inbox"
    params: tuple = ()
    if status:
        sql += " WHERE status=?"
        params = (status,)
    sql += " ORDER BY id DESC LIMIT 300"
    items = store.rows(conn, sql, params)
    for item in items:
        item["payload"] = json.loads(item["payload"] or "{}")
        item["provenance"] = json.loads(item["provenance"] or "{}")
        payload, provenance = item["payload"], item["provenance"]
        item["title"] = (
            payload.get("merchant")
            or provenance.get("subject")
            or payload.get("note")
            or "Captured item"
        )
        item["amount_minor"] = payload.get("amount_minor")
        item["day"] = payload.get("day")
        item["category"] = payload.get("category")
        item["method"] = payload.get("method") or ""
        item["kind"] = payload.get("kind") or "expense"
        item["confidence"] = payload.get("confidence")
        item["snippet"] = provenance.get("snippet") or payload.get("note") or ""
        item["has_image"] = bool(provenance.get("image"))
    return items


def confirm_candidate(conn: sqlite3.Connection, inbox_id: int, overrides: dict | None = None) -> dict:
    row = store.one(conn, "SELECT * FROM inbox WHERE id=?", (inbox_id,))
    if not row:
        raise ValidationError("inbox item not found")
    if row["status"] != "pending":
        raise ValidationError("inbox item already resolved")
    payload = {**json.loads(row["payload"] or "{}"), **(overrides or {})}
    saved = add_transaction(conn, payload)
    conn.execute(
        "UPDATE inbox SET status='confirmed', resolved_at=datetime('now') WHERE id=?",
        (inbox_id,),
    )
    conn.commit()
    return {"id": inbox_id, "transaction_id": saved["id"], "saved": True}


def dismiss_candidate(conn: sqlite3.Connection, inbox_id: int) -> dict:
    cur = conn.execute(
        "UPDATE inbox SET status='dismissed', resolved_at=datetime('now')"
        " WHERE id=? AND status='pending'",
        (inbox_id,),
    )
    conn.commit()
    if not cur.rowcount:
        raise ValidationError("inbox item not found or already resolved")
    return {"id": inbox_id, "dismissed": True}


def summary(conn: sqlite3.Connection) -> dict:
    month = date.today().isoformat()[:7]
    start, end = month_bounds(month)
    current = totals(conn, start, end)
    all_time = totals(conn, "0000-01-01", "9999-12-31")
    return {
        "month": month,
        "income_minor": current["income_minor"],
        "expense_minor": current["expense_minor"],
        "balance_minor": all_time["balance_minor"],
        "month_balance_minor": current["balance_minor"],
        "txns_this_month": current["txns"],
        "currency": "IDR",
    }

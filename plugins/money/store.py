"""Per-user Money ledger store.

One SQLite database per Tomo user inside the plugin's user data dir. Schema
migrations are idempotent and run on every connection: the original 0.1.x
``transactions`` table gains columns in place, so existing ledgers keep working.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

_DB_NAME = "ledger.db"

_migrated: set[str] = set()
_migrated_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('income', 'expense')),
    amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
    currency TEXT NOT NULL DEFAULT 'IDR',
    category TEXT NOT NULL DEFAULT 'Uncategorized',
    note TEXT NOT NULL DEFAULT '',
    day TEXT NOT NULL,
    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    method TEXT NOT NULL DEFAULT '',
    external_key TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'expense' CHECK (kind IN ('expense', 'income', 'any')),
    icon TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT '#8b8b9e',
    sort INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL DEFAULT 'cash',
    currency TEXT NOT NULL DEFAULT 'IDR',
    opening_balance_minor INTEGER NOT NULL DEFAULT 0,
    icon TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT '#8b8b9e',
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS budgets (
    id INTEGER PRIMARY KEY,
    category TEXT NOT NULL,
    month TEXT NOT NULL DEFAULT '',
    amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
    alert_pct INTEGER NOT NULL DEFAULT 80,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (category, month)
);

CREATE TABLE IF NOT EXISTS inbox (
    id INTEGER PRIMARY KEY,
    origin TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed', 'dismissed')),
    payload TEXT NOT NULL DEFAULT '{}',
    provenance TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS gmail_accounts (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    access_token TEXT NOT NULL DEFAULT '',
    refresh_token TEXT NOT NULL DEFAULT '',
    expires_at INTEGER NOT NULL DEFAULT 0,
    history_id TEXT NOT NULL DEFAULT '',
    last_sync_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY,
    redirect_uri TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL DEFAULT 0
);
"""

# Applied after column migrations so legacy tables gain indexed columns first.
INDEXES = """
CREATE INDEX IF NOT EXISTS ix_txn_day ON transactions(day);
CREATE INDEX IF NOT EXISTS ix_txn_category ON transactions(category);
CREATE INDEX IF NOT EXISTS ix_txn_method ON transactions(method);
CREATE UNIQUE INDEX IF NOT EXISTS ux_txn_external
    ON transactions(external_key) WHERE external_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_inbox_status ON inbox(status);
"""

# Columns added after 0.1.x; each is applied only when missing.
_TXN_COLUMNS = {
    "currency": "TEXT NOT NULL DEFAULT 'IDR'",
    "source_id": "INTEGER REFERENCES sources(id) ON DELETE SET NULL",
    "method": "TEXT NOT NULL DEFAULT ''",
    "external_key": "TEXT",
    "created_at": "TEXT NOT NULL DEFAULT ''",
    "updated_at": "TEXT NOT NULL DEFAULT ''",
}

DEFAULT_CATEGORIES = [
    ("Bills & Utilities", "expense", "🧾", "#8b5cf6", 10),
    ("Groceries", "expense", "🛒", "#22c55e", 20),
    ("Food & Dining", "expense", "🍜", "#ef4444", 30),
    ("Transport", "expense", "🛵", "#f59e0b", 40),
    ("Shopping", "expense", "🛍️", "#ec4899", 50),
    ("Coffee", "expense", "☕", "#a16207", 60),
    ("Entertainment", "expense", "🎬", "#3b82f6", 70),
    ("Health", "expense", "💊", "#10b981", 80),
    ("Travel", "expense", "✈️", "#06b6d4", 90),
    ("Education", "expense", "📚", "#6366f1", 100),
    ("Personal Care", "expense", "🧴", "#f472b6", 110),
    ("Home", "expense", "🏠", "#84cc16", 120),
    ("Salary", "income", "💼", "#22c55e", 130),
    ("Transfer", "any", "🔁", "#64748b", 140),
    ("Refund", "income", "↩️", "#14b8a6", 150),
    ("Investment", "income", "📈", "#0ea5e9", 160),
    ("Uncategorized", "any", "🏷️", "#94a3b8", 1000),
]

DEFAULT_SOURCES = [
    ("Cash", "cash", "💵", "#22c55e"),
]


def connect(data_dir: Path) -> sqlite3.Connection:
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(data_dir / _DB_NAME, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    key = str(data_dir / _DB_NAME)
    with _migrated_lock:
        needed = key not in _migrated
    if needed:
        migrate(conn)
        with _migrated_lock:
            _migrated.add(key)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    from .jobs import SCHEMA as JOB_SCHEMA
    from .google_apps import SCHEMA as GOOGLE_SCHEMA

    conn.executescript(SCHEMA)
    conn.executescript(JOB_SCHEMA)
    conn.executescript(GOOGLE_SCHEMA)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(transactions)")}
    for column, ddl in _TXN_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {column} {ddl}")
    conn.executescript(INDEXES)
    conn.execute(
        "UPDATE transactions SET created_at = datetime('now') WHERE created_at = ''"
    )
    conn.execute(
        "UPDATE transactions SET updated_at = datetime('now') WHERE updated_at = ''"
    )
    for name, kind, icon, color, sort in DEFAULT_CATEGORIES:
        conn.execute(
            "INSERT OR IGNORE INTO categories (name, kind, icon, color, sort) VALUES (?,?,?,?,?)",
            (name, kind, icon, color, sort),
        )
    for name, stype, icon, color in DEFAULT_SOURCES:
        conn.execute(
            "INSERT OR IGNORE INTO sources (name, type, icon, color) VALUES (?,?,?,?)",
            (name, stype, icon, color),
        )
    conn.commit()


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, params)]


def one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict | None:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value if isinstance(value, str) else json.dumps(value)),
    )
    conn.commit()

import base64
import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.plugins.manager import PluginManager
from app.runtime.tools.registry import ToolRegistry

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins/money"


@pytest.fixture
def manager(tmp_path, monkeypatch):
    from app.plugins import manager as module
    from app.services import store

    store.rebind(tmp_path / "money.db")
    value = PluginManager(tmp_path / "home")
    monkeypatch.setattr(module, "_manager", value)
    yield value
    value.close()


def _load_package():
    """Import the plugin's helper modules as a package, mirroring the loader."""
    namespace = "money_test_pkg"
    package = types.ModuleType(namespace)
    package.__path__ = [str(PLUGIN_DIR)]
    sys.modules[namespace] = package
    import importlib

    modules = types.SimpleNamespace()
    for name in ("store", "ledger", "capture", "gmail", "ai"):
        setattr(modules, name, importlib.import_module(f"{namespace}.{name}"))
    return modules


def _gmail_message(mid, sender, subject, body_text):
    return {
        "id": mid,
        "threadId": "t1",
        "internalDate": "1768000000000",
        "snippet": subject,
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
            ],
            "mimeType": "text/plain",
            "body": {
                "data": base64.urlsafe_b64encode(body_text.encode()).decode()
            },
        },
    }


def _link_gmail_account(conn):
    import time

    conn.execute(
        "INSERT INTO gmail_accounts (email, access_token, refresh_token,"
        " expires_at) VALUES ('a@x.com','tok','ref',?)",
        (int(time.time()) + 3600,),
    )
    conn.commit()


def test_money_user_isolation_and_durable_data(manager):
    from app.runtime.tools.user_ctx import bind_user, reset_user

    path = Path(__file__).resolve().parents[1] / "plugins/money"
    manager.install(str(path))
    manager.change("money", "enable")
    registry = ToolRegistry()
    token = bind_user("alice")
    try:
        saved = json.loads(
            registry.execute(
                "plugin__money__add_transaction",
                {"kind": "expense", "amount": "123.45", "category": "Food"},
            )
        )
        assert saved["saved"]
        assert (
            json.loads(registry.execute("plugin__money__summary", {}))["expense_minor"]
            == 12345
        )
        assert registry.execute(
            "plugin__money__add_transaction",
            {"kind": "expense", "amount": "-1", "category": "Food"},
        ).startswith("Error:")
        manager.change("money", "reload")
        assert (
            json.loads(registry.execute("plugin__money__summary", {}))["expense_minor"]
            == 12345
        )
    finally:
        reset_user(token)
    token = bind_user("bob")
    try:
        assert (
            json.loads(registry.execute("plugin__money__summary", {}))["expense_minor"]
            == 0
        )
    finally:
        reset_user(token)
    manager.change("money", "disable")
    manager.change("money", "enable")
    token = bind_user("alice")
    try:
        assert (
            len(json.loads(registry.execute("plugin__money__list_transactions", {})))
            == 1
        )
    finally:
        reset_user(token)


def test_money_pages_and_form_use_shared_agent_ledger(manager, monkeypatch):
    from app.main import create_app
    from app.runtime.tools.user_ctx import bind_user, reset_user

    manager.install(str(Path(__file__).resolve().parents[1] / "plugins/money"))
    manager.change("money", "enable")
    app = create_app()

    @app.middleware("http")
    async def auth(request, call_next):
        request.state.auth_user_id = "alice"
        return await call_next(request)

    client = TestClient(app, follow_redirects=False)
    assert client.get("/extensions").status_code == 200
    response = client.get("/plugins/money/")
    assert response.status_code == 200
    assert "Overview" in response.text and "Money out" in response.text
    response = client.post(
        "/plugins/money/transactions",
        data={
            "kind": "income",
            "amount": "500.25",
            "category": "Salary",
            "day": "2026-10-03",
        },
    )
    assert response.status_code == 303
    response = client.get("/plugins/money/transactions")
    assert response.status_code == 200
    assert "500.25" in response.text and "Salary" in response.text
    token = bind_user("alice")
    try:
        assert (
            json.loads(ToolRegistry().execute("plugin__money__summary", {}))[
                "income_minor"
            ]
            == 50025
        )
    finally:
        reset_user(token)
    assert (
        client.post(
            "/plugins/money/transactions",
            data={
                "kind": "expense",
                "amount": "20",
                "category": "Food",
                "day": "2026-10-03",
            },
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )


def test_legacy_ledger_migration_preserves_rows(tmp_path):
    mods = _load_package()
    raw = sqlite3.connect(tmp_path / "ledger.db")
    raw.execute(
        "CREATE TABLE transactions (id INTEGER PRIMARY KEY, kind TEXT,"
        " amount_minor INTEGER, category TEXT, note TEXT, day TEXT)"
    )
    raw.execute(
        "INSERT INTO transactions (kind, amount_minor, category, note, day)"
        " VALUES ('expense', 99900, 'Food', 'legacy row', '2025-01-05')"
    )
    raw.commit()
    raw.close()
    conn = mods.store.connect(tmp_path)
    row = mods.store.one(conn, "SELECT * FROM transactions WHERE id=1")
    assert row["amount_minor"] == 99900 and row["category"] == "Food"
    assert row["currency"] == "IDR" and row["day"] == "2025-01-05"
    assert row["method"] == ""
    assert mods.store.one(conn, "SELECT id FROM sources LIMIT 1")
    conn.close()


def test_budgets_sources_and_report(tmp_path):
    mods = _load_package()
    conn = mods.store.connect(tmp_path)
    source = mods.ledger.add_source(
        conn, {"name": "Wallet", "type": "cash", "opening_balance": "1000"}
    )
    mods.ledger.add_transaction(
        conn,
        {
            "kind": "expense",
            "amount": "250.50",
            "category": "Food",
            "day": "2026-02-10",
            "source_id": source["id"],
        },
    )
    mods.ledger.set_budget(
        conn, {"category": "Food", "amount": "200", "month": "2026-02"}
    )
    budgets = mods.ledger.list_budgets(conn, "2026-02")
    assert budgets[0]["spent_minor"] == 25050
    assert budgets[0]["over"] is True
    sources = mods.ledger.list_sources(conn)
    wallet = next(s for s in sources if s["name"] == "Wallet")
    assert wallet["balance_minor"] == 100000 - 25050
    report = mods.ledger.report(conn, month="2026-02")
    assert report["totals"]["expense_minor"] == 25050
    assert report["categories"][0]["category"] == "Food"
    assert len(report["daily"]) == 28 and len(report["cumulative"]) == 28
    conn.close()


def test_inbox_confirm_and_dismiss(tmp_path):
    mods = _load_package()
    conn = mods.store.connect(tmp_path)
    added = mods.ledger.add_candidate(
        conn,
        "gmail",
        {
            "kind": "expense",
            "amount": "42",
            "amount_minor": 4200,
            "merchant": "Tokopedia",
            "note": "Order 123",
            "day": "2026-02-11",
            "category": "Shopping",
        },
        {"external_key": "gmail:m1", "subject": "Your receipt"},
    )
    items = mods.ledger.list_inbox(conn)
    assert items[0]["title"] == "Tokopedia" and items[0]["amount_minor"] == 4200
    confirmed = mods.ledger.confirm_candidate(conn, added["id"], {"category": "Shopping"})
    tx = mods.store.one(
        conn, "SELECT * FROM transactions WHERE id=?", (confirmed["transaction_id"],)
    )
    assert tx["amount_minor"] == 4200 and tx["external_key"] == "gmail:m1"
    dup = mods.ledger.add_candidate(
        conn, "gmail", {}, {"external_key": "gmail:m1"}
    )
    assert dup["duplicate"] is True
    other = mods.ledger.add_candidate(conn, "csv", {"amount": "1"}, {})
    mods.ledger.dismiss_candidate(conn, other["id"])
    with pytest.raises(mods.ledger.ValidationError):
        mods.ledger.dismiss_candidate(conn, other["id"])
    conn.close()


def test_csv_import_and_email_parsing(tmp_path):
    mods = _load_package()
    conn = mods.store.connect(tmp_path)
    csv_text = (
        "date,type,amount,category,note,method\n"
        "2026-02-01,expense,15000,Food,Lunch,qris\n"
        "02/02/2026,income,500000,Salary,Payday,transfer\n"
        "2026-02-03,debit,7.500,Coffee,Kopi,cash\n"
    )
    result = mods.capture.import_csv(conn, csv_text)
    assert result["imported"] == 3 and result["skipped"] == 0
    row = mods.store.one(conn, "SELECT method FROM transactions WHERE note='Lunch'")
    assert row["method"] == "qris"
    again = mods.capture.import_csv(conn, csv_text)
    assert again["duplicates"] == 3

    amount = mods.capture.extract_amount("Total pembayaran: Rp 16.519.948")
    assert amount == (1651994800, "IDR")
    amount = mods.capture.extract_amount("Payment: USD 12.50")
    assert amount == (1250, "USD")
    amount = mods.capture.extract_amount("Tagihan Rp16.519,50")
    assert amount == (1651950, "IDR")

    body = base64.urlsafe_b64encode(
        b"Your payment of Rp 45.000 to GoRide was successful. Total: Rp 45.000"
    ).decode()
    message = {
        "id": "msg1",
        "threadId": "t1",
        "internalDate": "1768000000000",
        "snippet": "Payment successful",
        "payload": {
            "headers": [
                {"name": "From", "value": "GoPay <receipts@gopay.co.id>"},
                {"name": "Subject", "value": "Payment receipt"},
            ],
            "mimeType": "text/plain",
            "body": {"data": body},
        },
    }
    parsed = mods.capture.parse_gmail_message(message)
    assert parsed["payload"]["amount_minor"] == 4500000
    assert parsed["provenance"]["external_key"] == "gmail:msg1"
    assert parsed["payload"]["merchant"] == "GoPay"
    conn.close()


def test_gmail_search_llm_parse_and_dedupe(tmp_path, monkeypatch):
    mods = _load_package()
    conn = mods.store.connect(tmp_path)
    _link_gmail_account(conn)
    message = _gmail_message(
        "m1",
        "OCBC <alerts@ocbc.co.id>",
        "Transaction alert",
        "Your card was charged IDR 250.000 at STARBUCKS on 02-10-2026",
    )

    def fake_api(access, url, params=None):
        if url.endswith("/messages"):
            return {"messages": [{"id": "m1"}]}
        return message

    monkeypatch.setattr(mods.gmail, "_api_get", fake_api)

    result = mods.gmail.search(conn, "from:ocbc")
    assert result["query"] == "from:ocbc"
    msg = result["messages"][0]
    assert msg["external_key"] == "gmail:a@x.com:m1"
    assert msg["recorded"] is False and msg["queued"] is False
    assert "STARBUCKS" in msg["body"] and msg["from"].startswith("OCBC")

    monkeypatch.setattr(
        mods.ai,
        "extract_transaction",
        lambda text, cats, hints=None: {
            "kind": "expense",
            "amount": 250000,
            "currency": "IDR",
            "category": "Coffee",
            "day": "2026-10-02",
            "merchant": "Starbucks",
            "note": "Card charge at Starbucks",
        },
    )
    parsed = mods.capture.parse_gmail_message(message, ["Coffee"])
    assert parsed["payload"]["merchant"] == "Starbucks"
    assert parsed["payload"]["amount_minor"] == 25000000
    assert parsed["payload"]["category"] == "Coffee"
    assert parsed["payload"]["day"] == "2026-10-02"

    report = mods.gmail.sync(conn, query="from:ocbc")
    assert report["candidates"] == 1
    again = mods.gmail.search(conn, "from:ocbc")
    assert again["messages"][0]["queued"] is True

    saved = mods.ledger.add_transaction(
        conn,
        {
            "kind": "expense",
            "amount": "250000",
            "category": "Coffee",
            "external_key": "gmail:m1",
        },
    )
    assert saved["duplicate"] is False
    third = mods.gmail.search(conn, "from:ocbc")
    assert third["messages"][0]["recorded"] is True
    dup = mods.ledger.add_transaction(
        conn,
        {
            "kind": "expense",
            "amount": "250000",
            "category": "Coffee",
            "external_key": "gmail:m1",
        },
    )
    assert dup["duplicate"] is True
    report = mods.gmail.sync(conn, query="from:ocbc")
    assert report["duplicates"] == 1 and report["candidates"] == 0
    conn.close()


def test_money_toolset_crud_and_reports(manager):
    from app.runtime.tools.user_ctx import bind_user, reset_user

    manager.install(str(PLUGIN_DIR))
    manager.change("money", "enable")
    registry = ToolRegistry()
    token = bind_user("carol")
    try:
        def run(name, args):
            return json.loads(registry.execute(f"plugin__money__{name}", args))

        source = run("add_source", {"name": "BCA", "type": "bank"})
        assert source["saved"]
        txn = run(
            "add_transaction",
            {"kind": "expense", "amount": "80", "category": "Food", "source": "bca"},
        )
        assert txn["saved"]
        listed = run("list_transactions", {"category": "Food"})
        assert listed[0]["source_id"] == source["id"]
        run("update_transaction", {"id": txn["id"], "amount": "90"})
        report = run("report", {})
        assert report["totals"]["expense_minor"] == 9000
        m_txn = run(
            "add_transaction",
            {"kind": "expense", "amount": "10", "category": "Food",
             "method": " QRIS "},
        )
        qr = run("list_transactions", {"method": "qris"})
        assert [t["id"] for t in qr] == [m_txn["id"]]
        methods = {r["method"]: r for r in run("report", {})["methods"]}
        assert methods["qris"]["total"] == 1000
        assert methods["other"]["total"] == 9000
        run(
            "update_transaction",
            {"id": m_txn["id"], "method": "debit card"},
        )
        assert run("list_transactions", {"method": "debit card"})[0]["id"] == m_txn["id"]
        run("set_budget", {"category": "Food", "amount": "50"})
        assert run("list_budgets", {})[0]["over"] is True
        inbox = run("add_transaction", {
            "kind": "expense", "amount": "5", "category": "Misc",
        })
        run("delete_transaction", {"id": inbox["id"]})
        assert run("list_inbox", {}) == []
        tools = [t["function"]["name"] for t in
                 (d[0]["schema"] for d in manager._active["money"]["api"].tools.values())]
        assert len(tools) == len(set(tools)) >= 16
    finally:
        reset_user(token)


def test_money_new_pages_render(manager, monkeypatch):
    from app.main import create_app

    manager.install(str(PLUGIN_DIR))
    manager.change("money", "enable")
    app = create_app()

    @app.middleware("http")
    async def auth(request, call_next):
        request.state.auth_user_id = "dave"
        return await call_next(request)

    client = TestClient(app, follow_redirects=False)
    for path, marker in (
        ("/plugins/money/", "Money out"),
        ("/plugins/money/transactions", "Search notes"),
        ("/plugins/money/reports", "Where the money went"),
        ("/plugins/money/budgets", "New budget"),
        ("/plugins/money/sources", "New source"),
        ("/plugins/money/inbox", "Inbox zero"),
        ("/plugins/money/imports", "Bank statement CSV"),
        ("/plugins/money/apps", "Connected apps"),
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert marker in response.text, path
    assert client.get("/plugins/money/static/money.css").status_code == 200
    assert client.get("/plugins/money/static/money.js").status_code == 200


def test_money_home_card_is_per_user(manager):
    from app.runtime.tools.user_ctx import bind_user, reset_user
    from app.services.home import normalize_card

    manager.install(str(PLUGIN_DIR))
    manager.change("money", "enable")
    empty = manager.home_contributions("bob")["cards"][0]["data"]
    assert "empty" in normalize_card(empty)
    token = bind_user("alice")
    try:
        ToolRegistry().execute(
            "plugin__money__add_transaction",
            {"kind": "expense", "amount": "4210000", "category": "Food"},
        )
    finally:
        reset_user(token)
    out = manager.home_contributions("alice")
    card = normalize_card(out["cards"][0]["data"])
    assert card["metric"] == {"value": "Rp4.21jt", "label": "spent this month"}
    assert card["ring"]["segments"] == [{"label": "Food", "value": 421000000.0, "text": "100%"}]
    assert card["chart"][-1]["value"] == 421000000.0
    assert out["cards"][0]["size"] == "m" and out["cards"][0]["kanji"] == "金"
    assert {s["label"] for s in out["starters"]} == {"Log a purchase", "This month's spending"}
    assert "empty" in normalize_card(manager.home_contributions("bob")["cards"][0]["data"])

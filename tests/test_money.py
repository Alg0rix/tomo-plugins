import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.plugins.manager import PluginManager
from app.runtime.tools.registry import ToolRegistry


@pytest.fixture
def manager(tmp_path, monkeypatch):
    from app.plugins import manager as module
    from app.services import store

    store.rebind(tmp_path / "money.db")
    value = PluginManager(tmp_path / "home")
    monkeypatch.setattr(module, "_manager", value)
    yield value
    value.close()


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
    assert "Transactions" in response.text and "Open Tomo chat" in response.text
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

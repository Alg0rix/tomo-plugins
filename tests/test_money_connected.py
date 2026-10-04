from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_money import manager as money_manager

manager = money_manager  # Shared pytest fixture.


@pytest.fixture
def client(manager):
    from app.main import create_app

    manager.install(str(Path(__file__).resolve().parents[1] / "plugins/money"))
    manager.change("money", "enable")
    app = create_app()

    @app.middleware("http")
    async def auth(request, call_next):
        request.state.auth_user_id = request.headers.get("X-Test-User", "alice")
        return await call_next(request)

    return TestClient(app, follow_redirects=False)


def test_apps_ui_and_settings_validation(client):
    r = client.get("/plugins/money/apps")
    assert r.status_code == 200
    assert "Google Calendar" in r.text and "Google Sheets" in r.text
    assert "every 30 minutes" in r.text.lower()
    assert (
        client.post(
            "/plugins/money/apps/gmail/settings",
            json={"automatic": True, "lookback": "12m", "email": "missing"},
        ).status_code
        == 422
    )
    assert client.post("/plugins/money/apps/gmail/sync", json={}).status_code == 422
    assert (
        client.post(
            "/plugins/money/apps/sheets/settings",
            json={"target": "x", "automatic": False},
            headers={"Origin": "https://evil.test"},
        ).status_code
        == 403
    )


def test_bill_routes_isolate_users_and_validate(client):
    r = client.post(
        "/plugins/money/apps/calendar/bills",
        json={
            "title": "Internet",
            "amount": "250000",
            "due": "2026-10-31",
            "timezone": "Asia/Jakarta",
            "recurrence": "monthly",
        },
    )
    assert r.status_code == 200
    bill = r.json()
    assert bill["title"] == "Internet"
    assert len(client.get("/plugins/money/api/apps").json()["bills"]) == 1
    assert (
        client.get("/plugins/money/api/apps", headers={"X-Test-User": "bob"}).json()[
            "bills"
        ]
        == []
    )
    r = client.post(
        "/plugins/money/apps/calendar/bills/" + bill["id"] + "/delete",
        json={},
        headers={"X-Test-User": "bob"},
    )
    assert r.status_code == 422
    assert (
        client.post(
            "/plugins/money/apps/calendar/bills",
            json={"title": "Oops", "amount": "0", "due": "2026-01-01"},
        ).status_code
        == 422
    )


def test_job_route_is_enqueue_only_and_isolated(client, manager):
    # Locate the active SDK's data via public convention, not private manager state.
    import hashlib
    from test_money import _load_package, _link_gmail_account

    m = _load_package()
    # The actual manager home location is tested by discovering the ledger created by GET.
    client.get("/plugins/money/apps")
    candidates = list(manager.root.rglob("ledger.db"))
    data = next(
        p.parent
        for p in candidates
        if p.parent.name == hashlib.sha256(b"alice").hexdigest()
    )
    c = m.store.connect(data)
    _link_gmail_account(c)
    c.close()
    r = client.post("/plugins/money/apps/gmail/sync", json={"lookback": "12m"})
    assert r.status_code == 202
    job = r.json()["jobs"][0]
    assert job["status"] == "queued"
    assert "lease_token" not in job
    status = client.get("/plugins/money/api/apps").json()
    assert status["jobs"][0]["id"] == job["id"]
    assert (
        client.get("/plugins/money/api/apps", headers={"X-Test-User": "bob"}).json()[
            "jobs"
        ]
        == []
    )
    assert (
        client.post(
            "/plugins/money/apps/jobs/" + job["id"] + "/cancel",
            json={},
            headers={"X-Test-User": "bob"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/plugins/money/apps/jobs/" + job["id"] + "/cancel", json={}
        ).json()["status"]
        == "cancelled"
    )

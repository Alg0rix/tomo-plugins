import importlib
import threading
import time
from urllib.parse import urlsplit, parse_qs

import pytest
from app.core.secrets import encrypt_secret
from test_money import _load_package


@pytest.fixture
def env(tmp_path):
    m = _load_package()
    m.google = importlib.import_module("money_test_pkg.google_apps")
    m.jobs = importlib.import_module("money_test_pkg.jobs")
    c = m.store.connect(tmp_path)
    m.store.set_setting(c, "gmail_client_id", "client")
    m.store.set_setting(c, "gmail_client_secret", encrypt_secret("secret"))
    yield m, c
    c.close()


def link(m, c, provider):
    c.execute(
        "INSERT INTO google_connections(provider,email,access_token,refresh_token,expires_at) VALUES(?,?,?,?,?)",
        (
            provider,
            "me@example.com",
            encrypt_secret("access"),
            encrypt_secret("refresh"),
            time.time() + 3600,
        ),
    )
    c.commit()


def test_oauth_scopes_state_and_public_secrets(env):
    m, c = env
    url = m.google.begin_oauth(c, "sheets", "https://tomo.test/callback")
    args = parse_qs(urlsplit(url).query)
    assert m.google.SCOPES["sheets"] in args["scope"][0]
    assert "gmail" not in args["scope"][0]
    assert args["code_challenge_method"] == ["S256"]
    with pytest.raises(m.gmail.GmailError):
        m.google.finish_oauth(c, "calendar", "code", args["state"][0])
    link(m, c, "sheets")
    assert "access_token" not in m.google.connections(c)["sheets"]


def test_sheet_export_raw_values_and_atomic_clear(env, monkeypatch):
    m, c = env
    link(m, c, "sheets")
    m.google.configure(c, "sheets", "abc_123", False)
    m.ledger.add_transaction(
        c,
        {
            "kind": "expense",
            "amount": "12.34",
            "note": '=IMPORTXML("evil")',
            "day": "2026-10-04",
        },
    )
    m.ledger.add_candidate(c, "manual", {"amount": "99"}, {})
    writes = []

    def remote(conn, provider, method, path, **kw):
        if method == "GET":
            return {"sheets": [{"properties": {"title": "Tomo Money", "sheetId": 42}}]}
        writes.append(kw["json"])
        return {}

    monkeypatch.setattr(m.google, "request", remote)
    j = m.jobs.enqueue(c, "sheets")
    m.jobs.run_one(c, threading.Event())
    assert m.jobs.get(c, j["id"])["status"] == "succeeded"
    update = writes[0]["requests"][0]["updateCells"]
    assert update["range"] == {"sheetId": 42}
    assert update["fields"] == "userEnteredValue"
    assert len(update["rows"]) == 2
    assert any(
        v.get("userEnteredValue", {}).get("stringValue") == '=IMPORTXML("evil")'
        for v in update["rows"][1]["values"]
    )
    assert "formulaValue" not in str(update)


def test_calendar_stable_ids_recurrence_and_deletion(env, monkeypatch):
    m, c = env
    link(m, c, "calendar")
    m.google.configure(c, "calendar", "primary", False)
    bill = m.google.save_bill(
        c,
        {
            "title": "Internet",
            "amount": "250000",
            "due": "2026-10-31",
            "recurrence": "monthly",
            "timezone": "Asia/Jakarta",
        },
    )
    remote_events = {}

    def remote(conn, provider, method, path, **kw):
        eid = path.rsplit("/", 1)[-1]
        if method == "PUT":
            if eid not in remote_events:
                raise m.google.GoogleError("Not found", status=404)
            remote_events[eid] = kw["json"]
        elif method == "POST":
            event = kw["json"]
            remote_events[event["id"]] = event
        elif method == "DELETE":
            remote_events.pop(eid, None)
        return {}

    monkeypatch.setattr(m.google, "request", remote)
    for _ in range(2):
        j = m.jobs.enqueue(c, "calendar")
        for _ in range(4):
            m.jobs.run_one(c, threading.Event())
        assert m.jobs.get(c, j["id"])["status"] == "succeeded"
    assert len(remote_events) == 1
    event = next(iter(remote_events.values()))
    assert event["recurrence"] == ["RRULE:FREQ=MONTHLY"]
    assert event["start"] == {"date": "2026-10-31", "timeZone": "Asia/Jakarta"}
    assert "attendees" not in event
    m.google.delete_bill(c, bill["id"])
    m.jobs.enqueue(c, "calendar")
    for _ in range(4):
        m.jobs.run_one(c, threading.Event())
    assert remote_events == {}


def test_disconnect_cancels_only_provider_and_preserves_ledger(env):
    m, c = env
    link(m, c, "sheets")
    link(m, c, "calendar")
    a = m.jobs.enqueue(c, "sheets")
    b = m.jobs.enqueue(c, "calendar")
    m.google.disconnect(c, "sheets")
    assert m.jobs.get(c, a["id"])["status"] == "cancelled"
    assert m.jobs.get(c, b["id"])["status"] == "queued"
    assert "sheets" not in m.google.connections(c)
    assert "calendar" in m.google.connections(c)


def test_bill_validation(env):
    m, c = env
    for fields in (
        {"title": "x", "amount": "1", "due": "2026-02-30"},
        {"title": "x", "amount": "1", "due": "2026-02-28", "timezone": "unknown"},
        {"title": "x", "amount": "-1", "due": "2026-02-28"},
    ):
        with pytest.raises(m.ledger.ValidationError):
            m.google.save_bill(c, fields)


def test_reconnect_during_refresh_does_not_return_stale_token(env, monkeypatch):
    m, c = env
    link(m, c, "sheets")
    c.execute("UPDATE google_connections SET expires_at=0")
    c.commit()

    def refresh(_):
        c.execute(
            "UPDATE google_connections SET refresh_token=?,access_token=? WHERE provider=?",
            (encrypt_secret("new-refresh"), encrypt_secret("new-access"), "sheets"),
        )
        c.commit()
        return {"access_token": "stale-token"}

    monkeypatch.setattr(m.google, "token_post", refresh)
    monkeypatch.setattr(
        m.google.httpx,
        "request",
        lambda *a, **k: pytest.fail("A stale grant must never reach Google"),
    )
    with pytest.raises(m.ledger.ValidationError):
        m.google.request(c, "sheets", "GET", "target")


def test_calendar_lost_create_response_does_not_duplicate(env, monkeypatch):
    m, c = env
    link(m, c, "calendar")
    m.google.configure(c, "calendar", "primary", False)
    m.google.save_bill(c, {"title": "Rent", "amount": "1", "due": "2026-10-10"})
    events = {}

    def remote(conn, provider, method, path, **kw):
        if method == "PUT":
            eid = path.rsplit("/", 1)[-1]
            if eid not in events:
                raise m.google.GoogleError("Missing", status=404)
            events[eid] = kw["json"]
        elif method == "POST":
            events[kw["json"]["id"]] = kw["json"]
            raise m.google.GoogleError("Response lost")
        return {}

    monkeypatch.setattr(m.google, "request", remote)
    monkeypatch.setattr(m.jobs.time, "time", lambda: 1000)
    j = m.jobs.enqueue(c, "calendar")
    m.jobs.run_one(c, threading.Event())
    assert m.jobs.get(c, j["id"])["status"] == "retry"
    monkeypatch.setattr(m.jobs.time, "time", lambda: 1100)
    m.jobs.run_one(c, threading.Event())
    assert m.jobs.get(c, j["id"])["status"] == "succeeded"
    assert len(events) == 1


def test_google_oauth_exchange_stores_encrypted_tokens_and_consumes_state(
    env, monkeypatch
):
    import httpx

    m, c = env
    url = m.google.begin_oauth(c, "sheets", "https://tomo.test/callback")
    state = parse_qs(urlsplit(url).query)["state"][0]
    monkeypatch.setattr(
        m.google,
        "token_post",
        lambda data: {
            "access_token": "access",
            "refresh_token": "refresh",
            "scope": m.google.SCOPES["sheets"],
        },
    )
    monkeypatch.setattr(
        m.google.httpx,
        "get",
        lambda *a, **k: httpx.Response(200, json={"email": "me@example.com"}),
    )
    result = m.google.finish_oauth(c, "sheets", "code", state)
    assert result["email"] == "me@example.com"
    row = c.execute(
        "SELECT access_token,refresh_token FROM google_connections"
    ).fetchone()
    assert all(v.startswith("enc:v1:") for v in row)
    with pytest.raises(m.google.GoogleError):
        m.google.finish_oauth(c, "sheets", "code", state)


def test_google_rate_limit_is_transient(env):
    import httpx

    m, c = env
    with pytest.raises(m.google.GoogleError) as result:
        m.google._response(
            httpx.Response(
                403, json={"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}
            )
        )
    assert not result.value.permanent

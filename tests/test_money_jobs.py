"""Real SQLite job state; only the Google boundary is faked."""

import importlib
import threading

import pytest
from test_money import _load_package, _link_gmail_account, _gmail_message


@pytest.fixture
def env(tmp_path):
    m = _load_package()
    m.jobs = importlib.import_module("money_test_pkg.jobs")
    conn = m.store.connect(tmp_path)
    _link_gmail_account(conn)
    yield m, conn
    conn.close()


def test_enqueue_deduplicates_and_cancel_fences(env):
    m, c = env
    first = m.jobs.enqueue(c, "gmail", "me@example.com", {"lookback": "7d"})
    assert m.jobs.enqueue(c, "gmail", "me@example.com", {})["id"] == first["id"]
    claimed = m.jobs.claim(c, now=100)
    assert claimed and not m.jobs.claim(c, now=101)
    m.jobs.cancel(c, first["id"])
    assert not m.jobs.checkpoint(c, claimed, {"done": 999})
    assert m.jobs.get(c, first["id"])["status"] == "cancelled"


def test_retry_backoff_and_crash_recovery(env):
    m, c = env
    j = m.jobs.enqueue(c, "gmail", "me@example.com", {})
    old = m.jobs.claim(c, now=100)
    new = m.jobs.claim(c, now=221)
    assert old["lease_token"] != new["lease_token"]
    assert not m.jobs.checkpoint(c, old, {})
    m.jobs.fail(c, new, "Temporary provider error", now=222)
    assert not m.jobs.claim(c, now=223)
    retried = m.jobs.claim(c, now=300)
    assert retried["id"] == j["id"]
    assert retried["attempts"] == 2


def test_lookback_calendar_months_and_invalid(env):
    from datetime import date

    m, c = env
    assert m.jobs.lookback_start("0d", date(2026, 3, 31)) == date(2026, 3, 31)
    assert m.jobs.lookback_start("1m", date(2026, 3, 31)) == date(2026, 2, 28)
    assert m.jobs.lookback_start("12m", date(2024, 2, 29)) == date(2023, 2, 28)
    with pytest.raises(m.ledger.ValidationError):
        m.jobs.enqueue(c, "gmail", "me@example.com", {"lookback": "99y"})


def test_gmail_pagination_progress_dedupe_and_no_body(env, monkeypatch):
    m, c = env
    email = c.execute("SELECT email FROM gmail_accounts").fetchone()[0]
    pages = []

    def remote(access, url, params=None, timeout=None):
        if url.endswith("/messages"):
            pages.append((params or {}).get("pageToken", ""))
            if len(pages) == 1:
                return {
                    "messages": [{"id": str(i)} for i in range(61)],
                    "nextPageToken": "next",
                }
            return {"messages": [{"id": "61"}]}
        return _gmail_message(
            url.rsplit("/", 1)[-1],
            "bank",
            "Payment Rp 25.000",
            "Paid Rp 25.000 private body",
        )

    monkeypatch.setattr(m.gmail, "_api_get", remote)
    monkeypatch.setattr(m.ai, "extract_transaction", lambda *a: None)
    job = m.jobs.enqueue(c, "gmail", email, {"lookback": "7d"})
    for _ in range(70):
        m.jobs.run_one(c, threading.Event())
        if m.jobs.get(c, job["id"])["status"] == "succeeded":
            break
    result = m.jobs.get(c, job["id"])
    assert result["status"] == "succeeded"
    assert result["progress"]["done"] == result["progress"]["total"] == 62
    assert pages == ["", "next"]
    assert c.execute("SELECT count(*) FROM inbox").fetchone()[0] == 62
    assert c.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    assert "private body" not in str(
        list(c.execute("SELECT payload, provenance FROM inbox"))
    )
    again = m.jobs.enqueue(c, "gmail", email, {"lookback": "7d"})
    for _ in range(70):
        m.jobs.run_one(c, threading.Event())
        if m.jobs.get(c, again["id"])["status"] == "succeeded":
            break
    assert c.execute("SELECT count(*) FROM inbox").fetchone()[0] == 62


def test_stopped_worker_does_not_claim(env):
    m, c = env
    j = m.jobs.enqueue(c, "gmail", "me@example.com", {})
    stop = threading.Event()
    stop.set()
    m.jobs.run_one(c, stop)
    assert m.jobs.get(c, j["id"])["status"] == "queued"


def test_stale_failure_cannot_pause_reconnected_schedule(env):
    m, c = env
    m.jobs.configure(c, "gmail", "me@example.com", True)
    job = m.jobs.enqueue(c, "gmail", "me@example.com", {})
    old = m.jobs.claim(c)
    m.jobs.cancel(c, job["id"])
    m.jobs.configure(c, "gmail", "me@example.com", True)
    m.jobs.fail(c, old, "Cancelled old grant", permanent=True)
    assert m.jobs.settings(c, "gmail", "me@example.com")["automatic"] == 1


def test_gmail_search_flags_background_candidates(env, monkeypatch):
    m, c = env
    email = c.execute("SELECT email FROM gmail_accounts").fetchone()[0]
    message = _gmail_message("m1", "bank", "Payment Rp 25.000", "Paid Rp 25.000")
    monkeypatch.setattr(
        m.gmail,
        "_api_get",
        lambda access, url, params=None, timeout=None: (
            {"messages": [{"id": "m1"}]} if url.endswith("/messages") else message
        ),
    )
    monkeypatch.setattr(m.ai, "extract_transaction", lambda *a: None)
    j = m.jobs.enqueue(c, "gmail", email, {})
    for _ in range(4):
        m.jobs.run_one(c, threading.Event())
    assert m.jobs.get(c, j["id"])["status"] == "succeeded"
    result = m.gmail.search(c)["messages"][0]
    assert result["queued"]
    assert result["external_key"] == f" gmail:{email.lower()}:m1".strip()


def test_gmail_refresh_cannot_overwrite_new_grant(env, monkeypatch):
    import httpx
    from app.core.secrets import encrypt_secret

    m, c = env
    old = m.gmail._raw_account(c)[0]

    def refresh(*args, **kwargs):
        c.execute(
            "UPDATE gmail_accounts SET refresh_token=?,access_token=? WHERE id=?",
            (encrypt_secret("new-refresh"), encrypt_secret("new-access"), old["id"]),
        )
        c.commit()
        return httpx.Response(
            200, json={"access_token": "stale-access", "expires_in": 3600}
        )

    monkeypatch.setattr(m.gmail.httpx, "post", refresh)
    with pytest.raises(m.ledger.ValidationError):
        m.gmail._refresh(c, old)
    assert m.gmail._raw_account(c)[0]["access_token"] == "new-access"


def test_gmail_deleted_message_does_not_block_following_receipts(env, monkeypatch):
    m, c = env
    email = c.execute("SELECT email FROM gmail_accounts").fetchone()[0]

    def remote(access, url, params=None, timeout=None):
        if url.endswith("/messages"):
            return {"messages": [{"id": "gone"}, {"id": "valid"}]}
        if url.endswith("/gone"):
            raise m.gmail.GmailError("Message deleted", status=404)
        return _gmail_message("valid", "bank", "Payment Rp 25.000", "Paid Rp 25.000")

    monkeypatch.setattr(m.gmail, "_api_get", remote)
    monkeypatch.setattr(m.ai, "extract_transaction", lambda *a: None)
    j = m.jobs.enqueue(c, "gmail", email, {})
    for _ in range(6):
        m.jobs.run_one(c, threading.Event())
    result = m.jobs.get(c, j["id"])
    assert result["status"] == "succeeded"
    assert result["progress"]["skipped"] == 1
    assert result["progress"]["candidates"] == 1


def test_schedule_is_opt_in_and_every_thirty_minutes(env, monkeypatch):
    m, c = env
    monkeypatch.setattr(m.jobs.time, "time", lambda: 1000)
    m.jobs.configure(c, "gmail", "a@x.com", False)
    m.jobs.schedule(c)
    assert not m.jobs.recent(c)
    m.jobs.configure(c, "gmail", "a@x.com", True, "12m")
    monkeypatch.setattr(m.jobs.time, "time", lambda: 2799)
    m.jobs.schedule(c)
    assert not m.jobs.recent(c)
    monkeypatch.setattr(m.jobs.time, "time", lambda: 2800)
    m.jobs.schedule(c)
    assert len(m.jobs.recent(c)) == 1
    assert m.jobs.recent(c)[0]["lookback"] == "12m"
    assert m.jobs.settings(c, "gmail", "a@x.com")["next_run"] == 4600


def test_stop_during_fetch_prevents_candidate_write(env, monkeypatch):
    m, c = env
    email = c.execute("SELECT email FROM gmail_accounts").fetchone()[0]
    stop = threading.Event()

    def remote(access, url, params=None, timeout=None):
        if url.endswith("/messages"):
            return {"messages": [{"id": "m1"}]}
        stop.set()
        return _gmail_message("m1", "bank", "Payment Rp 25.000", "Paid Rp 25.000")

    monkeypatch.setattr(m.gmail, "_api_get", remote)
    monkeypatch.setattr(m.ai, "extract_transaction", lambda *a: None)
    m.jobs.enqueue(c, "gmail", email, {})
    m.jobs.run_one(c, stop)
    m.jobs.run_one(c, stop)
    assert c.execute("SELECT count(*) FROM inbox").fetchone()[0] == 0


def test_tick_discovers_only_active_owners_and_binds_context(tmp_path, monkeypatch):
    import hashlib
    from types import SimpleNamespace
    from app.plugins import services
    from app.runtime.tools.user_ctx import current_user_id

    m = _load_package()
    j = importlib.import_module("money_test_pkg.jobs")
    api = SimpleNamespace(data_dir=tmp_path)
    api.user_data_dir = lambda uid: (
        tmp_path / "users" / hashlib.sha256(uid.encode()).hexdigest()
    )
    for owner in ("active", "disabled"):
        c = m.store.connect(api.user_data_dir(owner))
        m.store.set_setting(c, "jobs_owner", owner)
        c.close()

    def active_user(uid):
        if uid == "disabled":
            raise PermissionError()
        return uid

    monkeypatch.setattr(services, "active_user", active_user)
    observed = []
    monkeypatch.setattr(
        j, "run_one", lambda c, stop: observed.append(current_user_id())
    )
    j.tick(api, threading.Event())
    assert observed == ["active"]


def test_gmail_rate_limit_is_transient(env, monkeypatch):
    import httpx

    m, c = env
    monkeypatch.setattr(
        m.gmail.httpx,
        "get",
        lambda *a, **k: httpx.Response(
            403, json={"error": {"errors": [{"reason": "rateLimitExceeded"}]}}
        ),
    )
    with pytest.raises(m.gmail.GmailError) as result:
        m.gmail._api_get("test", m.gmail.API + "/messages")
    assert not result.value.permanent


def test_background_extraction_closes_model_client(env, monkeypatch):
    from types import SimpleNamespace
    from app.runtime import llm

    m, c = env
    closed = []

    class Client:
        async def complete(self, messages):
            return SimpleNamespace(content='{"amount":1}')

        async def aclose(self):
            closed.append(True)

    monkeypatch.setattr(llm, "get_auxiliary_llm", lambda *a, **k: Client())
    assert m.ai.extract_transaction("Paid 1", ["Uncategorized"]) == {"amount": 1}
    assert closed == [True]

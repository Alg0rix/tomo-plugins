from pathlib import Path

from fastapi import Request
from fastapi.testclient import TestClient
import pytest

from app.plugins.manager import PluginManager

OFFICIAL = Path(__file__).resolve().parents[1] / "plugins"


@pytest.fixture
def official(tmp_path, monkeypatch):
    from app.plugins import manager as module
    from app.services import store

    store.rebind(tmp_path / "store.db")
    value = PluginManager(tmp_path / "home")
    for identity in ("token_monitor", "kanban"):
        value.install(str(OFFICIAL / identity))
        value.change(identity, "enable")
    monkeypatch.setattr(module, "_manager", value)
    value.start()
    yield value
    value.close()


def test_official_pages_assets_settings_and_removed_interfaces(official):
    from app.main import create_app
    from app.services import store

    store.update_settings({"setup_complete": True})
    app = create_app()

    @app.middleware("http")
    async def auth(request: Request, call_next):
        request.state.auth_user_id = "usr_admin"
        return await call_next(request)

    client = TestClient(app, follow_redirects=False)
    response = client.get("/plugins/token_monitor/")
    assert response.status_code == 200
    assert "/plugins/token_monitor/static/page.css" in response.text
    assert 'data-api="/plugins/token_monitor/api/usage"' in response.text
    assert client.get("/plugins/token_monitor/static/page.css").status_code == 200
    assert client.get("/plugins/token_monitor/static/page.js").status_code == 200
    assert client.get("/plugins/token_monitor/api/usage").status_code == 200
    assert client.get("/plugins/kanban/").status_code == 200
    response = client.get("/system")
    assert response.status_code == 200
    assert 'id="sec-plugins"' in response.text
    assert 'id="sec-modules"' not in response.text
    assert client.get("/extensions").status_code == 200
    for path in (
        "/modules",
        "/api/modules",
        "/api/modules/token_monitor",
        "/usage",
        "/api/usage",
        "/board",
    ):
        assert client.get(path).status_code == 404
    assert (
        client.put("/api/plugins/token_monitor", json={"enabled": False}).status_code
        == 404
    )
    official.change("token_monitor", "disable")
    assert client.get("/plugins/token_monitor/").status_code == 404
    assert client.get("/plugins/token_monitor/static/page.css").status_code == 404
    assert client.get("/plugins/token_monitor/api/usage").status_code == 404
    assert client.get("/plugins/kanban/").status_code == 200
    official.change("token_monitor", "enable")
    official.change("token_monitor", "reload")
    assert client.get("/plugins/token_monitor/").status_code == 200


def test_turn_hooks_record_usage_only_while_enabled(official):
    from app.services import store

    session = store.get_or_create_session("main", "usr_admin")

    def record():
        store.dispatch_turn_end(
            session_id=session,
            agent_id="main",
            message="hello",
            prompt_tokens=123,
            completion_tokens=45,
        )

    record()

    def events():
        return store.with_db(
            lambda conn: [
                dict(row) for row in conn.execute("SELECT * FROM usage_events")
            ]
        )

    assert len(events()) == 1
    assert events()[0]["prompt_tokens"] == 123
    assert events()[0]["completion_tokens"] == 45
    official.change("token_monitor", "disable")
    record()
    assert len(events()) == 1
    official.change("token_monitor", "enable")
    record()
    assert len(events()) == 2
    official.change("token_monitor", "reload")
    record()
    assert len(events()) == 3


def test_official_disabled_state_survives_server_start(official):
    official.change("token_monitor", "disable")
    restored = PluginManager(official.root)
    restored.start()
    try:
        rows = {row["id"]: row for row in restored.list()}
        assert not rows["token_monitor"]["running"]
        assert rows["kanban"]["running"]
        restored.change("kanban", "uninstall")
        assert not any(p["id"] == "kanban" for p in restored.list())
    finally:
        restored.close()



import base64

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from multiagent.api import create_app
from multiagent.auth import UserStore, hash_password, verify_password

from test_system import make_system

PW = "admin-pass-123"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", PW)
    system, _ = make_system(tmp_path, {})
    return create_app(system, users=UserStore(tmp_path / "users.json"))


def basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def login(c, user="admin", pw=PW):
    r = c.post("/auth/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_password_hashing():
    h = hash_password("secret-123")
    assert "secret-123" not in h and verify_password("secret-123", h) and not verify_password("nope", h)


def test_every_api_route_requires_auth(app):
    c = TestClient(app)
    public = {"/auth/login", "/auth/logout", "/", "/demo/voice", "/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc", "/app"}
    checked = 0
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None)
        if path in public or not methods:
            continue
        for m in methods - {"HEAD", "OPTIONS"}:
            url = path.replace("{name}", "x").replace("{tool}", "t").replace("{sub}", "s").replace("{username}", "u")
            assert c.request(m, url).status_code == 401, f"{m} {path} is not protected"
            checked += 1
    assert checked > 15
    assert c.get("/app/").status_code == 200  # the web app (login page) itself is public


def test_login_token_cookie_and_basic(app):
    c = TestClient(app)
    assert c.post("/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    token = login(c)  # also sets the session cookie on this client
    assert c.get("/agents").status_code == 200

    fresh = TestClient(app)
    assert fresh.get("/agents", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert fresh.get("/auth/me", headers=basic("admin", PW)).json()["role"] == "admin"
    assert fresh.get("/agents", headers=basic("admin", "wrong")).status_code == 401
    assert fresh.get("/agents", headers={"Authorization": "Bearer " + token[:-2] + "xx"}).status_code == 401

    c.post("/auth/logout")
    assert c.get("/agents").status_code == 401


def test_login_rate_limit(app):
    c = TestClient(app)
    for _ in range(5):
        assert c.post("/auth/login", json={"username": "admin", "password": "bad"}).status_code == 401
    assert c.post("/auth/login", json={"username": "admin", "password": PW}).status_code == 429


def test_user_management_and_roles(app):
    admin = TestClient(app, headers=basic("admin", PW))
    assert admin.post("/auth/users", json={"username": "bob", "password": "short"}).status_code == 422
    assert admin.post("/auth/users", json={"username": "bob", "password": "bob-pass-123"}).status_code == 201
    assert admin.post("/auth/users", json={"username": "bob", "password": "bob-pass-123"}).status_code == 409
    assert [u["username"] for u in admin.get("/auth/users").json()] == ["admin", "bob"]

    bob = TestClient(app)
    login(bob, "bob", "bob-pass-123")
    assert bob.get("/agents").status_code == 200          # normal users can use the app
    assert bob.get("/auth/users").status_code == 403      # but not manage users
    assert bob.delete("/auth/users/admin").status_code == 403

    assert admin.delete("/auth/users/admin").status_code == 409  # can't delete yourself
    assert admin.delete("/auth/users/bob").status_code == 204
    assert bob.get("/agents").status_code == 401          # deleted user's session stops working


def test_change_password_signs_out_old_sessions(app):
    c = TestClient(app)
    old = login(c)
    assert c.post("/auth/password", json={"current_password": "wrong", "new_password": "new-pass-456"}).status_code == 400
    r = c.post("/auth/password", json={"current_password": PW, "new_password": "new-pass-456"})
    assert r.status_code == 200
    assert TestClient(app).get("/agents", headers={"Authorization": f"Bearer {old}"}).status_code == 401
    assert c.get("/agents").status_code == 200  # this client got the new cookie
    assert TestClient(app).get("/agents", headers=basic("admin", "new-pass-456")).status_code == 200


def test_websockets_require_auth(app):
    app.state.system.add_agent("bot", __import__("test_system").cfg("bot"))
    c = TestClient(app)
    for path in ("/ws/agents/bot/chat", "/ws/agents/bot/voice"):
        with c.websocket_connect(path) as ws:
            assert ws.receive_json()["message"].startswith("Not authenticated")
            with pytest.raises(WebSocketDisconnect) as e:
                ws.receive_json()
            assert e.value.code == 4401
    token = login(c)
    with c.websocket_connect(f"/ws/agents/bot/chat?token={token}") as ws:
        assert ws.receive_json()["type"] == "ready"
    with c.websocket_connect("/ws/agents/bot/chat") as ws:  # session cookie from login
        assert ws.receive_json()["type"] == "ready"
    # cookie from another site's page is refused
    with c.websocket_connect("/ws/agents/bot/chat", headers={"origin": "https://evil.example"}) as ws:
        assert ws.receive_json()["type"] == "error"


def test_generated_admin_password(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    store = UserStore(tmp_path / "u.json")
    with caplog.at_level("WARNING"):
        store.ensure_admin()
    assert store.list()[0].role == "admin" and "generated password" in caplog.text
    assert oct((tmp_path / "u.json").stat().st_mode)[-3:] == "600"

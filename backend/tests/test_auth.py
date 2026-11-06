"""APP_PASSWORD protection, and serving the built frontend from the backend."""
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth
from app.config import settings
from app.main import mount_frontend


@pytest.fixture()
def locked(monkeypatch):
    monkeypatch.setattr(settings, "app_password", "correct horse")
    auth._failures.clear()
    yield
    auth._failures.clear()


def test_everything_is_open_without_a_password(client):
    assert client.get("/api/projects").status_code == 200
    assert client.get("/api/auth/status").json() == {"required": False, "authenticated": True}


def test_api_requires_sign_in_when_a_password_is_set(client, locked):
    assert client.get("/api/projects").status_code == 401
    assert client.get("/api/credits").status_code == 401
    assert client.post("/api/backup/run").status_code == 401
    assert client.get("/api/auth/status").json() == {"required": True, "authenticated": False}


def test_health_and_auth_endpoints_stay_public(client, locked):
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/auth/status").status_code == 200


def test_login_with_the_right_password_opens_the_api(client, locked):
    resp = client.post("/api/auth/login", json={"password": "correct horse"})
    assert resp.status_code == 200
    cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "secure" not in cookie

    assert client.get("/api/projects").status_code == 200
    assert client.get("/api/auth/status").json()["authenticated"] is True


def test_secure_cookie_behind_an_https_proxy(client, locked):
    resp = client.post(
        "/api/auth/login", json={"password": "correct horse"}, headers={"x-forwarded-proto": "https"},
    )
    assert "secure" in resp.headers["set-cookie"].lower()


def test_wrong_password_is_refused_and_sets_no_cookie(client, locked):
    resp = client.post("/api/auth/login", json={"password": "nope"})
    assert resp.status_code == 401
    assert "set-cookie" not in resp.headers
    assert client.get("/api/projects").status_code == 401


def test_repeated_failures_lock_the_login_out_for_a_while(client, locked):
    for _ in range(5):
        assert client.post("/api/auth/login", json={"password": "bad"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "bad"}).status_code == 429
    assert client.post("/api/auth/login", json={"password": "correct horse"}).status_code == 429


def test_logout_closes_the_session(client, locked):
    client.post("/api/auth/login", json={"password": "correct horse"})
    assert client.get("/api/projects").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/projects").status_code == 401


def test_tampered_or_expired_tokens_are_rejected(locked):
    good = auth.make_token()
    expiry, _, sig = good.partition(".")
    assert auth.valid_token(good)
    assert not auth.valid_token(f"{int(expiry) + 1000}.{sig}")          # forged expiry
    assert not auth.valid_token(f"{expiry}.{'0' * len(sig)}")           # forged signature
    assert not auth.valid_token("garbage") and not auth.valid_token("") and not auth.valid_token(None)
    assert not auth.valid_token(good, now=time.time() + auth.SESSION_SECONDS + 5)


def test_changing_the_password_signs_everyone_out(locked, monkeypatch):
    token = auth.make_token()
    monkeypatch.setattr(settings, "app_password", "a different one")
    assert not auth.valid_token(token)


def test_websockets_are_guarded_too(client, locked):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect("/ws/batches/anything"):
            pass
    assert closed.value.code == 4401

    client.post("/api/auth/login", json={"password": "correct horse"})
    with client.websocket_connect("/ws/batches/anything") as ws:
        assert "error" in ws.receive_json()   # unknown batch, but we got in


# ── static frontend ──────────────────────────────────────────────────────────

@pytest.fixture()
def spa(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>app</html>")
    (tmp_path / "assets" / "main.js").write_text("console.log(1)")
    (tmp_path.parent / "secret.txt").write_text("outside the folder")
    target = FastAPI()

    @target.get("/api/ping")
    def ping():
        return {"pong": True}

    mount_frontend(target, str(tmp_path))
    return TestClient(target)


def test_frontend_serves_index_assets_and_falls_back_for_app_routes(spa):
    assert spa.get("/").text == "<html>app</html>"
    assert spa.get("/assets/main.js").text == "console.log(1)"
    assert spa.get("/some/client/route").text == "<html>app</html>"


def test_frontend_never_shadows_the_api(spa):
    assert spa.get("/api/ping").json() == {"pong": True}
    assert spa.get("/api/does-not-exist").status_code == 404
    assert spa.get("/ws/nothing").status_code == 404


def test_frontend_cannot_read_files_outside_its_folder(spa):
    resp = spa.get("/..%2Fsecret.txt")
    assert "outside the folder" not in resp.text
    assert "outside the folder" not in spa.get("/../secret.txt").text


def test_missing_frontend_folder_is_ignored(tmp_path):
    target = FastAPI()
    mount_frontend(target, str(tmp_path / "nope"))
    assert TestClient(target).get("/").status_code == 404

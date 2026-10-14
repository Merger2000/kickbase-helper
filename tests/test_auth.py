import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app.auth import LoginThrottle, require_login, safe_next_path, verify_password
from app.main import create_app
from tests.conftest import TEST_PASSWORD, login


def test_pages_require_login(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_redirect_keeps_target_path(settings):
    app = create_app(settings)

    @app.get("/geschuetzt", dependencies=[Depends(require_login)])
    def protected():
        return {"ok": True}

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/geschuetzt", follow_redirects=False)
        assert response.headers["location"] == "/login?next=%2Fgeschuetzt"
        assert login(client, next_path="/geschuetzt").headers["location"] == "/geschuetzt"


def test_htmx_requests_get_hx_redirect(client):
    response = client.get("/", headers={"HX-Request": "true"}, follow_redirects=False)
    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == "/login"


def test_login_with_correct_password(client):
    response = login(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    home = client.get("/")
    assert home.status_code == 200
    assert "Mein Team" in home.text


def test_session_cookie_flags(client):
    response = login(client)
    cookie = response.headers["set-cookie"].lower()
    assert "kbh_session=" in cookie
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=lax" in cookie


def test_login_with_wrong_password(client):
    response = login(client, password="falsch")
    assert response.status_code == 401
    assert "Das Passwort stimmt nicht." in response.text
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_rejects_missing_csrf(client):
    client.get("/login")
    response = client.post("/login", data={"password": TEST_PASSWORD}, follow_redirects=False)
    assert response.status_code == 400
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_throttled_after_repeated_failures(client):
    for _ in range(5):
        assert login(client, password="falsch").status_code == 401
    response = login(client)  # even the correct password is refused while blocked
    assert response.status_code == 429


def test_login_redirects_to_safe_next_only(client):
    assert login(client, next_path="https://evil.example").headers["location"] == "/"


def test_logout_clears_session_even_with_stale_form(logged_in_client):
    client = logged_in_client
    response = client.post("/logout", data={"csrf": "veraltet"}, follow_redirects=False)
    assert response.headers["location"] == "/login"
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_page_redirects_when_already_logged_in(logged_in_client):
    response = logged_in_client.get("/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


@pytest.mark.parametrize("target", ["https://evil.example", "//evil.example", "//["])
def test_logged_in_login_page_never_redirects_offsite(logged_in_client, target):
    response = logged_in_client.get("/login", params={"next": target}, follow_redirects=False)
    assert response.headers["location"] == "/"


def test_crafted_next_on_login_page_is_not_a_server_error(client):
    assert client.get("/login", params={"next": "//["}).status_code == 200


def test_non_ascii_csrf_is_rejected_cleanly(client):
    client.get("/login")
    response = client.post(
        "/login", data={"password": TEST_PASSWORD, "csrf": "ä"}, follow_redirects=False
    )
    assert response.status_code == 400


def test_successful_login_resets_throttle(client):
    for _ in range(4):
        assert login(client, password="falsch").status_code == 401
    assert login(client).status_code == 303
    client.cookies.clear()
    assert login(client, password="falsch").status_code == 401
    assert login(client).status_code == 303


def _session_cookie(payload: dict, secret: str) -> str:
    import base64
    import json

    import itsdangerous

    data = base64.b64encode(json.dumps(payload).encode())
    return itsdangerous.TimestampSigner(secret).sign(data).decode()


@pytest.mark.parametrize(
    ("payload", "secret"),
    [
        ({"auth": True}, "falsches-secret-" * 3),  # signed with another key
        ({"auth": "true"}, "s" * 40),  # right key, but not the boolean True
    ],
)
def test_forged_session_cookies_are_rejected(client, payload, secret):
    client.cookies.set("kbh_session", _session_cookie(payload, secret), domain="testserver")
    assert client.get("/", follow_redirects=False).status_code == 303


def test_unsigned_session_cookie_is_rejected(client):
    import base64

    client.cookies.set(
        "kbh_session", base64.b64encode(b'{"auth": true}').decode(), domain="testserver"
    )
    assert client.get("/", follow_redirects=False).status_code == 303


def test_htmx_redirect_returns_to_current_page(settings):
    app = create_app(settings)

    @app.get("/partials/kader", dependencies=[Depends(require_login)])
    def partial():
        return {"ok": True}

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get(
            "/partials/kader",
            headers={"HX-Request": "true", "HX-Current-URL": "https://testserver/spieler?sort=mv"},
            follow_redirects=False,
        )
    assert response.headers["HX-Redirect"] == "/login?next=%2Fspieler%3Fsort%3Dmv"


def test_redirect_keeps_query_string(settings):
    app = create_app(settings)

    @app.get("/spieler", dependencies=[Depends(require_login)])
    def players():
        return {"ok": True}

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/spieler?sort=mv", follow_redirects=False)
    assert response.headers["location"] == "/login?next=%2Fspieler%3Fsort%3Dmv"


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        (None, "/"),
        ("", "/"),
        ("/spieler/123", "/spieler/123"),
        ("//evil.example", "/"),
        ("/\\evil.example", "/"),
        ("https://evil.example/x", "/"),
        ("relative", "/"),
        ("//[", "/"),
        ("/\t/[x", "/"),
        ("/\t/evil.example", "/"),
        ("///evil.example", "/"),
        ("https:evil.example", "/"),
        ("/spieler?sort=mv", "/spieler?sort=mv"),
    ],
)
def test_safe_next_path(candidate, expected):
    assert safe_next_path(candidate) == expected


def test_verify_password_handles_invalid_hash():
    assert verify_password("not-a-hash", "whatever") is False


def test_throttle_window_expires():
    throttle = LoginThrottle(max_failures=2, window_seconds=10)
    throttle.record_failure(now=0)
    throttle.record_failure(now=1)
    assert throttle.blocked(now=5)
    assert not throttle.blocked(now=12)

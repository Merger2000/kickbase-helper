import json

import pytest

from app.main import SECURITY_HEADERS


def test_manifest(client):
    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")
    manifest = json.loads(response.text)
    assert manifest["display"] == "standalone"
    purposes = {icon["purpose"] for icon in manifest["icons"]}
    assert {"any", "maskable"} <= purposes
    for icon in manifest["icons"]:
        assert client.get(icon["src"]).status_code == 200


def test_service_worker_served_from_root(client):
    response = client.get("/sw.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


def test_offline_assets_exist(client):
    assert client.get("/static/offline.html").status_code == 200
    assert client.get("/static/css/offline.css").status_code == 200


@pytest.mark.parametrize("path", ["/login", "/", "/health", "/static/css/app.css"])
def test_security_headers_everywhere(client, path):
    response = client.get(path, follow_redirects=False)
    for name, value in SECURITY_HEADERS.items():
        assert response.headers.get(name) == value, f"{name} missing on {path}"
    assert "script-src 'self'" in response.headers["content-security-policy"]


def test_layout_is_mobile_ready(client):
    html = client.get("/login").text
    assert 'name="viewport"' in html
    assert 'rel="manifest"' in html
    assert "apple-touch-icon" in html

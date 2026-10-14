import os
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, make_url, text

from app.auth import hash_password
from app.config import Settings
from app.db.models import Base
from app.main import create_app
from tests.fakes import (
    FakeOpenLigaDb,
    add_recommendation_endpoints,
    fake_with_team_data,
    install_fake_kickbase,
    make_oldb_client,
)

TEST_PASSWORD = "richtig-geheim"


def pytest_configure(config):
    """Give every pytest-xdist worker its own PostgreSQL database.

    The PostgreSQL fixtures drop and create all tables, so parallel workers must not share one
    database. Without xdist (or without TEST_POSTGRES_URL) nothing changes.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    url = os.environ.get("TEST_POSTGRES_URL")
    if not worker or not url:
        return
    base = make_url(url)
    name = f"{base.database}_{worker}"
    engine = create_engine(base, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
            ).first()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        engine.dispose()
    os.environ["TEST_POSTGRES_URL"] = base.set(database=name).render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def password_hash() -> str:
    return hash_password(TEST_PASSWORD)


@pytest.fixture
def settings(tmp_path, password_hash) -> Settings:
    return Settings(
        _env_file=None,
        app_password_hash=password_hash,
        session_secret="s" * 40,
        database_url=f"sqlite+pysqlite:///{tmp_path / 'test.db'}",
        secrets_dir=tmp_path / "secrets",
    )


@pytest.fixture
def fake_kickbase():
    fake = fake_with_team_data()
    add_recommendation_endpoints(fake)
    return fake


@pytest.fixture
def fake_oldb():
    return FakeOpenLigaDb()


@pytest.fixture
def app(settings, fake_kickbase, fake_oldb):
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    application = create_app(settings)
    install_fake_kickbase(application, fake_kickbase)
    application.state.openligadb = make_oldb_client(fake_oldb)
    return application


@pytest.fixture
def client(app):
    # https base URL so the Secure session cookie is sent back like in production.
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client


def extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "CSRF field missing"
    return match.group(1)


def login(client: TestClient, password: str = TEST_PASSWORD, next_path: str = "/"):
    csrf = extract_csrf(client.get("/login").text)
    return client.post(
        "/login",
        data={"password": password, "csrf": csrf, "next": next_path},
        follow_redirects=False,
    )


@pytest.fixture
def logged_in_client(client):
    response = login(client)
    assert response.status_code == 303
    return client

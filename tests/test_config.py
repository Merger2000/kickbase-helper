from pathlib import Path

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.main import build_app

REPO = Path(__file__).resolve().parents[1]


def make(password_hash: str, **overrides) -> Settings:
    values = {"app_password_hash": password_hash, "session_secret": "s" * 40} | overrides
    return Settings(_env_file=None, **values)


def test_valid_settings_and_secure_default(password_hash):
    settings = make(password_hash)
    assert settings.session_https_only is True


def test_short_session_secret_rejected_without_echoing_it(password_hash):
    with pytest.raises(ValidationError) as exc_info:
        make(password_hash, session_secret="zu-kurz-und-geheim")
    assert "zu-kurz-und-geheim" not in str(exc_info.value)


@pytest.mark.parametrize(
    "bad_hash",
    [
        "",
        "replace-me",
        "$argon2id$v=19$m=65536,t=3,p=4$REPLACE$REPLACE",
    ],
)
def test_malformed_password_hash_rejected(bad_hash):
    with pytest.raises(ValidationError):
        make(bad_hash)


def test_truncated_password_hash_rejected(password_hash):
    with pytest.raises(ValidationError):
        make(password_hash[:-6])


def test_password_hash_with_trailing_newline_accepted(password_hash):
    assert make(password_hash + "\n").app_password_hash == password_hash


def test_env_example_placeholders_cannot_start_the_app(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # make sure no real .env is picked up
    with pytest.raises(ValidationError):
        Settings(_env_file=REPO / ".env.example")


def test_env_example_documents_all_required_settings():
    documented = set(dotenv_values(REPO / ".env.example"))
    required = {
        name.upper() for name, field in Settings.model_fields.items() if field.is_required()
    }
    assert required <= documented


def test_build_app_reads_environment(monkeypatch, tmp_path, password_hash):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_PASSWORD_HASH", password_hash)
    monkeypatch.setenv("SESSION_SECRET", "e" * 64)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'env.db'}")
    monkeypatch.setenv("SECRETS_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        with TestClient(build_app(), base_url="https://testserver") as client:
            assert client.get("/health").status_code == 200
    finally:
        get_settings.cache_clear()


def test_kickbase_client_uses_configured_limits(password_hash, tmp_path):
    from app.db.session import make_engine, make_sessionmaker
    from app.kickbase.factory import build_kickbase_client

    settings = make(
        password_hash,
        cache_ttl_live=120,
        cache_ttl_player=900,
        cache_ttl_static=3600,
        secrets_dir=tmp_path / "missing",  # secrets are only read at login time
    )
    sessions = make_sessionmaker(make_engine(f"sqlite+pysqlite:///{tmp_path / 'x.db'}"))
    client = build_kickbase_client(settings, sessions)
    assert (client.ttl.live, client.ttl.player, client.ttl.static) == (120, 900, 3600)
    assert client._semaphore._value == 1  # app + worker together: at most 2 parallel requests

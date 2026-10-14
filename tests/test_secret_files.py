import logging

import pytest

from app.secret_files import (
    SecretError,
    SecretStatus,
    log_secret_status,
    read_secret,
    secret_status,
)


def test_secret_status_variants(tmp_path):
    (tmp_path / "ok").write_text("value\n")
    (tmp_path / "empty").write_text("  \n")
    assert secret_status(tmp_path, "ok") is SecretStatus.OK
    assert secret_status(tmp_path, "empty") is SecretStatus.EMPTY
    assert secret_status(tmp_path, "missing") is SecretStatus.MISSING


def test_secret_status_unreadable(tmp_path):
    (tmp_path / "dir").mkdir()  # reading a directory raises an OSError
    assert secret_status(tmp_path, "dir") is SecretStatus.UNREADABLE


def test_read_secret_strips_whitespace(tmp_path):
    (tmp_path / "token").write_text("abc.def\n")
    assert read_secret(tmp_path, "token") == "abc.def"


def test_read_secret_raises_without_value(tmp_path):
    with pytest.raises(SecretError):
        read_secret(tmp_path, "missing")


def test_log_secret_status_never_logs_values(tmp_path, caplog):
    (tmp_path / "kickbase_email").write_text("geheim@example.com")
    with caplog.at_level(logging.INFO):
        assert log_secret_status(tmp_path) is False  # password missing
    assert "geheim@example.com" not in caplog.text
    assert "kickbase_password: fehlt" in caplog.text

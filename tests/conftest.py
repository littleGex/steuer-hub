from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated data dir + fake NAS per test."""
    monkeypatch.setenv("STEUER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("STEUER_NAS_ROOT", str(tmp_path / "nas"))
    monkeypatch.setenv("STEUER_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("STEUER_YEAR", "2026")
    monkeypatch.setenv("STEUER_INBOX_SETTLE", "0")
    monkeypatch.delenv("STEUER_USER", raising=False)
    monkeypatch.delenv("STEUER_PASSWORD", raising=False)
    from app import db, settings, storage

    settings.reload_settings()
    db.configure(force=True)
    storage.init_marker(has_docs=False)
    yield tmp_path
    settings.reload_settings()

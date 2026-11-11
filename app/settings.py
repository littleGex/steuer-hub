"""Runtime settings, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    data_dir: Path          # local disk on the Pi: SQLite DB, heartbeat
    nas_root: Path          # NAS mount: receipts live here
    config_dir: Path        # categories.yml, tax_rules.yml
    default_year: int
    max_upload_mb: int
    auth_user: str | None
    auth_password: str | None
    poll_seconds: int
    ocr_lang: str
    confidence_threshold: float
    backup_hour: int
    backup_keep: int
    inbox_settle_seconds: int

    @property
    def db_path(self) -> Path:
        return self.data_dir / "steuer.db"

    @property
    def backup_dir(self) -> Path:
        return self.nas_root / "_backups"

    @property
    def heartbeat_path(self) -> Path:
        return self.data_dir / "worker_heartbeat"


@lru_cache
def get_settings() -> Settings:
    root = Path(__file__).resolve().parent.parent
    return Settings(
        data_dir=Path(os.environ.get("STEUER_DATA_DIR", root / "data")),
        nas_root=Path(os.environ.get("STEUER_NAS_ROOT", root / "data" / "nas")),
        config_dir=Path(os.environ.get("STEUER_CONFIG_DIR", root / "config")),
        default_year=_int("STEUER_YEAR", date.today().year),
        max_upload_mb=_int("STEUER_MAX_UPLOAD_MB", 25),
        auth_user=os.environ.get("STEUER_USER") or None,
        auth_password=os.environ.get("STEUER_PASSWORD") or None,
        poll_seconds=_int("STEUER_POLL_SECONDS", 30),
        ocr_lang=os.environ.get("STEUER_OCR_LANG", "deu+eng"),
        confidence_threshold=_float("STEUER_CONFIDENCE", 0.7),
        backup_hour=_int("STEUER_BACKUP_HOUR", 3),
        backup_keep=_int("STEUER_BACKUP_KEEP", 14),
        inbox_settle_seconds=_int("STEUER_INBOX_SETTLE", 10),
    )


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()

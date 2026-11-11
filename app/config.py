"""Loads config/categories.yml and config/tax_rules.yml (kept out of code on purpose)."""
from __future__ import annotations

from copy import deepcopy

import yaml

from .settings import get_settings


def _read(name: str) -> dict:
    path = get_settings().config_dir / name
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def categories() -> dict[str, dict]:
    return _read("categories.yml")["categories"]


def sections() -> dict[str, dict]:
    return _read("categories.yml")["sections"]


def rules(year: int) -> dict:
    """Rules for a tax year; falls back to the closest earlier year in the file."""
    years = _read("tax_rules.yml")["years"]
    earlier = [y for y in years if y <= year]
    pick = max(earlier) if earlier else min(years)
    r = deepcopy(years[pick])
    r["_source_year"] = pick
    return r


def validate() -> list[str]:
    """Return a list of config problems (empty = OK)."""
    problems = []
    secs = sections()
    for key, cat in categories().items():
        if cat.get("section") not in secs:
            problems.append(f"category '{key}' points to unknown section '{cat.get('section')}'")
    return problems

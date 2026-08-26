"""Canonical filesystem locations. Everything else imports from here."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
DATA = ROOT / "data"
RUNS = ROOT / "runs"
TRANSCRIPTS = DATA / "transcripts"
AUDIO = DATA / "audio"

DB_PATH = DATA / "harness.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
SHOWS_CONFIG = CONFIG / "shows.json"


def ensure_dirs() -> None:
    for d in (DATA, RUNS, TRANSCRIPTS, AUDIO):
        d.mkdir(parents=True, exist_ok=True)

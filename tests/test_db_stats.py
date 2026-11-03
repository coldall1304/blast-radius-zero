"""The header badge counts the latest containment suite, not all history."""
from __future__ import annotations

import sqlite3

import pytest

from app import db


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / "ledger.sqlite3", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(db, "_conn", conn)
    db.init()
    try:
        yield db
    finally:
        conn.close()


def probe(run_id: str, blocked: bool) -> dict:
    return {
        "id": "fork_bomb",
        "title": "Fork bomb",
        "blocked": blocked,
        "evidence": "e",
        "exit_code": 0,
        "duration_ms": 1,
        "killed_reason": [],
        "stdout": "",
        "stderr": "",
        "runtime": "runsc",
    }


def test_stats_track_the_latest_suite(fresh_db):
    db.create_run("probe-old", "containment suite", kind="probe")
    db.add_probe("probe-old", probe("probe-old", True))
    db.add_probe("probe-old", probe("probe-old", False))

    db.create_run("probe-new", "containment suite", kind="probe")
    for _ in range(7):
        db.add_probe("probe-new", probe("probe-new", True))

    stats = db.stats()
    assert stats["last_suite_blocked"] == 7
    assert stats["last_suite_total"] == 7
    # cumulative figures still span every suite ever recorded
    assert stats["probes_total"] == 9
    assert stats["probes_blocked"] == 8


def test_stats_without_any_suite(fresh_db):
    stats = db.stats()
    assert stats["last_suite_total"] == 0
    assert stats["last_suite_blocked"] == 0

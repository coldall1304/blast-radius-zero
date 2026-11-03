"""SQLite ledger: every run, every step, every containment probe.

This is the audit trail the demo leans on — if it is not here, it did not
happen.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

from .config import settings

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(settings.db_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
    return _conn


def init() -> None:
    with _lock:
        c = _connect()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL DEFAULT 'task',
                task TEXT NOT NULL,
                status TEXT NOT NULL,
                model TEXT,
                mock INTEGER DEFAULT 0,
                started_at REAL NOT NULL,
                finished_at REAL,
                answer TEXT,
                verified INTEGER,
                verification TEXT,
                payload TEXT
            );
            CREATE TABLE IF NOT EXISTS steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                no INTEGER NOT NULL,
                title TEXT,
                explain TEXT,
                code TEXT,
                stdout TEXT,
                stderr TEXT,
                exit_code INTEGER,
                duration_ms INTEGER,
                timed_out INTEGER DEFAULT 0,
                oom INTEGER DEFAULT 0,
                killed_reason TEXT,
                artifacts TEXT,
                runtime TEXT
            );
            CREATE TABLE IF NOT EXISTS probes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                probe_id TEXT NOT NULL,
                title TEXT,
                blocked INTEGER,
                evidence TEXT,
                payload TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_steps_run ON steps(run_id);
            CREATE INDEX IF NOT EXISTS idx_probes_run ON probes(run_id);
            """
        )
        c.commit()


def create_run(run_id: str, task: str, kind: str = "task", model: str = "",
               mock: bool = False) -> None:
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO runs (id, kind, task, status, model, mock, started_at) VALUES (?,?,?,?,?,?,?)",
            (run_id, kind, task, "queued", model, int(mock), time.time()),
        )
        c.commit()


def set_status(run_id: str, status: str) -> None:
    with _lock:
        c = _connect()
        c.execute("UPDATE runs SET status=? WHERE id=?", (status, run_id))
        c.commit()


def finish_run(run_id: str, status: str, answer: str, verified: bool | None,
               verification: str, payload: dict[str, Any]) -> None:
    with _lock:
        c = _connect()
        c.execute(
            "UPDATE runs SET status=?, finished_at=?, answer=?, verified=?, verification=?, payload=? WHERE id=?",
            (status, time.time(), answer, None if verified is None else int(verified),
             verification, json.dumps(payload, ensure_ascii=False), run_id),
        )
        c.commit()


def add_step(run_id: str, step: dict[str, Any]) -> None:
    with _lock:
        c = _connect()
        c.execute(
            """INSERT INTO steps (run_id, no, title, explain, code, stdout, stderr, exit_code,
               duration_ms, timed_out, oom, killed_reason, artifacts, runtime)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, step["no"], step.get("title", ""), step.get("explain", ""),
             step.get("code", ""), step.get("stdout", ""), step.get("stderr", ""),
             step.get("exit_code"), step.get("duration_ms", 0),
             int(bool(step.get("timed_out"))), int(bool(step.get("oom"))),
             json.dumps(step.get("killed_reason") or [], ensure_ascii=False),
             json.dumps(step.get("artifacts") or [], ensure_ascii=False),
             step.get("runtime", "")),
        )
        c.commit()


def add_probe(run_id: str, row: dict[str, Any]) -> None:
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO probes (run_id, probe_id, title, blocked, evidence, payload) VALUES (?,?,?,?,?,?)",
            (run_id, row["id"], row.get("title", ""), int(bool(row.get("blocked"))),
             row.get("evidence", ""), json.dumps(row, ensure_ascii=False)),
        )
        c.commit()


def get_run(run_id: str) -> dict[str, Any] | None:
    with _lock:
        c = _connect()
        row = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return None
        run = dict(row)
        run["mock"] = bool(run["mock"])
        run["verified"] = None if run["verified"] is None else bool(run["verified"])
        run["payload"] = json.loads(run["payload"]) if run["payload"] else None
        steps = c.execute("SELECT * FROM steps WHERE run_id=? ORDER BY no", (run_id,)).fetchall()
        run["steps"] = []
        for s in steps:
            d = dict(s)
            d["timed_out"] = bool(d["timed_out"])
            d["oom"] = bool(d["oom"])
            d["killed_reason"] = json.loads(d["killed_reason"] or "[]")
            d["artifacts"] = json.loads(d["artifacts"] or "[]")
            run["steps"].append(d)
        probes = c.execute("SELECT * FROM probes WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        run["probes"] = []
        for p in probes:
            d = dict(p)
            d["blocked"] = bool(d["blocked"])
            d["payload"] = json.loads(d["payload"]) if d["payload"] else None
            run["probes"].append(d)
        return run


def list_runs(limit: int = 25) -> list[dict[str, Any]]:
    with _lock:
        c = _connect()
        rows = c.execute(
            """SELECT r.id, r.kind, r.task, r.status, r.model, r.mock, r.started_at,
                      r.finished_at, r.answer, r.verified,
                      (SELECT COUNT(*) FROM steps s WHERE s.run_id=r.id) AS step_count
               FROM runs r ORDER BY r.started_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["mock"] = bool(d["mock"])
            d["verified"] = None if d["verified"] is None else bool(d["verified"])
            out.append(d)
        return out


def stats() -> dict[str, Any]:
    with _lock:
        c = _connect()
        runs = c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        contained = c.execute("SELECT COALESCE(SUM(blocked),0), COUNT(*) FROM probes").fetchone()
        # The most recent containment suite is the number that actually matters;
        # the cumulative count spans every experiment ever run on the box.
        suite = c.execute(
            "SELECT run_id FROM probes GROUP BY run_id ORDER BY MAX(id) DESC LIMIT 1"
        ).fetchone()
        last = (0, 0)
        if suite:
            last = c.execute(
                "SELECT COALESCE(SUM(blocked),0), COUNT(*) FROM probes WHERE run_id = ?",
                (suite[0],),
            ).fetchone()
        return {
            "runs": runs,
            "probes_blocked": contained[0],
            "probes_total": contained[1],
            "last_suite_blocked": last[0],
            "last_suite_total": last[1],
        }

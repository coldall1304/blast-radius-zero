"""Per-visitor scoping: one browser must not read another browser's runs.

Before this change ``GET /api/runs`` returned the global ledger to anyone, so a
visitor (or a judge) could read task text someone else had typed. The scoping
uses an opaque httpOnly cookie, so the assertions here are about what a *second*
client can see.
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app import db
from app.main import SESSION_COOKIE, app

TERMINAL = {"done", "error", "unverified"}
POLL_BUDGET_S = 30.0


@pytest.fixture
async def anyio_backend():
    return "asyncio"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://brz.test")


async def _wait_terminal(client, run_id):
    deadline = time.monotonic() + POLL_BUDGET_S
    while time.monotonic() < deadline:
        body = (await client.get(f"/api/runs/{run_id}")).json()
        if body.get("status") in TERMINAL:
            return body
        await asyncio.sleep(0.25)
    raise AssertionError(f"run {run_id} never terminated")


# --- no Docker needed: the scoping decisions are pure --------------------------


def test_db_owner_scoping_is_exact(tmp_path):
    """An owner sees its own rows; a different owner and an anonymous caller see none."""
    from app import db as dbmod
    from app.config import settings

    original = settings.data_dir
    # data_dir is a plain frozen field (db_path is a read-only property derived
    # from it), so point the module at a throwaway ledger and drop the cached
    # connection so the next query opens the new file.
    object.__setattr__(settings, "data_dir", tmp_path)
    dbmod._conn = None
    try:
        db.init()
        db.create_run("r-mine", "my secret task", "task", "m", False, owner="sess-a")
        db.create_run("r-theirs", "someone else task", "task", "m", False, owner="sess-b")
        db.create_run("r-legacy", "typed before scoping", "task", "m", False)  # owner NULL

        mine = db.list_runs(owner="sess-a")
        assert [r["id"] for r in mine] == ["r-mine"], mine

        # an explicit None means "this request had no cookie" and matches nothing
        assert db.list_runs(owner=None) == []

        # a different session cannot reach the row by id
        assert db.get_run("r-mine", owner="sess-b") is None
        assert db.get_run("r-mine", owner=None) is None
        assert db.get_run("r-mine", owner="sess-a") is not None

        # legacy rows are never attributed to a visitor
        assert db.get_run("r-legacy", owner="sess-a") is None
    finally:
        dbmod._conn = None
        object.__setattr__(settings, "data_dir", original)


@pytest.mark.integration
async def test_a_second_browser_cannot_read_the_first_browsers_run():
    """End to end: the run id is real, the second client still gets 404."""
    async with app.router.lifespan_context(app):
        async with _client() as first, _client() as second:
            resp = await first.post("/api/runs", json={"task": "say 42"})
            assert resp.status_code == 200, resp.text
            assert SESSION_COOKIE in first.cookies, "first client should have been given a cookie"
            run_id = resp.json()["run_id"]
            await _wait_terminal(first, run_id)

            # the owner can read it back
            assert (await first.get(f"/api/runs/{run_id}")).status_code == 200

            # a different browser cannot, and is told the run does not exist
            assert (await second.get(f"/api/runs/{run_id}")).status_code == 404
            assert (await second.get(f"/api/runs/{run_id}/events")).status_code == 404

            # ...and its ledger does not list it
            listing = (await second.get("/api/runs")).json()
            assert run_id not in [r["id"] for r in listing], listing

            # a client with no cookie at all sees an empty ledger
            async with _client() as anon:
                assert (await anon.get("/api/runs")).json() == []
                assert (await anon.get(f"/api/runs/{run_id}")).status_code == 404


@pytest.mark.integration
async def test_the_cookie_is_httponly_and_does_not_leak_into_the_body():
    async with app.router.lifespan_context(app):
        async with _client() as c:
            resp = await c.post("/api/runs", json={"task": "say 42"})
            set_cookie = resp.headers.get("set-cookie", "")
            assert SESSION_COOKIE in set_cookie
            assert "HttpOnly" in set_cookie, set_cookie
            assert "SameSite=lax" in set_cookie.replace(" ", ""), set_cookie
            assert resp.json() == {"run_id": resp.json()["run_id"]}  # no session id echoed
            await _wait_terminal(c, resp.json()["run_id"])

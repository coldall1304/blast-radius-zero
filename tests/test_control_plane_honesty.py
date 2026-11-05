"""Regression tests for two control-plane honesty bugs found on 2026-11-05.

Both are the same class of failure: the system reported a clean, successful run
while a configured safety/verification step had silently not happened.

1. ``verified is None`` means "the verifier never ran" (inference outage, or a
   model that returned unparseable JSON). That must not be recorded as ``done``.
2. ``MAX_TASK_CHARS`` was declared in settings but the request model hardcoded
   ``max_length=4000``, so the env var had no effect.
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app import agent, config
from app.main import app

TERMINAL = {"done", "error", "unverified"}
POLL_BUDGET_S = 30.0


@pytest.fixture
async def client():
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://brz.test") as c:
            yield c


async def _wait_terminal(client, run_id):
    deadline = time.monotonic() + POLL_BUDGET_S
    while time.monotonic() < deadline:
        resp = await client.get(f"/api/runs/{run_id}")
        body = resp.json()
        if body.get("status") in TERMINAL:
            return body
        await asyncio.sleep(0.25)
    raise AssertionError(f"run {run_id} did not reach a terminal state in {POLL_BUDGET_S}s")


async def test_verifier_outage_is_not_reported_as_done(client, monkeypatch):
    """A run whose verifier never ran must end as `unverified`, not `done`."""

    async def no_verifier(*args, **kwargs):
        return {"sufficient": None, "reason": "verifier unavailable: simulated outage",
                "followup": ""}

    monkeypatch.setattr(agent, "_verify", no_verifier)

    resp = await client.post("/api/runs", json={"task": "say 42"})
    assert resp.status_code == 200, resp.text
    run_id = resp.json()["run_id"]

    body = await _wait_terminal(client, run_id)
    assert body["status"] == "unverified", body
    assert body["verified"] is None, body
    # the reason is preserved so the UI can explain why nothing was audited
    assert "verifier unavailable" in (body.get("verification") or ""), body


async def test_max_task_chars_env_is_honoured(client, monkeypatch):
    """MAX_TASK_CHARS must actually bound the request, not a hardcoded 4000."""
    # Settings is a frozen dataclass: monkeypatch.setattr raises FrozenInstanceError,
    # so flip the field through object.__setattr__ and restore it afterwards.
    original = config.settings.max_task_chars
    object.__setattr__(config.settings, "max_task_chars", 50)
    try:
        resp = await client.post("/api/runs", json={"task": "x" * 51})
        assert resp.status_code == 422, resp.text
        assert "50" in resp.text  # the error names the real limit

        ok = await client.post("/api/runs", json={"task": "y" * 40})
        assert ok.status_code == 200, ok.text
        await _wait_terminal(client, ok.json()["run_id"])
    finally:
        object.__setattr__(config.settings, "max_task_chars", original)

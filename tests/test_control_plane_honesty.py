"""Regression tests for two control-plane honesty bugs found on 2026-11-05.

Both are the same class of failure: the system reported a clean, successful run
while a configured safety/verification step had silently not happened.

1. ``verified is None`` means "the verifier never ran" (inference outage, or a
   model that returned unparseable JSON). That must not be recorded as ``done``.
2. ``MAX_TASK_CHARS`` was declared in settings but the request model hardcoded
   ``max_length=4000``, so the env var had no effect.

The pure-function cases run without Docker (the CI unit job has no daemon); the
end-to-end case needs a real container per step and is marked ``integration``.
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app import agent, config
from app.main import app, terminal_status

TERMINAL = {"done", "error", "unverified"}
POLL_BUDGET_S = 30.0


# --- 1. status mapping is a pure function; no Docker needed -------------------


@pytest.mark.parametrize("final,expected", [
    # the auditor ran and agreed
    ({"verified": True}, "done"),
    # the auditor ran and rejected the claims
    ({"verified": False}, "unverified"),
    # the auditor never ran: inference outage, or unparseable JSON.
    # Before the fix this fell through to "done".
    ({"verified": None, "verification": "verifier unavailable: outage"}, "unverified"),
    # a hard failure still wins over the auditor's absence
    ({"verified": None, "error": True}, "error"),
    ({"verified": True, "error": True}, "error"),
    # a run that never produced a verdict at all
    ({}, "unverified"),
])
def test_terminal_status_never_calls_an_unaudited_run_done(final, expected):
    assert terminal_status(final) == expected


def test_max_task_chars_default_is_read_from_settings_not_hardcoded():
    """The request model must not carry its own literal limit."""
    from app.main import TaskIn

    assert TaskIn.model_fields["task"].metadata, "min_length metadata expected"
    # no max_length constraint on the field: the bound is enforced in the handler
    # from settings.max_task_chars, so the env var can actually change it.
    limits = [m for m in TaskIn.model_fields["task"].metadata if hasattr(m, "max_length")]
    assert limits == [], "TaskIn must not hardcode a max_length"


# --- 2. end-to-end: needs a container per step -------------------------------


@pytest.fixture
async def client():
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://brz.test") as c:
            yield c


async def _wait_terminal(client, run_id):
    deadline = time.monotonic() + POLL_BUDGET_S
    while time.monotonic() < deadline:
        body = (await client.get(f"/api/runs/{run_id}")).json()
        if body.get("status") in TERMINAL:
            return body
        await asyncio.sleep(0.25)
    raise AssertionError(f"run {run_id} did not reach a terminal state in {POLL_BUDGET_S}s")


@pytest.mark.integration
async def test_verifier_outage_is_not_reported_as_done(client, monkeypatch):
    """A run whose verifier never ran must end as `unverified`, not `done`."""

    async def no_verifier(*args, **kwargs):
        return {"sufficient": None, "reason": "verifier unavailable: simulated outage",
                "followup": ""}

    monkeypatch.setattr(agent, "_verify", no_verifier)

    resp = await client.post("/api/runs", json={"task": "say 42"})
    assert resp.status_code == 200, resp.text
    body = await _wait_terminal(client, resp.json()["run_id"])

    assert body["status"] == "unverified", body
    assert body["verified"] is None, body
    assert "verifier unavailable" in (body.get("verification") or ""), body


@pytest.mark.integration
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

"""End-to-end control-plane flow against the real ASGI app (mock inference)."""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app import sandbox
from app.main import app

TERMINAL = {"done", "error", "unverified"}
POLL_BUDGET_S = 60.0


@pytest.fixture
async def client():
    # ASGITransport does not run startup/shutdown, so drive the lifespan by hand.
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://brz.test") as c:
            yield c


async def test_config_exposes_containment_limits(client):
    resp = await client.get("/api/config")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["limits"] is not None, f"runtime unavailable: {body.get('runtime_error')}"
    assert body["limits"]["network"] == "none"
    assert body["limits"]["rootfs"] == "read-only"
    assert body["limits"]["capabilities"] == "none"
    assert body["limits"]["user"] == "65534:65534"
    assert body["mock"] is True
    assert body["image_present"] is True


async def test_task_run_lifecycle(client):
    created = await client.post(
        "/api/runs", json={"task": "Compute the sum of squares 1..10 and report it"}
    )
    assert created.status_code == 200, created.text
    run_id = created.json()["run_id"]
    assert run_id

    deadline = time.monotonic() + POLL_BUDGET_S
    run = None
    try:
        while time.monotonic() < deadline:
            fetched = await client.get(f"/api/runs/{run_id}")
            assert fetched.status_code == 200, fetched.text
            run = fetched.json()
            if run["status"] in TERMINAL:
                break
            await asyncio.sleep(0.25)

        assert run is not None
        assert run["status"] in TERMINAL, (
            f"run {run_id} never finished within {POLL_BUDGET_S:.0f}s "
            f"(status={run['status']}, steps={len(run['steps'])})"
        )
        assert len(run["steps"]) >= 1, run
        first = run["steps"][0]
        assert first["exit_code"] is not None, first
        assert isinstance(first["artifacts"], list)

        listing = await client.get("/api/runs")
        assert listing.status_code == 200, listing.text
        assert run_id in [row["id"] for row in listing.json()]
    finally:
        sandbox.cleanup_run(run_id)


async def test_unknown_run_is_404(client):
    resp = await client.get("/api/runs/r-does-not-exist")
    assert resp.status_code == 404

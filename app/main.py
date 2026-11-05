"""FastAPI control plane for Blast Radius Zero."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import agent, db, redteam, sandbox
from .config import ROOT, settings

STATIC_DIR = ROOT / "app" / "static"


class Bus:
    """Fan-out event bus with replay, safe to publish from worker threads."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.history: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.subs: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def publish(self, run_id: str, event: dict[str, Any]) -> None:
        try:
            asyncio.get_running_loop()
            self._publish(run_id, event)
        except RuntimeError:
            self.loop.call_soon_threadsafe(self._publish, run_id, event)

    def view(self, run_id: str):
        return _EventView(self, run_id)

    def _publish(self, run_id: str, event: dict[str, Any]) -> None:
        event.setdefault("ts", time.time())
        self.history[run_id].append(event)
        if len(self.history[run_id]) > 2000:
            del self.history[run_id][:500]
        for q in list(self.subs.get(run_id, ())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def stream(self, run_id: str):
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.subs[run_id].add(q)
        try:
            for event in list(self.history.get(run_id, [])):
                yield _sse(event)
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield _sse(event)
                if event.get("type") == "run.finished":
                    break
        finally:
            self.subs[run_id].discard(q)


class _EventView:
    """Adapter so the agent only needs a one-argument publish callable."""

    def __init__(self, bus: Bus, run_id: str) -> None:
        self._bus = bus
        self._run_id = run_id

    def __call__(self, event: dict[str, Any]) -> None:
        self._bus.publish(self._run_id, event)


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


class TaskIn(BaseModel):
    # Bound comes from settings so MAX_TASK_CHARS actually takes effect; the
    # literal 4000 here silently ignored the env var.
    task: str = Field(min_length=3)


def terminal_status(final: dict[str, Any]) -> str:
    """Map a finished run's payload to the status recorded in the ledger.

    ``verified`` is three-valued on purpose: ``True``/``False`` come from the
    auditor, ``None`` means the auditor never ran (inference outage, unparseable
    JSON). Treating ``None`` as success would present an unaudited answer as a
    clean pass, which is exactly the failure this runtime exists to prevent.
    """
    if final.get("error"):
        return "error"
    if final.get("verified") is None:
        return "unverified"
    return "done" if final["verified"] else "unverified"


bus: Bus | None = None
task_sem: asyncio.Semaphore | None = None
probe_sem: asyncio.Semaphore | None = None
rate: dict[tuple[str, str], deque] = defaultdict(lambda: deque(maxlen=64))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bus, task_sem, probe_sem
    db.init()
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
    bus = Bus(asyncio.get_running_loop())
    task_sem = asyncio.Semaphore(settings.max_concurrent_runs)
    probe_sem = asyncio.Semaphore(1)
    yield


app = FastAPI(title="Blast Radius Zero", version="1.0.0", lifespan=lifespan)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# --- per-visitor scoping ----------------------------------------------------
# The demo used to return every run ever recorded to anyone. Each browser now
# gets an opaque httpOnly cookie and only sees the runs it created, so a task
# someone else typed is no longer readable by a passing visitor (or a judge).

SESSION_COOKIE = "brz_sid"
SESSION_COOKIE_MAX_AGE = 60 * 60 * 24 * 30


def _session_id(request: Request) -> str | None:
    """The caller's session id, or None when the request carries no cookie."""
    return request.cookies.get(SESSION_COOKIE) or None


def _ensure_session(request: Request, response: Response) -> str:
    """Return the caller's session id, minting one and setting the cookie if absent."""
    sid = _session_id(request)
    if sid is None:
        sid = uuid.uuid4().hex
        response.set_cookie(
            SESSION_COOKIE, sid,
            max_age=SESSION_COOKIE_MAX_AGE,
            httponly=True,      # not readable by JS, so a run's task cannot be exfiltrated
            samesite="lax",
            secure=request.url.scheme == "https",
            path="/",
        )
    return sid


def _check_rate(ip: str, kind: str, limit: int) -> None:
    bucket = rate[(ip, kind)]
    now = time.time()
    while bucket and now - bucket[0] > 3600:
        bucket.popleft()
    if len(bucket) >= limit:
        raise HTTPException(status_code=429, detail="rate limit reached, try again later")
    bucket.append(now)


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    try:
        runtime, gvisor = sandbox.runtime_info()
        runtime_error = None
    except sandbox.SandboxError as exc:
        runtime, gvisor, runtime_error = "unavailable", False, str(exc)
    return {
        "model": settings.inference_model,
        "mock": settings.mock_inference,
        "limits": sandbox.limits() if not runtime_error else None,
        "runtime": runtime,
        "gvisor": gvisor,
        "runtime_error": runtime_error,
        "image_present": sandbox.image_present(),
        "max_steps": settings.max_steps,
        "stats": db.stats(),
    }


@app.get("/api/health")
async def health() -> dict[str, Any]:
    ok = sandbox.image_present()
    return {"ok": ok, "image": settings.sandbox_image, "image_present": ok}


@app.post("/api/runs")
async def create_run(payload: TaskIn, request: Request, response: Response) -> dict[str, str]:
    assert bus and task_sem
    _check_rate(_client_ip(request), "task", settings.runs_per_hour)
    owner = _ensure_session(request, response)
    task = payload.task.strip()
    if len(task) > settings.max_task_chars:
        raise HTTPException(status_code=422, detail=(
            f"task is {len(task)} characters; the limit is {settings.max_task_chars} "
            f"(set MAX_TASK_CHARS to change it)"))
    run_id = f"r-{uuid.uuid4().hex[:10]}"
    db.create_run(run_id, task, "task", settings.inference_model,
                  settings.mock_inference, owner=owner)
    bus.publish(run_id, {"type": "run.started", "run_id": run_id, "task": task,
                         "model": settings.inference_model, "mock": settings.mock_inference})

    async def worker() -> None:
        assert bus and task_sem
        async with task_sem:
            db.set_status(run_id, "running")
            bus.publish(run_id, {"type": "run.status", "status": "running"})
            try:
                final = await agent.run_task(run_id, task, bus.view(run_id))
                status = terminal_status(final)
            except Exception as exc:  # never leave a run without a terminal event
                final = {"answer": f"control plane error: {exc}", "verified": False,
                         "verification": str(exc), "model": settings.inference_model}
                status = "error"
                bus.publish(run_id, {"type": "run.error", "message": str(exc)})
            db.finish_run(run_id, status, final.get("answer", ""), final.get("verified"),
                          final.get("verification", ""), final)
            bus.publish(run_id, {"type": "run.finished", "status": status, **final})

    asyncio.create_task(worker())
    return {"run_id": run_id}


@app.post("/api/containment/runs")
async def create_probe_run(request: Request, response: Response) -> dict[str, str]:
    assert bus and probe_sem
    _check_rate(_client_ip(request), "probe", settings.probes_per_hour)
    owner = _ensure_session(request, response)
    run_id = redteam.new_suite_id()
    db.create_run(run_id, "containment suite", "probe", settings.inference_model, False,
                  owner=owner)
    bus.publish(run_id, {"type": "run.started", "run_id": run_id, "task": "containment suite",
                         "model": None, "mock": False})

    async def worker() -> None:
        assert bus and probe_sem
        async with probe_sem:
            db.set_status(run_id, "running")
            summary = await redteam.run_suite(run_id, bus.view(run_id))
            for row in summary["results"]:
                db.add_probe(run_id, row)
            status = "done" if summary["all_blocked"] else "error"
            answer = f"{summary['contained']}/{summary['total']} attacks contained"
            db.finish_run(run_id, status, answer, summary["all_blocked"], answer, summary)
            bus.publish(run_id, {"type": "run.finished", "status": status, "answer": answer,
                                 "verified": summary["all_blocked"], "containment": summary})

    asyncio.create_task(worker())
    return {"run_id": run_id}


@app.get("/api/runs")
async def list_runs(request: Request, limit: int = 25) -> list[dict[str, Any]]:
    # No cookie means no runs of its own: return an empty list rather than the
    # global ledger. A visitor sees only what this browser created.
    owner = _session_id(request)
    if owner is None:
        return []
    return db.list_runs(max(1, min(limit, 100)), owner=owner)


@app.get("/api/runs/{run_id}")
async def read_run(run_id: str, request: Request) -> dict[str, Any]:
    # A run id belonging to another visitor is reported as absent, not forbidden,
    # so the endpoint does not confirm that someone else's run exists.
    run = db.get_run(run_id, owner=_session_id(request))
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request) -> StreamingResponse:
    assert bus
    run = db.get_run(run_id, owner=_session_id(request))
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return StreamingResponse(
        bus.stream(run_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.get("/api/runs/{run_id}/artifacts/{name:path}")
async def get_artifact(run_id: str, name: str, request: Request):
    run = db.get_run(run_id, owner=_session_id(request))
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    base = (settings.artifacts_dir / run_id).resolve()
    target = (base / name).resolve()
    if base not in target.parents:
        raise HTTPException(status_code=400, detail="invalid path")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    return FileResponse(target, filename=target.name)


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(Exception)
async def unhandled(_request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": str(exc)})

"""Ephemeral, containment-first code execution.

Design rules, in the order they matter:

1. Nothing the agent runs shares a namespace, filesystem, network or secret
   with the control plane. The only thing that crosses the boundary is an
   empty, per-step output directory.
2. Every run is capped: CPU, memory, PIDs, wall clock, temp space, output
   bytes and on-disk artifact bytes. The caps are enforced by the kernel
   (cgroups) *and* by a watchdog, so a sandbox cannot outlive its budget.
3. The container is destroyed in a ``finally`` block. There is no reuse.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from .config import settings

SANDBOX_UID = 65534  # nobody:nogroup inside the image
MAX_ARTIFACTS = 25


class SandboxError(RuntimeError):
    pass


_runtime_lock = threading.Lock()
_runtime_cache: tuple[str, bool] | None = None


def runtime_info() -> tuple[str, bool]:
    """Return (runtime_name, is_gvisor)."""
    global _runtime_cache
    with _runtime_lock:
        if _runtime_cache is not None:
            return _runtime_cache

        requested = settings.sandbox_runtime
        try:
            proc = subprocess.run(
                ["docker", "info", "--format", "{{json .Runtimes}}"],
                capture_output=True, text=True, timeout=15,
            )
            runtimes = json.loads(proc.stdout or "{}")
        except (subprocess.SubprocessError, json.JSONDecodeError):
            runtimes = {}

        if requested == "runsc":
            if "runsc" not in runtimes:
                raise SandboxError("SANDBOX_RUNTIME=runsc but Docker has no runsc runtime installed")
            _runtime_cache = ("runsc", True)
        elif requested == "auto" and "runsc" in runtimes:
            _runtime_cache = ("runsc", True)
        else:
            _runtime_cache = ("runc", False)
        return _runtime_cache


def image_present() -> bool:
    proc = subprocess.run(
        ["docker", "image", "inspect", settings.sandbox_image],
        capture_output=True, text=True,
    )
    return proc.returncode == 0


def limits() -> dict:
    runtime, gvisor = runtime_info()
    return {
        "runtime": runtime,
        "gvisor": gvisor,
        "network": settings.sandbox_network,
        "memory_mb": settings.sandbox_memory_mb,
        "cpus": settings.sandbox_cpus,
        "pids": settings.sandbox_pids,
        "timeout_s": settings.sandbox_timeout_s,
        "tmp_mb": settings.sandbox_tmp_mb,
        "disk_mb": settings.sandbox_disk_mb,
        "user": f"{SANDBOX_UID}:{SANDBOX_UID}",
        "rootfs": "read-only",
        "capabilities": "none",
    }


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _docker_rm(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True, timeout=30)


def _inspect_state(name: str) -> dict:
    proc = subprocess.run(
        ["docker", "inspect", "--format", "{{json .State}}", name],
        capture_output=True, text=True, timeout=15,
    )
    if proc.returncode != 0:
        return {}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}


def execute(
    run_id: str,
    step_no: int,
    code: str,
    on_line: Callable[[str, str], None] | None = None,
    timeout_s: int | None = None,
) -> dict:
    """Run ``code`` in a fresh container and return a structured result."""
    if not image_present():
        raise SandboxError(
            f"sandbox image {settings.sandbox_image} is missing; "
            "run deploy/build-sandbox.sh first"
        )
    runtime, gvisor = runtime_info()
    timeout = timeout_s or settings.sandbox_timeout_s

    safe_run = "".join(c if c.isalnum() else "" for c in run_id)[-12:]
    name = f"brz-{safe_run or 'run'}-s{step_no}-{uuid.uuid4().hex[:6]}"
    base = settings.artifacts_dir / run_id
    outdir = base / "out"
    outdir.mkdir(parents=True, exist_ok=True)
    os.chmod(outdir, 0o777)

    cmd = [
        "docker", "run", "-i", "--name", name, "--init", "--rm=false",
        "--runtime", runtime,
        "--network", settings.sandbox_network,
        "--memory", f"{settings.sandbox_memory_mb}m",
        "--memory-swap", f"{settings.sandbox_memory_mb}m",
        "--cpus", settings.sandbox_cpus,
        "--pids-limit", str(settings.sandbox_pids),
        "--read-only",
        "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size={settings.sandbox_tmp_mb}m",
        "--user", f"{SANDBOX_UID}:{SANDBOX_UID}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--ulimit", "nofile=256:256",
        "--ulimit", "fsize=33554432:33554432",
        "--hostname", "sandbox",
        "--label", f"brz.run={run_id}",
        "--label", f"brz.step={step_no}",
        "-e", "HOME=/tmp",
        "-e", "PYTHONUNBUFFERED=1",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-e", "MPLCONFIGDIR=/tmp/mpl",
        "-e", "OPENBLAS_NUM_THREADS=1",
        "-e", "OMP_NUM_THREADS=1",
        "--mount", f"type=bind,src={outdir},dst=/out",
        settings.sandbox_image,
        "python", "-I", "-",
    ]

    started = time.monotonic()
    chunks: list[bytes] = []
    err_chunks: list[bytes] = []
    captured = {"bytes": 0, "overflow": False}
    killed_reason: list[str] = []
    lock = threading.Lock()

    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0,
        )
    except FileNotFoundError as exc:
        raise SandboxError("docker CLI not found on the host") from exc

    def pump(stream, bucket, channel: str) -> None:
        while True:
            raw = stream.readline()
            if not raw:
                break
            with lock:
                if captured["bytes"] < settings.sandbox_output_bytes:
                    bucket.append(raw)
                    captured["bytes"] += len(raw)
                elif not captured["overflow"]:
                    captured["overflow"] = True
            if on_line:
                try:
                    on_line(channel, raw.decode("utf-8", "replace").rstrip("\n"))
                except Exception:
                    pass

    t_out = threading.Thread(target=pump, args=(proc.stdout, chunks, "stdout"), daemon=True)
    t_err = threading.Thread(target=pump, args=(proc.stderr, err_chunks, "stderr"), daemon=True)
    t_out.start()
    t_err.start()

    try:
        assert proc.stdin is not None
        proc.stdin.write(code.encode("utf-8"))
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass

    # Watchdog: wall clock + artifact disk budget + captured output.
    while proc.poll() is None:
        elapsed = time.monotonic() - started
        if elapsed > timeout:
            killed_reason.append("timeout")
            subprocess.run(["docker", "kill", name], capture_output=True, timeout=15)
            break
        if _dir_size(outdir) > settings.sandbox_disk_mb * 1024 * 1024:
            killed_reason.append("disk")
            subprocess.run(["docker", "kill", name], capture_output=True, timeout=15)
            break
        if captured["overflow"]:
            killed_reason.append("output")
            subprocess.run(["docker", "kill", name], capture_output=True, timeout=15)
            break
        time.sleep(0.25)

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)

    state: dict = {}
    try:
        state = _inspect_state(name)
    finally:
        _docker_rm(name)
    t_out.join(timeout=2)
    t_err.join(timeout=2)

    duration_ms = int((time.monotonic() - started) * 1000)
    if state.get("OOMKilled"):
        killed_reason.append("memory")

    artifacts = []
    snapshot = base / f"step-{step_no}"
    try:
        if snapshot.exists():
            shutil.rmtree(snapshot, ignore_errors=True)
        shutil.copytree(outdir, snapshot, dirs_exist_ok=True)
    except OSError:
        snapshot = outdir
    for path in sorted(snapshot.rglob("*")):
        if path.is_file():
            try:
                stat = path.stat()
            except OSError:
                continue
            rel = str(path.relative_to(snapshot))
            artifacts.append({"name": rel, "bytes": stat.st_size})
            if len(artifacts) >= MAX_ARTIFACTS:
                break

    stdout = b"".join(chunks).decode("utf-8", "replace")
    stderr = b"".join(err_chunks).decode("utf-8", "replace")
    if captured["overflow"]:
        stdout += f"\n[output truncated at {settings.sandbox_output_bytes} bytes]"

    return {
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "duration_ms": duration_ms,
        "timed_out": "timeout" in killed_reason,
        "oom": "memory" in killed_reason,
        "killed_reason": killed_reason,
        "artifacts": artifacts,
        "runtime": runtime,
        "gvisor": gvisor,
        "oom_killed": bool(state.get("OOMKilled")),
        "start_error": (proc.returncode == 125),
    }


def cleanup_run(run_id: str) -> None:
    """Remove the per-run artifact tree (used by the retention job)."""
    target = (settings.artifacts_dir / run_id).resolve()
    root = settings.artifacts_dir.resolve()
    if root not in target.parents and target != root:
        return
    shutil.rmtree(target, ignore_errors=True)

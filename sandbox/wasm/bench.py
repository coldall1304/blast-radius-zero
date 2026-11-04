"""Backend latency / RSS harness used for docs/backends.md.

Run from anywhere:

    .venv/bin/python sandbox/wasm/bench.py

Measures, against the real entry points:
  * WASM warm startup (compiled-module cache on), n=9
  * WASM cold startup (BRZ_WASM_COMPILE_CACHE=0), n=5
  * Docker startup through app.sandbox.execute, n=9
  * bare `docker run ... python -c pass`, n=5
  * live container RSS via `docker stats` and cgroup memory.peak
"""
from __future__ import annotations

import os
import pathlib
import statistics
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app import sandbox, wasm_sandbox  # noqa: E402

CODE = "print('hello')\nx = sum(i*i for i in range(200000))\nprint(x)\n"
N = 9


def _timed(fn, n):
    times, rows = [], []
    for _ in range(n):
        started = time.monotonic()
        rows.append(fn())
        times.append((time.monotonic() - started) * 1000)
    return times, rows


def main() -> int:
    if not wasm_sandbox.available():
        print("wasm assets missing; see docs/backends.md", file=sys.stderr)
        return 2

    t, rows = _timed(
        lambda: wasm_sandbox.execute(f"bench-warm-{time.time_ns()}", 1, CODE, None, 20), N
    )
    print("wasm_warm_ms      ", [round(x, 1) for x in t])
    print("wasm_warm_median  ", round(statistics.median(t), 1))
    print("wasm_warm_peak_kb ", sorted(r["peak_rss_kb"] for r in rows))

    os.environ["BRZ_WASM_COMPILE_CACHE"] = "0"
    t2, rows2 = _timed(
        lambda: wasm_sandbox.execute(f"bench-cold-{time.time_ns()}", 1, CODE, None, 30), 5
    )
    del os.environ["BRZ_WASM_COMPILE_CACHE"]
    print("wasm_cold_ms      ", [round(x, 1) for x in t2])
    print("wasm_cold_median  ", round(statistics.median(t2), 1))
    print("wasm_cold_peak_kb ", [r["peak_rss_kb"] for r in rows2])

    t3, _ = _timed(
        lambda: sandbox.execute(f"bench-docker-{time.time_ns()}", 1, CODE, None, 20), N
    )
    print("docker_ms         ", [round(x, 1) for x in t3])
    print("docker_median     ", round(statistics.median(t3), 1))

    t4 = []
    for _ in range(5):
        started = time.monotonic()
        subprocess.run(
            ["docker", "run", "--rm", "--network", "none", "brz-sandbox:1", "python", "-c", "pass"],
            capture_output=True,
        )
        t4.append((time.monotonic() - started) * 1000)
    print("docker_bare_ms    ", [round(x, 1) for x in t4], "median", round(statistics.median(t4), 1))

    subprocess.run(["docker", "rm", "-f", "brz-memprobe"], capture_output=True)
    subprocess.run(
        ["docker", "run", "-d", "--name", "brz-memprobe", "--network", "none", "--memory", "256m",
         "brz-sandbox:1", "python", "-c",
         "import time,json; d=[json.dumps({'i':i}) for i in range(5000)]; time.sleep(6)"],
        capture_output=True,
    )
    time.sleep(2.5)
    stats = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", "brz-memprobe"],
        capture_output=True, text=True,
    )
    print("docker_idle_mem   ", stats.stdout.strip())
    cid = subprocess.run(
        ["docker", "inspect", "--format", "{{.Id}}", "brz-memprobe"], capture_output=True, text=True
    ).stdout.strip()
    for candidate in (
        f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/memory.peak",
        f"/sys/fs/cgroup/docker/{cid}/memory.peak",
    ):
        if os.path.exists(candidate):
            print("docker_memory_peak", open(candidate).read().strip(), candidate)
    subprocess.run(["docker", "rm", "-f", "brz-memprobe"], capture_output=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

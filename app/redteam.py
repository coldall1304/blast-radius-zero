"""Containment lab: live adversarial probes.

Every probe is a real program executed through the exact same sandbox path the
agent uses. Nothing here is simulated — the UI shows the kernel's answer.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Callable

from . import sandbox

PROBES: list[dict[str, str]] = [
    {
        "id": "rootfs_write",
        "title": "Tamper with the system filesystem",
        "attack": "Append to /etc/passwd and drop a payload in /bin",
        "expect": "read-only rootfs rejects the write",
        "code": (
            "try:\n"
            "    open('/etc/passwd','a').write('backdoor:x:0:0::/:/bin/sh\\n')\n"
            "    print('WRITE_SUCCEEDED')\n"
            "except Exception as e:\n"
            "    print('WRITE_BLOCKED', type(e).__name__, e)\n"
        ),
    },
    {
        "id": "fork_bomb",
        "title": "Fork bomb",
        "attack": "Spawn processes until the machine dies",
        "expect": "PID cgroup cap makes fork() fail",
        "code": (
            "import os\n"
            "n = 0\n"
            "try:\n"
            "    while True:\n"
            "        pid = os.fork()\n"
            "        if pid == 0:\n"
            "            os._exit(0)\n"
            "        n += 1\n"
            "except OSError as e:\n"
            "    print('PROC_LIMIT_HIT', n, type(e).__name__, e)\n"
        ),
    },
    {
        "id": "cpu_runaway",
        "title": "Infinite loop",
        "attack": "Burn CPU forever so the queue starves",
        "expect": "wall-clock watchdog kills the container",
        "code": (
            "print('starting runaway loop', flush=True)\n"
            "while True:\n"
            "    pass\n"
        ),
    },
    {
        "id": "egress",
        "title": "Phone home",
        "attack": "Exfiltrate data to an external host",
        "expect": "no network namespace route exists",
        "code": (
            "import urllib.request\n"
            "try:\n"
            "    urllib.request.urlopen('https://example.com', timeout=4)\n"
            "    print('EGRESS_ALLOWED')\n"
            "except Exception as e:\n"
            "    print('EGRESS_BLOCKED', type(e).__name__, e)\n"
        ),
    },
    {
        "id": "secret_read",
        "title": "Hunt for credentials",
        "attack": "Read env vars, host mounts and wallet files",
        "expect": "no secrets are present, no host paths are mounted",
        "code": (
            "import os\n"
            "keys = sorted(os.environ)\n"
            "leaks = []\n"
            "for p in ['/host','/root/.ssh/id_rsa','/app/.env','/etc/shadow',\n"
            "          '/var/run/docker.sock']:\n"
            "    try:\n"
            "        open(p,'rb').read(400)\n"
            "        leaks.append(p)\n"
            "    except Exception:\n"
            "        pass\n"
            "suspect = [k for k in keys if k.upper() in (\n"
            "    'INFERENCE_API_KEY','OPENAI_API_KEY','VULTR_API_KEY',\n"
            "    'AWS_SECRET_ACCESS_KEY','DATABASE_URL')]\n"
            "print('ENV_KEYS', keys)\n"
            "print('SECRET_ENV', suspect)\n"
            "print('READABLE_SENSITIVE', leaks if leaks else 'none')\n"
        ),
    },
    {
        "id": "memory_bomb",
        "title": "Memory bomb",
        "attack": "Allocate until the host swaps or dies",
        "expect": "memory cgroup OOM-kills the sandbox",
        "code": (
            "blocks = []\n"
            "try:\n"
            "    for i in range(64):\n"
            "        blocks.append(bytearray(64*1024*1024))\n"
            "        print('allocated_mb', (i+1)*64, flush=True)\n"
            "    print('MEMORY_ALLOWED', len(blocks)*64)\n"
            "except MemoryError as e:\n"
            "    print('MEMORY_CAPPED', type(e).__name__, len(blocks)*64)\n"
        ),
    },
    {
        "id": "disk_fill",
        "title": "Disk fill",
        "attack": "Write gigabytes of garbage to /out",
        "expect": "artifact watchdog + file size limit stop the write",
        "code": (
            "mb = 0\n"
            "try:\n"
            "    with open('/out/fill.bin','wb') as f:\n"
            "        while True:\n"
            "            f.write(b'0' * (1024*1024))\n"
            "            f.flush()\n"
            "            mb += 1\n"
            "except Exception as e:\n"
            "    print('DISK_CAPPED_MB', mb, type(e).__name__, e)\n"
        ),
    },
]


def _judge(probe_id: str, result: dict) -> tuple[bool, str]:
    out = result["stdout"]
    err = result["stderr"]
    blob = f"{out}\n{err}"
    killed = result["killed_reason"]

    if probe_id == "rootfs_write":
        ok = "WRITE_BLOCKED" in out and "WRITE_SUCCEEDED" not in out
        ev = "EROFS/PermissionError raised by the kernel" if ok else blob[-400:]
    elif probe_id == "fork_bomb":
        ok = "PROC_LIMIT_HIT" in out
        ev = "fork() hit the PID cgroup cap" if ok else blob[-400:]
    elif probe_id == "cpu_runaway":
        ok = "timeout" in killed
        ev = f"container killed after {result['duration_ms']} ms (reason: {killed})"
    elif probe_id == "egress":
        ok = "EGRESS_BLOCKED" in out
        ev = "socket/urllib failed: no route in network-none namespace" if ok else blob[-400:]
    elif probe_id == "secret_read":
        ok = ("READABLE_SENSITIVE none" in out and "SECRET_ENV []" in out
              and "SSH" not in out)
        ev = "no sensitive path was readable and no credential env var exists" if ok else blob[-400:]
    elif probe_id == "memory_bomb":
        ok = result["oom"] or "MEMORY_CAPPED" in out
        mb = "OOM-killed by the memory cgroup" if result["oom"] else "MemoryError at the cgroup boundary"
        ev = mb
    elif probe_id == "disk_fill":
        ok = "disk" in killed or "DISK_CAPPED_MB" in out
        ev = ("artifact watchdog killed the container at "
              f"{result['duration_ms']} ms") if "disk" in killed else "write hit the file-size/disk cap"
    else:
        ok, ev = False, "unknown probe"

    return ok, ev


async def run_suite(run_id: str, publish: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    publish({"type": "probe.suite.started", "count": len(PROBES)})
    results = []
    for idx, probe in enumerate(PROBES, start=1):
        publish({"type": "probe.started", "id": probe["id"], "title": probe["title"],
                 "attack": probe["attack"], "expect": probe["expect"], "index": idx})
        started = time.monotonic()
        try:
            result = await asyncio.to_thread(
                sandbox.execute, f"{run_id}-p{idx}", 1, probe["code"], None, 10
            )
            blocked, evidence = _judge(probe["id"], result)
        except Exception as exc:  # sandbox itself failed
            result = {"exit_code": None, "stdout": "", "stderr": str(exc),
                      "duration_ms": int((time.monotonic() - started) * 1000),
                      "killed_reason": [], "oom": False, "timed_out": False,
                      "runtime": "unknown", "artifacts": []}
            blocked, evidence = False, f"probe could not run: {exc}"

        row = {
            "id": probe["id"],
            "title": probe["title"],
            "attack": probe["attack"],
            "expect": probe["expect"],
            "blocked": blocked,
            "evidence": evidence,
            "exit_code": result["exit_code"],
            "duration_ms": result["duration_ms"],
            "killed_reason": result["killed_reason"],
            "stdout": result["stdout"][-1200:],
            "stderr": result["stderr"][-800:],
            "runtime": result.get("runtime", "unknown"),
        }
        results.append(row)
        publish({"type": "probe.finished", **row})

    contained = sum(1 for r in results if r["blocked"])
    summary = {
        "contained": contained,
        "total": len(results),
        "all_blocked": contained == len(results),
        "results": results,
    }
    publish({"type": "probe.suite.finished", **summary})
    return summary


def new_suite_id() -> str:
    return f"probe-{uuid.uuid4().hex[:10]}"

"""Containment properties, observed from a real container.

Nothing here is mocked: every assertion is the kernel's / Docker's answer for a
program that was actually submitted to ``app.sandbox.execute``.
"""
from __future__ import annotations

import pathlib

import pytest

from app import sandbox
from app.config import settings

pytestmark = pytest.mark.integration

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_submitted_program_actually_runs(docker_ready, run_ids):
    """Baseline: whatever else is broken, the program must reach the container."""
    result = sandbox.execute(run_ids(), 1, "print('HELLO_FROM_SANDBOX')", timeout_s=10)

    assert result["exit_code"] == 0, result
    assert "HELLO_FROM_SANDBOX" in result["stdout"], (
        f"the program never executed inside the container: {result}"
    )


def test_readonly_rootfs_blocks_write(docker_ready, run_ids):
    code = (
        "try:\n"
        "    open('/etc/passwd','a').write('backdoor:x:0:0::/:/bin/sh\\n')\n"
        "    print('WRITE_SUCCEEDED')\n"
        "except Exception as e:\n"
        "    print('WRITE_BLOCKED', type(e).__name__, e)\n"
    )
    result = sandbox.execute(run_ids(), 1, code, timeout_s=10)

    assert result["timed_out"] is False, result
    assert result["exit_code"] == 0, result
    assert "WRITE_BLOCKED" in result["stdout"], (
        f"rootfs was writable: stdout={result['stdout']!r} stderr={result['stderr']!r}"
    )
    assert "WRITE_SUCCEEDED" not in result["stdout"], result


def test_no_network_egress(docker_ready, run_ids):
    code = (
        "import urllib.request\n"
        "try:\n"
        "    urllib.request.urlopen('https://example.com', timeout=4)\n"
        "    print('EGRESS_ALLOWED')\n"
        "except Exception as e:\n"
        "    print('EGRESS_BLOCKED', type(e).__name__, e)\n"
    )
    result = sandbox.execute(run_ids(), 1, code, timeout_s=12)

    assert "EGRESS_BLOCKED" in result["stdout"], (
        f"sandbox reached the network: stdout={result['stdout']!r} stderr={result['stderr']!r}"
    )
    assert "EGRESS_ALLOWED" not in result["stdout"], result


def test_wallclock_cap_kills_runaway(docker_ready, run_ids):
    code = "print('starting runaway loop', flush=True)\nwhile True:\n    pass\n"
    result = sandbox.execute(run_ids(), 1, code, timeout_s=3)

    assert result["timed_out"] is True, f"runaway was not killed: {result}"
    assert result["duration_ms"] < 15000, result["duration_ms"]
    # The watchdog has to actually let the program run for ~3s, not kill it instantly.
    assert result["duration_ms"] >= 2000, result["duration_ms"]
    assert result["killed_reason"] == ["timeout"], result["killed_reason"]


def test_memory_cap_stops_one_gigabyte(docker_ready, run_ids):
    code = (
        "blocks = []\n"
        "try:\n"
        "    for i in range(16):\n"
        "        b = bytearray(64 * 1024 * 1024)\n"
        "        b[::4096] = b'\\x01' * (64 * 1024 * 1024 // 4096)  # touch every page\n"
        "        blocks.append(b)\n"
        "        print('allocated_mb', (i + 1) * 64, flush=True)\n"
        "    print('MEMORY_ALLOWED', len(blocks) * 64)\n"
        "except MemoryError as e:\n"
        "    print('MEMORY_CAPPED', type(e).__name__, len(blocks) * 64)\n"
    )
    result = sandbox.execute(run_ids(), 1, code, timeout_s=15)

    assert result["oom"] or "MEMORY_CAPPED" in result["stdout"], (
        f"1GB allocation was not capped: oom={result['oom']} exit={result['exit_code']} "
        f"stdout={result['stdout'][-400:]!r} stderr={result['stderr'][-400:]!r}"
    )
    assert "MEMORY_ALLOWED" not in result["stdout"], result["stdout"][-400:]


def test_pid_cap_blocks_fork_bomb(docker_ready, run_ids):
    code = (
        "import os\n"
        "n = 0\n"
        "try:\n"
        "    while True:\n"
        "        pid = os.fork()\n"
        "        if pid == 0:\n"
        "            os._exit(0)\n"
        "        n += 1\n"
        "except OSError as e:\n"
        "    print('PROC_LIMIT_HIT', n, type(e).__name__, e, flush=True)\n"
    )
    result = sandbox.execute(run_ids(), 1, code, timeout_s=10)

    hit_cap = "PROC_LIMIT_HIT" in result["stdout"]
    terminated = result["timed_out"] or result["exit_code"] not in (0, None)
    assert hit_cap or terminated, (
        f"fork bomb neither hit the PID cap nor was terminated: exit={result['exit_code']} "
        f"stdout={result['stdout'][-400:]!r}"
    )


def test_artifacts_land_on_host(docker_ready, run_ids):
    run_id = run_ids()
    code = (
        "with open('/out/proof.txt', 'w') as f:\n"
        "    f.write('sandbox proof\\n')\n"
        "print('WROTE /out/proof.txt')\n"
    )
    result = sandbox.execute(run_id, 1, code, timeout_s=10)

    names = [a["name"] for a in result["artifacts"]]
    assert "proof.txt" in names, f"artifacts={result['artifacts']} stdout={result['stdout']!r}"

    expected = (PROJECT_ROOT / "data" / "artifacts").resolve() / run_id / "step-1" / "proof.txt"
    assert expected.is_file(), f"missing artifact file at {expected}"
    assert expected.read_text() == "sandbox proof\n"

    entry = next(a for a in result["artifacts"] if a["name"] == "proof.txt")
    assert entry["bytes"] == expected.stat().st_size

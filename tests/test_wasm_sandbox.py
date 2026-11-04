"""The WASM backend, observed from real Wasmtime executions.

Nothing here is mocked: every assertion is the answer of a real
``wasmtime run`` against the pinned CPython-WASI build. The module is opt-in and
is never imported by the default Docker path.

These tests need the pinned assets (see ``docs/backends.md``); when they are
missing the whole module skips rather than faking a result.
"""
from __future__ import annotations

import os
import time

import pytest

from app import sandbox, wasm_sandbox

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def wasm_ready() -> str:
    """Skip (do not fake) when Wasmtime or the CPython-WASI build is absent."""
    if not wasm_sandbox.available():
        pytest.skip(
            "wasm assets missing: install wasmtime and cache the CPython-WASI build "
            "under ~/.cache/brz-wasm (see docs/backends.md)"
        )
    return wasm_sandbox.python_version()


@pytest.fixture
def run_ids():
    created: list[str] = []

    def new(prefix: str = "wasm") -> str:
        run_id = f"{prefix}-{os.urandom(5).hex()}"
        created.append(run_id)
        return run_id

    yield new

    for run_id in created:
        wasm_sandbox.cleanup_run(run_id)


def test_contract_matches_docker_backend(wasm_ready):
    """The result dict must be a superset of the Docker backend's keys."""
    import inspect

    docker_keys = set(inspect.signature(sandbox.execute).parameters)
    wasm_keys = set(inspect.signature(wasm_sandbox.execute).parameters)
    assert docker_keys == wasm_keys

    # Keys documented by app.sandbox.execute's return statement.
    expected = {
        "exit_code", "stdout", "stderr", "duration_ms", "timed_out", "oom",
        "killed_reason", "artifacts", "runtime", "gvisor", "oom_killed",
        "start_error", "host_procs_before", "host_procs_peak",
    }
    result = wasm_sandbox.execute("contract-keys", 1, "print('x')", None, 20)
    assert expected <= set(result), expected - set(result)


def test_stdlib_program_runs_and_stdout_is_captured(wasm_ready, run_ids):
    code = (
        "import json, statistics\n"
        "print('HELLO_FROM_WASM')\n"
        "print(json.dumps({'mean': statistics.mean([1, 2, 3])}))\n"
    )
    result = wasm_sandbox.execute(run_ids(), 1, code, timeout_s=20)

    assert result["exit_code"] == 0, result
    assert "HELLO_FROM_WASM" in result["stdout"], result
    assert '{"mean": 2}' in result["stdout"], result
    assert result["stderr"] == "", result["stderr"]
    assert result["timed_out"] is False
    assert result["start_error"] is False
    assert result["runtime"] == "wasmtime"
    assert result["backend"] == "wasm"
    # It really is the WASI interpreter, not the host python.
    assert wasm_ready.startswith("3."), wasm_ready


def test_nonzero_exit_code_and_stderr_are_reported(wasm_ready, run_ids):
    result = wasm_sandbox.execute(run_ids(), 1, "raise SystemExit(3)\n", timeout_s=20)

    assert result["exit_code"] == 3, result
    result = wasm_sandbox.execute(run_ids(), 1, "raise ValueError('boom')\n", timeout_s=20)
    assert result["exit_code"] == 1, result
    assert "ValueError: boom" in result["stderr"], result["stderr"]


def test_file_in_preopened_dir_becomes_an_artifact(wasm_ready, run_ids):
    code = (
        "with open('/out/report.txt', 'w') as fh:\n"
        "    fh.write('a' * 123)\n"
        "import os\n"
        "os.makedirs('/out/sub', exist_ok=True)\n"
        "open('/out/sub/nested.bin', 'wb').write(b'\\x00' * 7)\n"
        "print('WROTE')\n"
    )
    result = wasm_sandbox.execute(run_ids(), 1, code, timeout_s=20)

    assert result["exit_code"] == 0, result
    by_name = {a["name"]: a["bytes"] for a in result["artifacts"]}
    assert by_name.get("report.txt") == 123, result["artifacts"]
    assert by_name.get("sub/nested.bin") == 7, result["artifacts"]


def test_network_is_unavailable(wasm_ready, run_ids):
    code = (
        "import socket\n"
        "try:\n"
        "    socket.socket()\n"
        "    print('NET_ALLOWED')\n"
        "except BaseException as e:\n"
        "    print('NET_BLOCKED', type(e).__name__, e)\n"
    )
    result = wasm_sandbox.execute(run_ids(), 1, code, timeout_s=20)

    assert "NET_BLOCKED" in result["stdout"], result
    assert "NET_ALLOWED" not in result["stdout"], result
    # WASI has no socket capability at all: the failure is EOPNOTSUPP (58), not
    # a refused connection.
    assert "Not supported" in result["stdout"], result["stdout"]


def test_writing_outside_the_preopen_fails(wasm_ready, run_ids):
    code = (
        "targets = ['/etc/passwd', '/tmp/scratch', '/usr/local/pwned',\n"
        "           '/usr/local/lib/python3.14/sitecustomize.py']\n"
        "for path in targets:\n"
        "    try:\n"
        "        open(path, 'a').write('x')\n"
        "        print('WRITE_SUCCEEDED', path)\n"
        "    except BaseException as e:\n"
        "        print('WRITE_BLOCKED', path, type(e).__name__)\n"
    )
    result = wasm_sandbox.execute(run_ids(), 1, code, timeout_s=20)

    assert result["exit_code"] == 0, result
    assert "WRITE_SUCCEEDED" not in result["stdout"], result["stdout"]
    assert result["stdout"].count("WRITE_BLOCKED") == 4, result["stdout"]
    # The stdlib is written to by the interpreter itself; the write must have
    # been refused by the read-only permission bits, not by a missing path.
    assert "PermissionError" in result["stdout"], result["stdout"]


def test_wallclock_cap_kills_runaway(wasm_ready, run_ids):
    started = time.monotonic()
    result = wasm_sandbox.execute(
        run_ids(), 1, "print('starting runaway loop', flush=True)\nwhile True:\n    pass\n", timeout_s=2
    )
    wall = time.monotonic() - started

    assert result["timed_out"] is True, result
    assert result["killed_reason"] == ["timeout"], result["killed_reason"]
    assert 1800 <= result["duration_ms"] <= 8000, result["duration_ms"]
    assert wall < 12, wall
    assert "starting runaway loop" in result["stdout"], result["stdout"]


def test_memory_growth_is_capped_without_killing_the_host(wasm_ready, run_ids):
    code = (
        "blocks = []\n"
        "try:\n"
        "    for _ in range(64):\n"
        "        blocks.append(bytearray(64 * 1024 * 1024))\n"
        "    print('MEMORY_ALLOWED', len(blocks) * 64)\n"
        "except MemoryError:\n"
        "    print('MEMORY_CAPPED', len(blocks) * 64)\n"
    )
    result = wasm_sandbox.execute(run_ids(), 1, code, timeout_s=30)

    assert result["exit_code"] == 0, result
    assert "MEMORY_CAPPED" in result["stdout"], result["stdout"]
    assert "MEMORY_ALLOWED" not in result["stdout"], result["stdout"]
    # The cap fires inside the guest (memory.grow refused), so nothing is OOM
    # killed and the process still exits 0.
    assert result["oom"] is False
    assert result["peak_rss_kb"] < 400_000, result["peak_rss_kb"]


def test_host_environment_and_rootfs_are_absent(wasm_ready, run_ids):
    code = (
        "import os\n"
        "print('ENV', sorted(os.environ))\n"
        "try:\n"
        "    print('ROOT', sorted(os.listdir('/')))\n"
        "except BaseException as e:\n"
        "    print('ROOT_DENIED', type(e).__name__)\n"
    )
    result = wasm_sandbox.execute(run_ids(), 1, code, timeout_s=20)

    assert result["exit_code"] == 0, result
    assert "INFERENCE_API_KEY" not in result["stdout"], result["stdout"]
    # There is no root filesystem to list: only /out and /usr/local exist.
    assert "ROOT_DENIED" in result["stdout"], result["stdout"]


def test_data_science_stack_is_unavailable(wasm_ready, run_ids):
    """Pinned as a finding, not a bug: WASI CPython is stdlib-only."""
    code = (
        "for name in ('numpy', 'pandas', 'matplotlib'):\n"
        "    try:\n"
        "        __import__(name)\n"
        "        print('IMPORTED', name)\n"
        "    except BaseException as e:\n"
        "        print('UNAVAILABLE', name, type(e).__name__)\n"
    )
    result = wasm_sandbox.execute(run_ids(), 1, code, timeout_s=20)

    assert result["exit_code"] == 0, result
    assert result["stdout"].count("UNAVAILABLE") == 3, result["stdout"]
    assert "IMPORTED" not in result["stdout"], result["stdout"]

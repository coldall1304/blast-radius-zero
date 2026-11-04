"""Opt-in WebAssembly isolation backend.

This is a *drop-in alternative* to :mod:`app.sandbox`, not a replacement: it
implements the same ``execute()`` contract (same keyword arguments, same result
dict keys) on top of a Wasmtime-hosted CPython-WASI build instead of a Docker
container. Nothing here is imported by the default code path — ``app.main`` /
``app.config`` keep using :mod:`app.sandbox` unless a caller explicitly picks
this module.

What the boundary is
--------------------
The agent's program is compiled to nothing at all: CPython itself is the WASM
module (``python.wasm``), and the submitted Python source is fed to it on
stdin, exactly like ``python -I -`` in the container. The guest therefore only
has what WASI gives it:

* **no sockets** — ``socket.socket()`` fails with ``OSError: [Errno 58] Not
  supported`` because the Wasmtime CLI configures no network capability;
* **no root filesystem** — only the two explicit pre-opens below exist, so
  ``/etc/passwd`` is not "read-only", it is *absent*;
* **no host environment** — the CLI passes no environment unless ``--env`` is
  given, so the guest sees only the three variables we set;
* **one writable directory**, the per-step ``/out`` pre-open, bounded by a
  watchdog and by ``RLIMIT_FSIZE`` on the Wasmtime process;
* the standard library is pre-opened at ``/usr/local`` **read-only at the host
  permission level** (mode ``0555``/``0444``, enforced with ``chmod`` on our own
  asset cache) — WASI preview1 has no read-only pre-open flag in this CLI, so
  this is a defence-in-depth measure rather than a capability. See
  ``docs/backends.md`` for the honest caveat.

Capability ceiling
------------------
WASI CPython ships the standard library only. ``import numpy`` (and pandas,
matplotlib, requests, ...) raises ``ModuleNotFoundError`` inside the sandbox.
That is a fundamental limitation of this backend, measured and documented in
``docs/backends.md`` — not a bug to work around.

Nothing in this module touches Docker, so importing it cannot disturb the
default backend.
"""
from __future__ import annotations

import glob
import os
import resource
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from .config import settings

MAX_ARTIFACTS = 25

# CPython-WASI is built with prefix=/usr/local, so the stdlib has to appear at
# /usr/local/lib/pythonX.Y inside the guest.
WASI_GUEST_PREFIX = "/usr/local"

# Where the pinned assets are cached (outside the repository, never committed).
ASSET_ROOT = Path(os.environ.get("BRZ_WASM_ROOT", str(Path.home() / ".cache" / "brz-wasm")))

DEFAULT_FSIZE_BYTES = 32 * 1024 * 1024  # mirrors --ulimit fsize in the Docker path

_ro_marker = ASSET_ROOT / ".pywasi-readonly"
_ro_checked = False
_version_cache: str | None = None
_version_lock = threading.Lock()


class WasmSandboxUnavailable(RuntimeError):
    """The Wasmtime binary or the CPython-WASI build is missing."""


# --------------------------------------------------------------------------
# asset discovery
# --------------------------------------------------------------------------
def _wasmtime_bin() -> Path:
    explicit = os.environ.get("BRZ_WASMTIME")
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return path
        raise WasmSandboxUnavailable(f"BRZ_WASMTIME={explicit} does not exist")
    found = shutil.which("wasmtime")
    if found:
        return Path(found)
    for candidate in sorted(glob.glob(str(ASSET_ROOT / "wasmtime-*" / "wasmtime")), reverse=True):
        return Path(candidate)
    raise WasmSandboxUnavailable(
        "wasmtime not found; run the installer in docs/backends.md or set BRZ_WASMTIME"
    )


def _python_wasm() -> Path:
    explicit = os.environ.get("BRZ_WASI_PYTHON")
    path = Path(explicit).expanduser() if explicit else ASSET_ROOT / "pywasi" / "python.wasm"
    if not path.is_file():
        raise WasmSandboxUnavailable(
            f"CPython-WASI module missing at {path}; see docs/backends.md"
        )
    return path


def _stdlib_root(module: Path) -> Path:
    explicit = os.environ.get("BRZ_WASI_STDLIB")
    root = Path(explicit).expanduser() if explicit else module.parent
    if not (root / "lib").is_dir():
        raise WasmSandboxUnavailable(
            f"CPython-WASI stdlib missing under {root / 'lib'}; see docs/backends.md"
        )
    return root


def available() -> bool:
    """True when both assets can be located (does not start anything)."""
    try:
        _wasmtime_bin()
        _stdlib_root(_python_wasm())
    except WasmSandboxUnavailable:
        return False
    return True


def version() -> str:
    """``wasmtime --version`` output, cached for the process lifetime."""
    global _version_cache
    with _version_lock:
        if _version_cache is None:
            proc = subprocess.run(
                [str(_wasmtime_bin()), "--version"],
                capture_output=True, text=True, timeout=15,
            )
            _version_cache = (proc.stdout or proc.stderr).strip() or "unknown"
        return _version_cache


def python_version() -> str:
    """Reported ``sys.version`` of the WASI interpreter (one tiny run, cached)."""
    cached = ASSET_ROOT / ".pywasi-version"
    if cached.is_file():
        return cached.read_text().strip()
    module = _python_wasm()
    proc = subprocess.run(
        [str(_wasmtime_bin()), "run", "--dir", f"{_stdlib_root(module)}::{WASI_GUEST_PREFIX}",
         str(module), "-I", "-c", "import sys;print(sys.version.split()[0])"],
        capture_output=True, text=True, timeout=60,
    )
    text = (proc.stdout or "").strip() or "unknown"
    try:
        cached.write_text(text + "\n")
    except OSError:
        pass
    return text


# --------------------------------------------------------------------------
# limits / capability description
# --------------------------------------------------------------------------
def limits() -> dict:
    return {
        "runtime": "wasmtime",
        "engine_version": version(),
        "python": python_version(),
        "gvisor": False,
        "network": "none (WASI has no socket capability)",
        "memory_mb": settings.sandbox_memory_mb,
        "cpus": "1 (single-threaded wasm; hostile loops die on the wall clock)",
        "pids": 1,
        "timeout_s": settings.sandbox_timeout_s,
        "tmp_mb": 0,
        "disk_mb": settings.sandbox_disk_mb,
        "fsize_bytes": DEFAULT_FSIZE_BYTES,
        "output_bytes": settings.sandbox_output_bytes,
        "user": "host uid (no container uid mapping)",
        "rootfs": "absent (only two pre-opened directories)",
        "capabilities": "none (WASI preview1)",
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


def _host_procs() -> int:
    try:
        return sum(1 for entry in os.listdir("/proc") if entry.isdigit())
    except OSError:
        return -1


def _rss_kb(pid: int) -> int:
    """VmHWM (peak RSS since exec) of the Wasmtime process, in kB."""
    try:
        with open(f"/proc/{pid}/status", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _harden_stdlib(root: Path) -> bool:
    """Strip write bits from our asset cache so the pre-open is effectively RO.

    WASI preview1 pre-opens are read-write; the Wasmtime CLI has no read-only
    directory flag. The guest runs as the same unprivileged uid as the parent,
    so ``chmod -w`` on the host tree is what actually refuses the write. This is
    permission-based, not capability-based — documented as such.
    """
    global _ro_checked
    if _ro_marker.is_file() and not os.access(root, os.W_OK):
        _ro_checked = True
        return True
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            try:
                os.chmod(os.path.join(dirpath, name), 0o555)
            except OSError:
                pass
        for name in filenames:
            p = os.path.join(dirpath, name)
            try:
                if os.stat(p).st_mode & 0o222:
                    os.chmod(p, 0o444)
            except OSError:
                pass
    try:
        os.chmod(root, 0o555)
        _ro_marker.write_text("1\n")
    except OSError:
        pass
    _ro_checked = True
    return True


def _fsize_limit() -> None:  # pragma: no cover - runs in the forked child
    """Applied in the forked child of :class:`subprocess.Popen`."""
    resource.setrlimit(resource.RLIMIT_FSIZE, (DEFAULT_FSIZE_BYTES, DEFAULT_FSIZE_BYTES))


# --------------------------------------------------------------------------
# execute
# --------------------------------------------------------------------------
def execute(
    run_id: str,
    step_no: int,
    code: str,
    on_line: Callable[[str, str], None] | None = None,
    timeout_s: int | None = None,
) -> dict:
    """Run ``code`` under Wasmtime + CPython-WASI and return the same shape
    of result as :func:`app.sandbox.execute`."""
    wasmtime = _wasmtime_bin()
    module = _python_wasm()
    stdlib = _stdlib_root(module)
    _harden_stdlib(stdlib)

    timeout = timeout_s or settings.sandbox_timeout_s
    base = settings.artifacts_dir / run_id
    outdir = base / "out"
    outdir.mkdir(parents=True, exist_ok=True)
    os.chmod(outdir, 0o777)

    cmd = [
        str(wasmtime), "run",
        "--dir", f"{outdir}::/out",
        "--dir", f"{stdlib}::{WASI_GUEST_PREFIX}",
        "--env", "PYTHONUNBUFFERED=1",
        "--env", "PYTHONDONTWRITEBYTECODE=1",
        "-W", f"max-memory-size={settings.sandbox_memory_mb * 1024 * 1024}",
        # Engine-level backstop: our watchdog is the primary cap, this makes a
        # runaway loop die even if the watchdog thread is starved.
        "-W", f"timeout={(timeout + 5) * 1000}ms",
        # On-disk compiled-module cache: ~1.6 s cold compile vs ~0.2 s warm.
        # Set BRZ_WASM_COMPILE_CACHE=0 to measure the cold path.
        "-C", "cache=" + ("y" if os.environ.get("BRZ_WASM_COMPILE_CACHE", "1") != "0" else "n"),
        str(module),
        "-I", "-",
    ]

    started = time.monotonic()
    procs_before = _host_procs()
    procs_peak = procs_before
    chunks: list[bytes] = []
    err_chunks: list[bytes] = []
    captured = {"bytes": 0, "overflow": False}
    killed_reason: list[str] = []
    peak_rss = {"kb": 0}
    lock = threading.Lock()

    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0, preexec_fn=_fsize_limit, start_new_session=True,
        )
    except OSError as exc:
        return {
            "exit_code": 125,
            "stdout": "",
            "stderr": f"could not start wasmtime: {exc}",
            "duration_ms": int((time.monotonic() - started) * 1000),
            "timed_out": False,
            "oom": False,
            "killed_reason": ["start"],
            "artifacts": [],
            "runtime": "wasmtime",
            "gvisor": False,
            "oom_killed": False,
            "start_error": True,
            "host_procs_before": procs_before,
            "host_procs_peak": procs_before,
            "backend": "wasm",
            "peak_rss_kb": 0,
        }

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

    def kill_tree() -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass

    while True:
        try:
            proc.wait(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            pass
        elapsed = time.monotonic() - started
        rss = _rss_kb(proc.pid)
        if rss > peak_rss["kb"]:
            peak_rss["kb"] = rss
        if elapsed > timeout:
            killed_reason.append("timeout")
            kill_tree()
            break
        if _dir_size(outdir) > settings.sandbox_disk_mb * 1024 * 1024:
            killed_reason.append("disk")
            kill_tree()
            break
        if captured["overflow"]:
            killed_reason.append("output")
            kill_tree()
            break
        current = _host_procs()
        if current > procs_peak:
            procs_peak = current

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    t_out.join(timeout=2)
    t_err.join(timeout=2)

    duration_ms = int((time.monotonic() - started) * 1000)
    rc = proc.returncode
    stderr_text = b"".join(err_chunks).decode("utf-8", "replace")

    # SIGXFSZ == the RLIMIT_FSIZE cap fired (same role as --ulimit fsize).
    if rc == -signal.SIGXFSZ:
        killed_reason.append("disk")
    elif rc == -signal.SIGXCPU:
        killed_reason.append("cpu")

    start_error = rc == 125 or (
        not chunks and "Error: failed to" in stderr_text
    )

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
            artifacts.append({"name": str(path.relative_to(snapshot)), "bytes": stat.st_size})
            if len(artifacts) >= MAX_ARTIFACTS:
                break

    stdout = b"".join(chunks).decode("utf-8", "replace")
    if captured["overflow"]:
        stdout += f"\n[output truncated at {settings.sandbox_output_bytes} bytes]"

    return {
        "exit_code": rc,
        "stdout": stdout,
        "stderr": stderr_text,
        "duration_ms": duration_ms,
        "timed_out": "timeout" in killed_reason,
        "oom": False,  # WASI caps growth with max-memory-size; it never OOM-kills
        "killed_reason": killed_reason,
        "artifacts": artifacts,
        "runtime": "wasmtime",
        "gvisor": False,
        "oom_killed": False,
        "start_error": start_error,
        "host_procs_before": procs_before,
        "host_procs_peak": procs_peak,
        # wasm-specific extras
        "backend": "wasm",
        "engine_version": version(),
        "python_version": python_version(),
        "peak_rss_kb": peak_rss["kb"],
        "memory_limit_mb": settings.sandbox_memory_mb,
    }


def cleanup_run(run_id: str) -> None:
    """Remove the per-run artifact tree (same semantics as the Docker backend)."""
    target = (settings.artifacts_dir / run_id).resolve()
    root = settings.artifacts_dir.resolve()
    if root not in target.parents and target != root:
        return
    shutil.rmtree(target, ignore_errors=True)

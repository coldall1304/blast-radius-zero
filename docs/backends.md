# Execution backends: Docker/runc vs. WASM (Wasmtime + CPython-WASI)

Blast Radius Zero ships **one** default execution backend: a hardened Docker
container using `runc`. This document describes an **opt-in second backend**
(`app/wasm_sandbox.py`) and measures it honestly against the default. The WASM
backend is *not* wired into `app/main.py`; selecting it is a one-line dispatch
decision left to the operator. Nothing here changes the default path.

Every number below was measured on the development host
(8 vCPU, 31 GB RAM, Ubuntu, Docker 29.1.3, runc only) on 2026-11-04, using the
pinned versions recorded in [Versions](#versions-pinned). Where a number is a
sample, the sample size is stated. Nothing is extrapolated.

---

## TL;DR

| | Docker / runc (default) | WASM / Wasmtime (opt-in) |
|---|---|---|
| Startup, warm | 560 ms (n=12, full `execute()`; 240 ms bare `docker run`) | **305 ms** (n=12, full `execute()`) |
| Startup, cold | 240 ms bare `docker run` (n=5) | **1 833 ms** (n=5, no compile cache) |
| Peak RSS | **8.7 MiB** (cgroup `memory.peak`) | 80.9 MB warm / 274 MB cold |
| Network | netns `none`; loopback only | **no socket capability at all** (`EOPNOTSUPP`) |
| Filesystem | read-only rootfs + one bind mount | **no rootfs**; two pre-opens, one writable |
| Python | 3.12.16 **+ numpy 2.5.4, pandas 3.1.0, matplotlib 3.11.2** | 3.14.9 **stdlib only** |
| 7 adversarial probes | 7/7 blocked | 6/7 expressible, 6/6 blocked; 1 not expressible |

**The honest headline:** the WASM backend is a genuinely tighter *capability*
boundary — there is no root filesystem to write to and no socket API to abuse,
so two whole classes of attack stop existing rather than being denied. But it
is **slower to start cold (~7.6×), ~9× hungrier for memory, slightly slower on
CPU work, and cannot run the data-science stack the agent is told it has.** For the
product as currently prompted (`app/agent.py:34` promises "stdlib, numpy, pandas
and matplotlib"), the WASM backend is a *containment demo*, not a drop-in
replacement.

---

## Versions (pinned)

| Component | Version | Source |
|---|---|---|
| Wasmtime CLI | **50.0.1** (`c307ec81d 2026-10-29`) | `https://github.com/bytecodealliance/wasmtime/releases/download/v50.0.1/wasmtime-v50.0.1-x86_64-linux.tar.xz` (11 577 424 B) |
| CPython-WASI | **3.14.9**, built with WASI SDK 24 | `https://github.com/brettcannon/cpython-wasi-build/releases/download/v3.14.9/python-3.14.9-wasi_sdk-24.zip` (14 333 948 B) |
| `python.wasm` | 30 628 506 B | inside the zip above |
| CPython-WASI tree (`lib/` + module) | 42 371 525 B on disk | inside the zip above |
| Wasmtime binary | 67 964 264 B | from the tarball above |
| Wasmtime compile cache | ~12 MB after first run | `~/.cache/wasmtime` (default) |
| Docker sandbox image | `brz-sandbox:1`, 300 338 432 B, Python 3.12.16 | `sandbox/Dockerfile` |

Note on provenance: `python-build-standalone` (astral-sh) **no longer publishes
WASI assets** — the latest release (`20261026`, 991 assets) contains zero
`wasi`-named artifacts, and none of the 15 most recent releases do either. The
WASI builds used here come from `brettcannon/cpython-wasi-build`, the upstream
project the CPython WASI documentation points at. That is a real supply-chain
consideration: this is a community build, not a PSF release artifact, and it is
pinned by URL + size above for that reason.

The assets are cached **outside the repository** at `~/.cache/brz-wasm/`
(override with `BRZ_WASM_ROOT`) and are never committed.

---

## Isolation model

| Property | Docker/runc | WASM/Wasmtime |
|---|---|---|
| Unit of isolation | Linux namespaces + cgroups; container | WebAssembly sandbox inside one host process |
| Boundary enforced by | kernel (namespaces, cgroups, seccomp-ish defaults, capabilities) | Wasmtime's WASI implementation — no host syscalls are exposed unless a WASI function maps to them |
| Root filesystem | image rootfs, mounted **read-only** | **does not exist**: only `/out` and `/usr/local` are pre-opened |
| Writable space | `/out` bind mount + 16 MB tmpfs `/tmp` | `/out` only (no `/tmp`) |
| Network | `--network none` (isolated netns, loopback only) | WASI preview1 has no socket API; `socket.socket()` → `OSError [Errno 58]` |
| PIDs | `--pids-limit 64` | single process — no `fork`/`spawn` exists in WASI |
| Memory | cgroup `--memory 256m`, OOM-kill | `-W max-memory-size=268435456` → guest `memory.grow` fails → `MemoryError` in Python |
| CPU | `--cpus 0.5` (CFS quota) | none — one hostile loop owns one core until the wall-clock watchdog or `-W timeout` fires |
| User | `65534:65534` | **host uid** (whatever runs the control plane) |
| Capabilities | `--cap-drop ALL`, `no-new-privileges` | N/A (no Linux process inside) |
| Secrets | none in env; no host paths mounted | 2 env vars only (`PYTHONUNBUFFERED`, `PYTHONDONTWRITEBYTECODE`); no host env inherited |

Measured evidence for the WASM side (full programs, real output):

```
socket.socket()                       -> BLOCKED OSError [Errno 58] Not supported
urllib.request.urlopen('http://…')    -> BLOCKED AttributeError module '_socket' has no attribute 'getaddrinfo'
subprocess.run(['ls'])                -> BLOCKED OSError [Errno 58] wasi does not support processes.
hasattr(os, 'fork')                   -> False
os.listdir('/')                       -> FileNotFoundError [Errno 44]
open('/etc/hostname')                 -> FileNotFoundError [Errno 44]
open('~/.ssh/id_rsa')                 -> FileNotFoundError [Errno 44]
open('/tmp/x','w')                    -> FileNotFoundError [Errno 44]
open('/usr/local/pwned','w')          -> PermissionError [Errno 2]
sorted(os.environ)                    -> ['PYTHONDONTWRITEBYTECODE', 'PYTHONUNBUFFERED']
open('/out/x.bin','wb')               -> OK
```

---

## Measured comparison

### Startup latency

Workload: `print('hello')` plus a 200 k-iteration sum, submitted through the
real `execute()` entry point of each backend (artifact snapshot included).
Medians are over the stated runs; range in parentheses.

| Path | n | Median | Range | Notes |
|---|---|---|---|---|
| WASM, warm (compile cache on) | 12 | **305.1 ms** | 304.5–337.9 | default configuration, interleaved with the Docker runs below |
| WASM, cold (`-C cache=n`) | 5 | **1 833.1 ms** | 1 798.0–1 953.7 | Wasmtime JIT-compiles the 30 MB module every run |
| WASM, warm, CLI only | 5 | 212 ms | 209.5–239.5 | excludes watchdog/artifact overhead |
| Docker, full `execute()` | 12 | **559.7 ms** | 302.1–606.4 | **bimodal**: `app/sandbox.py` polls at 0.25 s, so runs land near 303 ms or ~570 ms |
| Docker, bare `docker run … python -c pass` | 5 | 240 ms | 218.1–261.2 | container create + interpreter boot |

Reading this honestly:

* **Warm, the WASM backend is the faster of the two as implemented** (305 vs
  560 ms) — but only because of the Docker path's own watchdog: `app/sandbox.py`
  sleeps 0.25 s per loop iteration, so a run that finishes mid-sleep waits out
  the remainder. The engine-level comparison is 212 ms (Wasmtime CLI) versus
  ~240 ms (bare `docker run`): **roughly a tie, Docker marginally ahead.**
  Do not read the 305-vs-560 gap as WASM being inherently quicker.
* **Cold, WASM loses badly: 1.83 s vs 0.24 s** because every fresh process
  JIT-compiles a 30 MB module. The compile cache is not a micro-optimisation
  here, it is the difference between usable and unusable.
* The 1.83 s cold figure also means the *first* run after a deploy pays it.

### Memory footprint

| Backend | Measurement | Value |
|---|---|---|
| Docker | `docker stats --no-stream` on a live container running an idle interpreter | **8.46 MiB** (two runs: 8.461, 8.457 MiB) |
| Docker | cgroup v2 `memory.peak` after the same run | **8.6–8.7 MiB** (9 052 160 / 9 129 984 B) |
| WASM, warm | `VmHWM` of the Wasmtime process (n=12) | **80 348–81 032 kB (≈78–79 MB)** |
| WASM, cold | `VmHWM` during JIT compile (n=5) | **262 452–282 656 kB (≈256–276 MB)** |
| WASM, memory-bomb probe | `VmHWM` while the guest holds 192 MB | 199 700–214 940 kB |

Two things worth stating plainly:

1. **The WASM backend is ~9× heavier per run.** A 30 MB module plus JIT'd code
   plus the interpreter's linear memory costs ~80 MB before the guest does any
   work. If you are running this on a small Vultr instance, that is the number
   that matters, not the elegance of the boundary.
2. **`max-memory-size` is not `--memory`.** It bounds the guest's *linear
   memory* to 256 MB; it does not bound the host process. The cold run peaked at
   **282 MB host RSS with a 256 MB guest cap** — proof that the two limits are
   not the same. A cgroup/RLIMIT_AS wrapper around the Wasmtime process would be
   needed to make the host-side claim true.

### CPU throughput

Same workload in both backends: `for i in range(2_000_000): s += i*i % 7`.

| Backend | Run 1 | Run 2 |
|---|---|---|
| WASM | 1 159 ms | 1 015 ms |
| Docker | 808 ms | 901 ms |

≈ **1.25–1.3× slower** under Wasmtime. Data-heavy work is closer to parity in
this sample (50 000-row JSON + CSV build and SHA-256: WASM 225/261 ms vs Docker
294/281 ms), so the penalty is concentrated in tight arithmetic loops, not I/O
shaped code.

---

## Capability matrix — what the WASM backend CANNOT run

Measured with one program that imports 36 modules and reports OK/FAIL. Counts:
**25 importable, 11 not.**

| Module | WASM | Docker | Note |
|---|---|---|---|
| `json` `math` `statistics` `csv` `re` `datetime` `decimal` `fractions` `itertools` `collections` `hashlib` `base64` `struct` `random` `textwrap` `logging` `argparse` `pathlib` `typing` `xml.etree.ElementTree` | ✅ | ✅ | full stdlib purity works |
| `threading` | ✅ (single-threaded) | ✅ | no parallelism in wasm |
| `socket`, `urllib.request`, `http.client` | ⚠️ import OK, **unusable** | ✅ (but netns `none`) | construction fails with `EOPNOTSUPP` / `getaddrinfo` missing |
| `subprocess` | ❌ runtime: `wasi does not support processes` | ✅ | |
| `multiprocessing` | ❌ `ModuleNotFoundError` | ✅ | |
| `ctypes` | ❌ `ModuleNotFoundError` | ✅ | no FFI |
| `sqlite3` | ❌ `ModuleNotFoundError` | ✅ | no `_sqlite3` |
| `zlib`, `gzip` | ❌ `ModuleNotFoundError` | ✅ | no compression |
| `ssl` | ❌ `ModuleNotFoundError` | ✅ | |
| **`numpy`** | ❌ `ModuleNotFoundError` | ✅ 2.5.4 | |
| **`pandas`** | ❌ `ModuleNotFoundError` | ✅ 3.1.0 | |
| **`matplotlib`** | ❌ `ModuleNotFoundError` | ✅ 3.11.2 | |
| `requests`, `scipy` | ❌ `ModuleNotFoundError` | ❌ (not in image) | unchanged |

### The consequence, quantified

`app/agent.py` tells the model, verbatim: *"Python 3.12 with stdlib, numpy,
pandas and matplotlib available."* Under the WASM backend:

* **1 of the 4 advertised capabilities is real** (the stdlib), and only the
  pure-Python part of it — `sqlite3`, `zlib`, `ctypes` and `ssl` are missing too.
* Any agent program that reaches for `numpy`/`pandas`/`matplotlib` — which is
  the entire shape of a data-analysis step: load CSV → DataFrame → groupby →
  describe → plot PNG → write XLSX — fails at **import time** with
  `ModuleNotFoundError`, i.e. before a single line of the actual analysis runs.
  The step-verifier would see a traceback, not a wrong answer.
* Concretely: of a 6-stage typical analysis (read, reshape/group, numeric
  aggregation, statistics, plot, spreadsheet export), **2 stages are
  expressible** with the stdlib alone (read/`csv`, statistics/`statistics`), and
  the plot and spreadsheet stages have no path at all. The WASM backend can run
  a *program*, not *this product's programs*.

This is a **finding, not a defect to paper over**. Making the WASM backend
useful would require either a WASI wheel set (none of numpy/pandas/matplotlib
publish `wasm32-wasi` wheels) or a per-backend prompt that tells the model to
use `csv` + `statistics` and emit SVG by hand.

---

## Adversarial probes: the existing 7, run against WASM

Each probe is the exact program from `app/redteam.py`, executed through
`app.wasm_sandbox.execute(..., timeout_s=10)`. The Docker verdicts are from the
same probe set on the default backend.

| Probe | Expressible under WASI? | Blocked? | Evidence (measured) |
|---|---|---|---|
| `rootfs_write` | yes | **yes** | `WRITE_BLOCKED FileNotFoundError [Errno 44] No such file or directory: '/etc/passwd'` — there is no rootfs to be read-only; the path does not exist. Exit 0, 304 ms. |
| `fork_bomb` | **no** | n/a — attack impossible | `AttributeError: module 'os' has no attribute 'fork'` at line 5, exit 1, 505 ms, **zero child processes**. WASI has no `fork`; `subprocess` raises `wasi does not support processes`. The probe's judge (`settings.sandbox_pids` delta) has nothing to measure, so this is reported as *not expressible*, not as *blocked*. |
| `cpu_runaway` | yes | **yes** | `killed_reason=['timeout']`, `timed_out=true`, exit −9 (SIGKILL), 10 079 ms for a 10 s budget. Print of `starting runaway loop` was captured before the kill. |
| `egress` | yes | **yes** | `EGRESS_BLOCKED URLError <urlopen error unknown url type: https>` (no `ssl`). A direct `socket.socket()` is refused with `OSError [Errno 58] Not supported`, and `urlopen('http://…')` with `AttributeError: module '_socket' has no attribute 'getaddrinfo'`. Exit 0, 405 ms. |
| `secret_read` | yes | **yes** | `ENV_KEYS ['PYTHONDONTWRITEBYTECODE', 'PYTHONUNBUFFERED']`, `SECRET_ENV []`, `READABLE_SENSITIVE none`. Every one of `/host`, `/root/.ssh/id_rsa`, `/app/.env`, `/etc/shadow`, `/var/run/docker.sock` was unreadable. |
| `memory_bomb` | yes | **yes** | `allocated_mb 64 / 128 / 192`, then `MEMORY_CAPPED MemoryError 192`. The 4th 64 MB block was refused because the guest hit `max-memory-size=256 MiB`. Exit 0, 406 ms — a clean in-guest `MemoryError`, **not** an OOM kill (`oom=false`). |
| `disk_fill` | yes | **yes** | `exit_code=-25`, `killed_reason=['disk']`, `fill.bin` frozen at **33 554 432 B** (exactly the 32 MiB `RLIMIT_FSIZE`); the process died on `SIGXFSZ` after 1 414 ms, before the 64 MB directory watchdog could fire. |

**Score: 6 of 7 probes expressible, 6 of 6 blocked.** One probe (`fork_bomb`)
cannot be posed at all — a *stronger* result than "blocked", but reported
separately because the suite's `blocked` boolean would have to be `false` for a
program that never ran.

Note the difference in *mechanism* from Docker: `rootfs_write` is refused for a
different reason (path absent vs. `EROFS`), and `disk_fill` is stopped by the
per-file `RLIMIT_FSIZE` on the host process rather than `--ulimit fsize` inside
a container. Same outcome, different layer.

---

## What WASM does NOT protect against

Being precise about the boundary matters more than praising it.

1. **Host-side Wasmtime bugs / JIT escapes.** The entire guarantee rests on
   Wasmtime's correctness and its WASI implementation. A memory-safety bug in
   Wasmtime (Cranelift-generated code, the runtime, the WASI host functions) is
   a full escape to the control plane, which holds the API key and the
   database. Docker's boundary is the Linux kernel's — a much older, more
   attacked surface, but a *different* one. A serious deployment should treat a
   Wasmtime CVE as a containment incident, exactly like a runc CVE.
2. **The control plane still holds `/var/run/docker.sock`.** Choosing the WASM
   backend does not remove the Docker socket from the host: the other backend
   needs it, and `deploy/` keeps it mounted for the app container. Any escape
   from the *control plane* still has root-equivalent Docker access. The WASM
   backend protects the *agent's* programs, not the controller.
3. **The stdlib "read-only" mount is permissions, not capability.** WASI
   preview1 pre-opens are read-write and Wasmtime 50.0.1's CLI has **no
   read-only directory flag** (`--dir` only; the request is open upstream as
   bytecodealliance/wasmtime#9608). `app/wasm_sandbox.py` therefore `chmod`s its
   own asset cache to `0555`/`0444`; the guest runs as the host uid, so the
   refusal is the host kernel's `EACCES`. It works (measured: `PermissionError`
   on `/usr/local/pwned` and on overwriting `os.py`), but it is defense in
   depth, not a WASI capability — and it breaks if the process ever runs as
   root, since root ignores those bits.
4. **Host memory is not capped.** As measured above, the 256 MB
   `max-memory-size` is a guest-linear-memory limit; the host process peaked at
   282 MB during compilation. Nothing stops many concurrent WASM runs from
   exhausting host RAM except the control plane's own concurrency limit.
5. **No CPU quota.** One runaway guest pins one core for the whole timeout
   window. Docker caps at `--cpus 0.5` on a shared host; WASM has no equivalent
   here, only the wall clock.
6. **No per-run host filesystem isolation beyond the two pre-opens.** If a
   future change pre-opens anything else (a cache dir, the artifact root, `$HOME`),
   the guest gets read-write access to it by default. There is no
   `--read-only` to fall back on.
7. **Asset integrity is on you.** The CPython-WASI build is a community zip, and
   the Wasmtime tarball is downloaded over TLS with no signature check in the
   install path. Pin by hash in production.
8. **It is slower and hungrier.** Cold start 6× and RSS 9× versus Docker. On a
   small instance that is a denial-of-service vector in itself.

---

## How to reproduce

All commands are run from the repository root. Assets land in
`~/.cache/brz-wasm/`, outside the repo.

```bash
# 1. Wasmtime 50.0.1 (official installer, or the pinned tarball directly)
curl -sSf https://wasmtime.dev/install.sh | bash          # -> ~/.wasmtime/bin
# pinned equivalent:
# curl -sSL -o wt.tar.xz https://github.com/bytecodealliance/wasmtime/releases/download/v50.0.1/wasmtime-v50.0.1-x86_64-linux.tar.xz
# tar xf wt.tar.xz -C ~/.cache/brz-wasm

# 2. CPython 3.14.9 for WASI (WASI SDK 24)
mkdir -p ~/.cache/brz-wasm && cd ~/.cache/brz-wasm
curl -sSL -o pywasi.zip \
  https://github.com/brettcannon/cpython-wasi-build/releases/download/v3.14.9/python-3.14.9-wasi_sdk-24.zip
python3 -c "import zipfile; zipfile.ZipFile('pywasi.zip').extractall('pywasi')"
# `app/wasm_sandbox.py` strips write bits from this tree on first use.

# 3. Smoke test the interpreter by hand
WT=$(ls -d ~/.cache/brz-wasm/wasmtime-*/wasmtime | head -1)
P=~/.cache/brz-wasm/pywasi
echo "print('hello from wasi')" | "$WT" run -C cache=y \
  --dir /tmp::/out --dir "$P"::/usr/local "$P/python.wasm" -I -

# 4. The test suite (10 tests: stdlib run, artifacts, network, write-outside,
#    wall clock, memory cap, env/rootfs absence, data-science absence, contract)
.venv/bin/python -m pytest tests/test_wasm_sandbox.py -q

# 5. Backend comparison numbers (latency, RSS, cold vs warm)
.venv/bin/python sandbox/wasm/bench.py

# 6. The 7 adversarial probes against WASM
.venv/bin/python sandbox/wasm/run_probes.py
```

Environment knobs the module honours (all optional):

| Variable | Default | Meaning |
|---|---|---|
| `BRZ_WASM_ROOT` | `~/.cache/brz-wasm` | asset cache root |
| `BRZ_WASMTIME` | autodetect | explicit Wasmtime binary |
| `BRZ_WASI_PYTHON` | `<root>/pywasi/python.wasm` | interpreter module |
| `BRZ_WASI_STDLIB` | module's directory | tree pre-opened at `/usr/local` |
| `BRZ_WASM_COMPILE_CACHE` | `1` | `0` disables `-C cache=y` (cold-path measurement) |

Docker-side numbers were produced with the image already built
(`deploy/build-sandbox.sh`) and the daemon idle:

```bash
docker run --rm --network none brz-sandbox:1 python -c "pass"   # ~240 ms
docker image inspect brz-sandbox:1 --format '{{.Size}}'         # 300338432
```

---

## Measurement notes (what these numbers are not)

* Single host, one commit of the harness, repeated batches on an otherwise
  idle machine. Reported ranges are real observed spread, not confidence
  intervals. The Docker `execute()` median moved between batches (309 ms in one
  batch, 560 ms in a later interleaved batch) *because* of the polling
  quantisation described below; the WASM warm median did not (305–306 ms across
  every batch). Trust the paired interleaved run for the warm comparison.
* The Docker `execute()` latency includes `app/sandbox.py`'s 0.25 s watchdog
  poll, which quantises results into two clusters; the bare `docker run` figure
  is the fair "engine cost" comparison against Wasmtime's 212 ms CLI figure.
* "Cold" for WASM means *no compiled-module cache*; the 30 MB `.wasm` may still
  be in the page cache. A true cold page cache would be slower still.
* "Cold" for Docker is not measurable without root (you cannot evict the image
  from the page cache), so the Docker figure is *warm image, fresh container* —
  which is what every request actually experiences.
* RSS for WASM is `VmHWM` of the Wasmtime process, sampled every 100 ms; a spike
  shorter than that could be missed. Docker RSS is cgroup `memory.peak`, which is
  exact.
* CPU throughput was sampled twice per backend on one workload; treat
  "1.25–1.3×" as an order-of-magnitude statement, not a benchmark score.

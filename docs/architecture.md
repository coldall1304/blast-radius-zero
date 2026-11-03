# Architecture

Blast Radius Zero is a single-process FastAPI control plane that drives an external container
runtime. There is no queue, no worker fleet and no database server: the control plane, the
inference client, the sandbox driver and the ledger all live in one Python process on one Vultr VM.

This document describes the request lifecycle, the boundaries, the failure modes, and where the
implementation currently diverges from the design.

---

## 1. Components

| Component | File | Responsibility |
|---|---|---|
| HTTP surface | `app/main.py` | Routes, request validation, per-IP rate limit, SSE fan-out, static UI |
| Event bus | `app/main.py` (`Bus`) | In-memory history + subscriber queues per run; replay on connect |
| Orchestrator | `app/agent.py` | Step loop, planner/verifier prompts, JSON decisions, feedback |
| Inference client | `app/inference.py` | OpenAI-compatible `POST /chat/completions`, tolerant JSON extraction, mock |
| Sandbox driver | `app/sandbox.py` | `docker run` argv, streaming capture, watchdogs, artifact enumeration, destroy |
| Probe suite | `app/redteam.py` | 7 attack programs, per-probe judges, suite summary |
| Ledger | `app/db.py` | SQLite schema (`runs`, `steps`, `probes`), WAL mode, single connection + lock |
| UI | `app/static/` | Console (dispatch + live steps), Containment Lab, Ledger |

Two semaphores bound load: `MAX_CONCURRENT_RUNS` (default 2) for tasks, and a separate
`Semaphore(1)` for probe suites, so an adversarial suite never interleaves with another one.

## 2. Request lifecycle

```
User            API              Orchestrator         Inference        Sandbox (Docker)      Ledger
 │               │                    │                   │                  │                │
 │ POST /api/runs│                    │                   │                  │                │
 ├──────────────▶│ rate-limit (ip,12/h)                  │                  │                │
 │               ├── insert run(status=queued) ──────────────────────────────────────────────▶│
 │               ├── publish run.started                 │                  │                │
 │               ├── 200 {run_id} ──▶│                   │                  │                │
 │◀──────────────┤                    │                   │                  │                │
 │ GET .../events│                    │                   │                  │                │
 ├──────────────▶│ replay history ────┤                   │                  │                │
 │◀── SSE ───────┤                    │                   │                  │                │
 │               │                    │ status=running    │                  │                │
 │               │                    ├── planner(step 1) ─▶                  │                │
 │               │                    │◀── {"action":"run","code":...}        │                │
 │               │                    ├── publish step.started                 │                │
 │               │                    ├── docker run (fresh container) ───────▶│                │
 │               │                    │◀── stdout lines (step.log, streamed) ──┤                │
 │               │                    │◀── exit / stderr / artifacts / flags ──┤                │
 │               │                    ├── publish step.finished                │                │
 │               │                    │                                       │  docker rm -f  │
 │               │                    ├── planner(step 2) ─▶ (with evidence block)              │
 │               │                    │◀── {"action":"finish","answer","claims"}                │
 │               │                    │ status=verifying  │                  │                │
 │               │                    ├── verifier(answer + evidence) ─▶      │                │
 │               │                    │◀── {"sufficient":true|false,...}      │                │
 │               │                    ├── publish verify                       │                │
 │               │                    ├── true → publish run.finished ───────────────────────▶│
 │◀── SSE ───────┤                    │ false → feedback, next turn (≤ MAX_VERIFY_RETRIES)     │
```

A step is a *complete round trip*: one model decision, one container, one evidence record. The
model sees the previous steps only as a compact text block (`_evidence_block`): exit code, timeout
flag, killed reasons, stdout truncated to 2500 chars, stderr to 800 chars, artifact names.

### Step-loop state machine

```
                 ┌──────────── invalid action / empty code / code > 20k chars ─────────────┐
                 ▼                                                                          │
   planning ──▶ decide ──▶ run ──▶ execute ──▶ record ──▶ (next turn) ───────────────────────┘
                 │
                 ├──▶ finish ──▶ verify ──sufficient──▶ done
                 │                  │
                 │                  └──not sufficient──▶ (retries left? feedback : unverified)
                 │
                 └──▶ 5 turns elapsed ──▶ exhausted (verified=false)
```

Hard stops inside the loop:

- at most `MAX_STEPS = 5` planner turns per run;
- at most `MAX_VERIFY_RETRIES = 1` additional finish attempt after a rejection, and no retry if the
  budget is already spent on that turn;
- at most 2 JSON-parse attempts per planner call (the model gets one "reply with JSON only" nudge);
- programs larger than 20 000 characters are rejected before they reach Docker.

## 3. Boundaries

```
┌────────────────────────── HOST / CONTROL PLANE ──────────────────────────┐
│  FastAPI process (uid = deploy user)                                     │
│   • INFERENCE_API_KEY in process memory                                  │
│   • /var/run/docker.sock access  ← root-equivalent, see docs/security.md │
│   • SQLite file + artifacts tree on local disk                           │
└───────────────┬───────────────────────────────────┬──────────────────────┘
                │ HTTPS (Bearer key)                │ docker CLI (local socket)
                ▼                                   ▼
     Vultr Serverless Inference          ┌───────────────────────────────┐
     (no task data retained by us)       │ per-step container            │
                                         │  namespaces: pid/net/mnt/...  │
                                         │  net: none                    │
                                         │  rootfs: ro                   │
                                         │  rw: /tmp (tmpfs), /out (bind)│
                                         │  uid 65534, caps: none        │
                                         │  cgroups: mem/cpu/pids        │
                                         └───────────────────────────────┘
```

What crosses each boundary:

| Boundary | Inbound | Outbound |
|---|---|---|
| Browser → API | task text (≤ 4000 chars) | SSE events, artifacts |
| API → Inference | prompts: task, evidence blocks, code (for the planner's own history) | JSON decisions, verdicts |
| API → Sandbox | one Python program on stdin | stdout/stderr (≤ 200 KB captured), exit code, up to 25 artifact entries |
| Sandbox → API | files under `/out` only, via a per-step bind mount | — |

The task text and the model's own code are the only attacker-controlled strings that reach the
sandbox. Neither reaches the host shell: the argv is a fixed list, and the program is written to
`python -I -`'s stdin.

## 4. Failure modes

Every row below is the behaviour of the current code, not an aspiration.

| Failure | Where it is caught | Observable result | Run status |
|---|---|---|---|
| Inference transport error / HTTP ≥ 400 | `inference.chat()` raises `InferenceError` | `run.error` event; answer = "Inference failed before a plan was produced: …" | `done` (see gap G5) |
| Missing `INFERENCE_API_KEY` | `inference.chat()` | same as above, message "INFERENCE_API_KEY is not configured" | `done` |
| Model returns non-JSON twice | `_ask_planner` | `InferenceError("planner produced no JSON: …")` → `run.error` | `done` |
| Model returns unknown `action` | `run_task` | feedback "Invalid action…", turn consumed | continues |
| Empty program / >20 000 chars | `run_task` | feedback, turn consumed, no container created | continues |
| Sandbox image missing | `sandbox.execute()` → `SandboxError` | `run.error`; answer "Sandbox unavailable: …" | `done` |
| Docker CLI absent | `Popen` → `FileNotFoundError` → `SandboxError` | same | `done` |
| `SANDBOX_RUNTIME=runsc` but no runsc | `runtime_info()` → `SandboxError` | `/api/config` returns `runtime: "unavailable"` + `runtime_error`; runs fail with the same message | `done` |
| `SANDBOX_RUNTIME=auto`, no runsc | `runtime_info()` | silent fallback to `runc`, `gvisor: false` in every step and in `/api/config` | normal |
| Container refuses to start (bad argv, image issue) | `docker run` exits 125 | `exit_code: 125`, `start_error: true`, stderr captured, shown to the model | continues |
| OOM | `docker inspect .State.OOMKilled` after the step | `oom: true`, `killed_reason: ["memory"]`; planner is told to chunk its data | continues |
| Wall clock exceeded | watchdog loop (250 ms poll) → `docker kill` | `timed_out: true`, `killed_reason: ["timeout"]`, exit 137 | continues |
| `/out` over 64 MB | watchdog `_dir_size()` → `docker kill` | `killed_reason: ["disk"]` | continues |
| Single file over `fsize` 32 MB | kernel `--ulimit fsize` | `OSError: [Errno 27] File too large` inside the program | continues |
| Output over 200 KB | capture counter → `docker kill` | `killed_reason: ["output"]`, stdout suffixed with a truncation marker | continues |
| Non-zero exit | `proc.returncode` | `exit_code != 0`, stderr shown, planner told to fix that exact error | continues |
| Step budget exhausted | end of `run_task` | answer "Step budget exhausted…", `run.status: exhausted` | `unverified` |
| Verifier rejects | `_verify` | `verify` event with reason; feedback injected; retry once | `unverified` after retries |
| Verifier itself fails | `_verify` catches everything | `sufficient: null`, reason "verifier unavailable: …" | `done` (fail-open, deliberate) |
| Rate limit | `_check_rate` | HTTP 429 before any run row is created | — |
| SSE consumer too slow | per-subscriber queue (max 1000) | events dropped for that client only; history replay still works | unaffected |
| Process restart mid-run | nothing | run stays `running` in the ledger, SSE history is empty; no resume | stale |

Degradation knobs, in order of how much safety they cost:

1. `SANDBOX_RUNTIME=auto` → `runc` fallback: kernel namespaces instead of gVisor's user-space
   kernel. Weaker against kernel-surface exploits, identical for everything the Lab tests.
2. `SANDBOX_NETWORK=bridge`: restores egress and invalidates the `egress` probe. Not a supported
   configuration for a public demo.
3. `MOCK_INFERENCE=1`: no model, deterministic two-step plan. Exercises sandbox + SSE + ledger;
   the verifier is not mocked, so every mock run ends `unverified`.

## 5. Watchdog details

`sandbox.execute()` does not trust the container to stop itself:

- the parent process streams stdout/stderr on two daemon threads and counts captured bytes under a
  lock, so a fast writer cannot deadlock the reader;
- every 250 ms it checks wall clock, `/out` size, and the output counter, and calls
  `docker kill <name>` on the first violation;
- after the loop it waits up to 10 s for the CLI process, then kills the CLI; it inspects
  `.State.OOMKilled` (which is why the container is started with `--rm=false`), and only then
  removes it with `docker rm -f` in a `finally`-style path that also runs when the step raised;
- the container is removed with `docker rm -f` after the loop, on the normal, timeout, disk and
  OOM paths. Removal is **not** inside a `try/finally`: if `docker inspect` or `docker kill` itself
  raises (subprocess timeout), the removal call is skipped and a stopped container can linger —
  gap G7;
- artifacts are enumerated from the bind-mounted directory on the host *after* the container is
  gone, so a hostile container cannot race the listing (it can only have written files there);
- at most 25 artifact entries are returned; their total size is bounded by the 64 MB watchdog
  (with up to one 250 ms poll of slack) and the 32 MB per-file `fsize` limit.

Observed on the reference host: a 10 s probe budget produced a kill at ~10.14 s (poll interval
plus kill latency), and a full 7-probe suite took ~14.4 s wall clock, dominated by that one probe.

## 6. Ledger

SQLite in WAL mode, one connection, one lock (the process is effectively single-writer).

```
runs(id, kind, task, status, model, mock, started_at, finished_at, answer, verified, verification, payload)
steps(id, run_id, no, title, explain, code, stdout, stderr, exit_code, duration_ms,
      timed_out, oom, killed_reason, artifacts, runtime)
probes(id, run_id, probe_id, title, blocked, evidence, payload)
```

Statuses written by the worker: `done`, `unverified`, `error`; probe suites write `done` when all
probes were contained and `error` otherwise. `kind` is `task` or `probe`.

## 7. Implementation gaps found during verification

These are documented here because the architecture above describes the *intended* data flow and
the build does not yet satisfy all of it. Line references are to the current tree.

| # | Gap | Impact | Fix |
|---|---|---|---|
| G1 | `docker run` argv lacks `-i` (`app/sandbox.py:144`) while code is written to stdin | **No program ever executes**; every step returns exit 0 with empty output; Lab reports 0/7 | add `--interactive` |
| G2 | `secret_read` probe lists `/proc/1/environ`, and the probe is PID 1 | probe always self-reports as breached → 6/7 ceiling | remove that path or judge env contents (`app/redteam.py:81,147`) |
| G3 | `db.add_step()` has no caller (`app/db.py:113`) | `GET /api/runs/{id}` returns `steps: []`; ledger cannot replay steps | call it from the worker in `app/main.py` after `step.finished` |
| G4 | artifacts live at `<run>/step-N/<name>`, UI links `<run>/<name>` (`app/static/app.js:223`) | artifact download 404s | include `step-N/` in the event payload or the URL |
| G5 | inference/sandbox failures return `verified = null`, mapped to `done` (`app/main.py:171`) | an outage looks like a successful run | map to `error` |
| G6 | *resolved*: a hand-built `brz-sandbox:1` predated `sandbox/Dockerfile` and lacked matplotlib, while the planner prompt promises it (`app/agent.py:34`) | plots failed with `ModuleNotFoundError` | always build via `deploy/build-sandbox.sh`; verified `numpy 2.5.4 / pandas 3.1.0 / matplotlib 3.11.2` |
| G7 | `docker rm -f` is not in a `try/finally` (`app/sandbox.py:239-240`) | an inspect/kill subprocess timeout can leave a container behind | wrap the post-run block in `finally` |

G1 and G2 are the two that block an honest containment demo.

# We gave an autonomous agent a container, then attacked it

Blast Radius Zero is a small web application. You type a task, a planner writes Python, the Python
runs in a container on a Vultr VM, and the answer returns audited against the bytes the sandbox
produced. The second half of the app attacks that sandbox with seven programs and records the
kernel's answers. This post is about what bounds the box, what each attack actually proves, and where
the guarantees stop.

## The generated program is hostile input

The system's thesis, as written in `docs/security.md`:

> an autonomous agent is an untrusted code author. Treat its output as hostile input, give it a
> disposable box with no secrets and no route out, bound every resource, destroy the box, and keep a
> ledger of what happened.

Three actors, three trust levels. The visitor is untrusted: no authentication, so anyone can type
anything. The model is untrusted *output*: steerable by the task text and by its own earlier output,
so it is read as a data source, never an authority. The program it writes is hostile. That is the
design centre, and it changes the questions. You stop asking whether the model will write safe code.
You ask what the box does when it writes `open('/etc/passwd','a')` and means it.

## A container is not a sandbox

`docker run` with defaults is a packaging boundary. It shares the host kernel, has a route out, a
writable root filesystem, uid 0 and a default capability set. That is not a criticism of Docker; it
is what the defaults are for.

What bounds the blast radius here is a list of decisions, each measurable from inside the box:

| Control | What it removes |
|---|---|
| No host mounts except one bind-mounted `/out` directory for that run | Host files are unreachable by path |
| `--read-only` root filesystem | Writes to `/etc`, `/bin`, `/usr` fail with `EROFS` |
| `--network none` | No route, no DNS, loopback only |
| No secrets in the container environment | Nothing inside is worth exfiltrating |
| `--cap-drop ALL`, `no-new-privileges`, uid 65534 | No mount, no ptrace, no setuid path |
| Memory, CPU, PID cgroups and file-size ulimits | One program cannot exhaust the host |
| Parent-process watchdog on clock, `/out` bytes and output | The container is not trusted to stop itself |
| `docker rm -f` after every step | Nothing survives to be reused |

The argument is not "we used Docker". It is that these flags are visible to the program inside, and
the lab measures them. The argv, trimmed, from `app/sandbox.py` (comments ours):

```python
cmd = [
    "docker", "run", "-i", "--name", name, "--init", "--rm=false",
    "--runtime", runtime,
    "--network", settings.sandbox_network,          # none
    "--memory", f"{settings.sandbox_memory_mb}m",   # 256
    "--memory-swap", f"{settings.sandbox_memory_mb}m",   # no swap
    "--cpus", settings.sandbox_cpus,                # 0.5
    "--pids-limit", str(settings.sandbox_pids),     # 64
    "--read-only",
    "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size={settings.sandbox_tmp_mb}m",
    "--user", f"{SANDBOX_UID}:{SANDBOX_UID}",       # 65534
    "--cap-drop", "ALL",
    "--ulimit", "fsize=33554432:33554432",
    "--mount", f"type=bind,src={outdir},dst=/out",
    settings.sandbox_image,
    "python", "-I", "-",
]
```

Two details matter more than the list. The program goes to the stdin of `python -I -` inside a fixed
argv, so model text never reaches a shell. And the watchdog lives in the parent process, polling every
250 ms, because a container busy burning CPU will not honor its own deadline.

## Seven attacks, and what the kernel said

Every probe in `app/redteam.py` is a real program sent through `sandbox.execute()` — the same entry
point the agent uses, with a 10 s budget instead of 20 s — and judged by its own output. None tests an
exploit. They test behaviour: what an agent does when it is confused, prompt-injected or simply
wrong.

**Tamper with the system filesystem.** We append to `/etc/passwd` and try to drop a payload in
`/bin`. The kernel answers `EROFS` / `PermissionError`; `WRITE_SUCCEEDED` never prints. That proves
the root filesystem is read-only for this uid on this runtime. It says nothing about a mount added
later, or about container escapes.

**Phone home.** `urlopen('https://example.com', timeout=4)` fails with a socket error, no DNS, no
default route. That proves egress is closed in that namespace at that moment. It does not prove the
host is offline — it is not — and it is void the moment someone sets `SANDBOX_NETWORK=bridge`, which
the config allows and the docs call unsupported.

**Hunt for credentials.** We dump the environment and try five paths — `/host`, `/root/.ssh/id_rsa`,
`/app/.env`, `/etc/shadow`, `/var/run/docker.sock`. None is readable. The environment holds only
`GPG_KEY`, `HOME`, `HOSTNAME`, `LANG`, `MALLOC_ARENA_MAX`, `MPLBACKEND`, `MPLCONFIGDIR`,
`OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `PATH`, `PYTHONDONTWRITEBYTECODE`, `PYTHONHASHSEED`,
`PYTHONNOUSERSITE`, `PYTHONUNBUFFERED`, `PYTHON_SHA256`, `PYTHON_VERSION` — base-image build
metadata plus six knobs the driver sets. No provider key. That proves the key is absent from the
environment and those five paths are shut. It does not prove no secret exists anywhere; a probe finds
what it looks for.

**Memory bomb.** We allocate 64 MB bytearrays in a loop. The memory cgroup OOM-kills the container at
about 192 MB against a 256 MB cap with swap disabled, and the kill is recorded as `oom: true`,
`killed_reason: ["memory"]`. That proves the cgroup enforces. It does not prove the VM stays healthy
under many boxes at once: the caps bound one container, not a fleet.

**Fork bomb.** `os.fork()` up to 400 times stops with `BlockingIOError` at 62 children against a PID
limit of 64. The judge does not trust that message alone: it measures host process-count growth
against the cap plus slack, because gVisor blocks the forker at the cap instead of returning `EAGAIN`
and the victim's own error string can be a false negative. That proves the PID cgroup holds and the
bound is measured from outside. It proves nothing about threads — `--pids-limit` counts them, so a
thread-hungry program hits the cap early. Usability cost, not a safety one.

**Disk fill.** We write 1 MB blocks to `/out` until it fails: `OSError: [Errno 27] File too large` at
32 MB, from the `fsize` ulimit. A second layer, the 64 MB `/out` watchdog, would kill the container
if the file limit were lifted. That proves the file and directory budgets. It does not prove host
disk is safe over time: cleanup exists as a function but no scheduler calls it, so artifacts
accumulate until someone prunes them.

**CPU runaway.** `while True: pass` dies at about 10.16 s against a 10 s probe budget, exit 137,
`killed_reason: ["timeout"]`. That proves the parent watchdog works without the container's help. It
does not show the CPU quota killing anything — a quota throttles, it does not kill, which is why the
watchdog exists.

A full suite takes about 14.4 s, dominated by that probe.

One honest note on the count. The repo records two runs. An early build was missing `-i` on `docker
run` while the program is fed on stdin, so no probe executed and the suite reported 0/7 — everything
looked breached because nothing ran. A scratch copy with the flag reported 6/7, the seventh being the
`secret_read` judge reading `/proc/1/environ` when the probe itself is PID 1. Both are fixed in the
tree, and `README.md` §9 and `docs/submission.md` record 7/7 on the deployed instance on 2026-11-03.
`docs/security.md` §6 and `docs/architecture.md` §7 have since been updated to record both fixes and
the evidence for them, so the docs no longer contradict the code.

## gVisor is a smaller surface, not a solution

Plain `runc` shares the host kernel: a kernel vulnerability in the reachable surface is a host
compromise, and no amount of `--cap-drop` changes that. gVisor `runsc` puts a user-space kernel
between the workload and the host, so the host kernel sees only the sentry's syscall set.

Smaller is not solved. The sentry is code with its own bugs. The seccomp profile is Docker's default,
not a custom allowlist; nobody wrote one for Python's syscall mix. The runtime is reported per step
and in `/api/config`, and `SANDBOX_RUNTIME=auto` falls back to `runc` silently when `runsc` is
missing — so the UI shows the field.

## The ledger records; it does not prove

Every step writes a row: exit code, duration, timeout, OOM, kill reason, runtime, artifact list. The
audit trail is queryable, not narrative.

`verified` means "not contradicted by recorded evidence". A second model call at temperature 0 sees
the answer and the evidence and is told to reject unsupported claims. It is another model, not a
prover. When it fails, the run is not blocked: it is stored with `verified: null` and the reason, a
deliberate availability trade. The ledger is not tamper-evident either — no hash chain, no external
sink, and the control plane can rewrite its own rows. "Immutable" in the UI means "not editable
through the API".

## The real trust boundary is the Docker socket

The control plane shells out to the `docker` CLI, so the service account must reach
`/var/run/docker.sock`. That is not a sandbox property. It is a root-equivalent capability: whoever
drives the daemon can start a privileged container with `/` mounted and own the VM.

So: every containment guarantee above holds for the code the agent writes. None of them hold for a
compromised control plane. In this build the two share one host, the API is internet-facing without
authentication, and the inference key is a plain process environment variable. Accepted risks for a
hackathon demo, written down rather than papered over. The hardening order is in `docs/security.md`
§4: rootless Docker or a socket proxy limited to `run/rm/inspect/kill`, then a separate executor host
with a signed job payload, then a scoped provider token, then auth before the URL is shared widely.

One more residual: `GET /api/runs` and `/api/runs/{id}` are unauthenticated and return other
visitors' tasks and answers. Do not paste secrets into the task box of a public demo. The fix is auth
or per-session scoping, and it is not in this build.

## What we do not claim

- That the sandbox is immune to kernel exploits. The lab tests behaviour, not exploits.
- That gVisor makes escape impossible. It reduces host kernel exposure and nothing more.
- That the verifier proves truth. It flags unsupported claims.
- That the ledger is tamper-evident.
- That the API is safe to expose with real data behind it. It has no authentication.
- That the caps bound a fleet. Two concurrent runs is the ceiling, there is no queue, and the rate
  limiter is in memory, per process, and trusts `X-Forwarded-For` unconditionally.
- That containers can never linger. Removal runs in a `finally` now, but a subprocess timeout inside
  inspect or kill is still a path worth watching.

## Reproducing it

The code is MIT-licensed at `github.com/coldall1304/blast-radius-zero`. The live instance is
`https://158.247.240.81.sslip.io`: a `vc2-1c-1gb` Vultr VM in Seoul, TLS valid to 2027-02-01, gVisor
`runsc` as the sandbox runtime, planner and verifier both on Vultr Serverless Inference (`glm-5.3`).
Locally, Docker is enough:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
./deploy/build-sandbox.sh                       # builds and smoke-tests brz-sandbox:1
MOCK_INFERENCE=1 uvicorn app.main:app --port 8000
curl -s localhost:8000/api/config | jq '{runtime, gvisor, limits}'
curl -s -X POST localhost:8000/api/containment/runs   # the seven probes
```

`runtime: "runsc", gvisor: true` means gVisor is in use; `runc` means the weaker path, which is why
the field is published. Probe definitions and judges are in `app/redteam.py`; the threat model is in
`docs/security.md`.

<!--
Notes for the editor

Sources for every factual claim
- Thesis quote, actor trust levels, control table, residual-risk statements: docs/security.md §1-§3.
- 256 MB cap / no swap, 0.5 CPU, pids 64, fsize 32 MB, nofile 256, 64 MB /out, 200 KB output,
  20 s task / 10 s probe budget, 2 concurrent runs, rate limits: docs/security.md §3.4.
- OOM at ~192 MB, BlockingIOError at 62 children, "File too large" at 32 MB, kill at ~10.16 s:
  docs/security.md §3.4 ("Verified" paragraph).
- Suite wall clock ~14.4 s, 250 ms watchdog poll, removal in finally, artifact snapshot after the
  container is gone: docs/architecture.md §5, plus app/sandbox.py:247-257.
- docker run argv excerpt: app/sandbox.py:152-179, trimmed, comments added by us.
- Probe programs, judge semantics, host-process-growth test: app/redteam.py.
- 0/7 early build, 6/7 with `-i` added, secret_read /proc/1/environ self-flag: docs/security.md §5
  and §6. Both defects are now fixed in the tree (app/sandbox.py:153 has "-i"; the probe no longer
  reads /proc/1/environ), so those notes are pre-fix.
- 7/7 on the deployment 2026-11-03, 70 tests (58 at the time of that entry), runsc runtime, TLS to
  2027-02-01: README.md §9, docs/submission.md §3.
- Docker socket as root-equivalent, accepted risks, hardening order: docs/security.md §4.
- Unauthenticated GET /api/runs returning other visitors' data: docs/security.md §3.3.
- Live deployment facts (vc2-1c-1gb, Seoul, sslip.io URL, glm-5.3): README.md intro,
  docs/submission.md §1.
- "verified = not contradicted", fail-open verifier, no hash chain: docs/security.md §3.2, §3.6;
  README.md §10.

Softened or dropped for lack of a source
- Dropped "7/7 contained" as a bare headline claim. The body reports 0/7 -> 6/7 -> 7/7 in order and
  names the doc that is stale, because the count depends on which build you run.
- Did not use the "51 tests" figure from README §8; README §9 and docs/submission.md both say 58
  passed, so only 58 appears.
- Did not claim the gVisor sentry, the seccomp profile or the sandbox image have been audited.
- Did not give per-probe timings except the CPU probe and the suite total; no others exist.
- Did not claim the control plane is network-isolated from the sandbox; they share a host.
- Avoided "immutable ledger" except to deny it.

Owner decisions before publishing
1. Include the live demo URL https://158.247.240.81.sslip.io (and its IP-derived hostname)? It is an
   unauthenticated, rate-limited endpoint; publishing it invites traffic and inference spend.
2. Platform constraints: confirm Vultr Community markdown keeps or strips HTML comments, and remove
   this block if it strips them.
3. Whether to link the 116 s demo video listed in README.md.
4. The 0/7 paragraph is both the most credible and the most awkward part. Recommendation: keep it.
5. docs/security.md §6 still says the suite reports 0/7 as shipped. This post works around it, but
   the repo should not contradict itself; fix or annotate before publishing.
-->

# Submission

Target: **lablab.ai × Vultr — Agent Rush Hackathon, Challenge 1: Blast Radius Zero**
(online build 3–8 Nov 2026; on-site 8 Nov, Salt Lake City).

This file has three parts: (1) the submission checklist with the *actual* state of this
repository, (2) the pre-recording gate, and (3) a shot-by-shot script for a 2:30 demo video with
verbatim English narration.

---

## 1. Checklist

Legend: ✅ done and verifiable in this tree · ⚠️ partially done · ❌ not done · 🧩 required by the
platform but not code.

### 1.1 Core requirements

| # | Requirement | Status | Evidence / action |
|---|---|---|---|
| 1 | Public **GitHub repository** with installation instructions | ⚠️ | Code, installer, Dockerfile, tests and docs are in place; no `.git` and no `LICENSE` yet. Action: `git init`, add MIT `LICENSE`, push public |
| 2 | **Installation docs** good enough for a stranger | ✅ | `README.md` §4 (three steps) + `deploy/README.md` (instance size, firewall, gVisor check, rollback) + `.env.example` |
| 3 | Backend **runs on a Vultr VM** | ⚠️ | `deploy/install.sh` provisions Ubuntu 24.04, systemd unit binds `127.0.0.1:8000`, Caddy terminates TLS. Needs an actual VM run + screenshot for proof |
| 4 | **Vultr Serverless Inference** for the LLM | ✅ in code ⚠️ in evidence | `app/inference.py` posts to `/chat/completions` with a Bearer key; default model `glm-5.3`. No key was available on the build host, so a real-model run is still unrecorded |
| 5 | **Public demo URL** | ❌ | Everything needed is committed (`install.sh` + `Caddyfile` + `BRZ_DOMAIN=<ip>.sslip.io` for a DNS-free certificate). Action: run the installer on a VM, then put the URL in README and here |
| 6 | **Recorded demo video** (≤ 3 min) | ❌ | Script in §3. Do not record before §2 passes |
| 7 | **Architecture and use-case description** | ✅ | `README.md` §3, `docs/architecture.md`, `docs/security.md` |
| 8 | **A containment moment on camera** | ❌ (blocked) | The Containment Lab exists, but as shipped no probe program executes (missing `-i` on `docker run`) and the suite reports 0/7. Fix `docs/architecture.md` §7 G1 (+G2) first, then film the Lab |
| 9 | Multi-step agentic workflow, real executed results | ✅ design / ⚠️ demo | Plan → execute → verify, ≤ 5 steps, evidence fed back. Real-model behaviour unrecorded without a key |
| 10 | Production-style web app (not a notebook) | ⚠️ | Console / Lab / Ledger UI, SSE streaming, SQLite ledger, per-IP limits, systemd + TLS + security headers. Missing: auth, `/metrics`, artifact retention job |
| 11 | Automated tests | ⚠️ | `tests/` has 51 pytest cases (unit + Docker-marked integration). Current run: 43 passed, 8 failed — all 8 are the missing `--interactive` (G1). Green suite is a submission gate |
| 12 | Sandbox image reproducible | ✅ | `sandbox/Dockerfile` pinned (`numpy==2.5.4`, `pandas==3.1.0`, `matplotlib==3.11.2`); `deploy/build-sandbox.sh` builds and smoke-tests it. Verified: `brz-sandbox ok 2.5.4 3.1.0 3.11.2` |

### 1.2 The four containment focus areas (official framing)

| Focus area | Implemented | Where | Filmed |
|---|---|---|---|
| Process isolation — execution never touches host or app runtime | ✅ | fresh container, `--network none`, `--read-only`, uid 65534, caps dropped, gVisor when present | shot 4a |
| Secret hygiene — no keys/credentials in the sandbox | ✅ | no `-e` for the key; six non-secret env vars; no host mounts but `/out` | shot 4e (optional) |
| Resource limits — time and memory caps on every run | ✅ | 256 MB, 0.5 CPU, 64 PIDs, 20 s watchdog, 64 MB `/out`, 200 KB output | shots 4b, 4c |
| Lifecycle discipline — reset or destroy after each task | ✅ | `--rm=false` + `docker rm -f` on every path; verified 0 leftover containers | shot 5 (mention + `docker ps` if desired) |

### 1.3 lablab.ai platform submission fields

| Field | Status |
|---|---|
| Project title, short + long description | ⚠️ README can be pasted; keep the long description ≤ 1 000 words |
| Technology & category tags | TODO: FastAPI, Docker, gVisor, Vultr Serverless Inference, sandboxing, AI agents |
| Cover image | TODO |
| Slide presentation (separate asset from the video) | TODO — reuse the architecture diagram + Lab screenshots |
| Demo application platform + application URL | TODO — same as item 5 |
| Open-source license | TODO — MIT recommended; template files should mention it |

### 1.4 Judging-relevant differentiators (what to point at)

- The **Containment Lab is not a marketing page**: it runs the attacks on the same code path as the
  agent and writes the verdict to the ledger. A failing probe stays visible.
- The **verifier** is a second model call that audits claims against recorded output, and the run
  ends `unverified` when it cannot be supported.
- The **UI shows the actual enforcement values** (`/api/config` → limits strip: network, memory,
  cpu, pids, wall, rootfs, user, caps, runtime) rather than a badge that says "secure".

---

## 2. Pre-recording gate

Do not start recording until every line is true.

| Gate | How to check |
|---|---|
| G1 fixed: `--interactive` in `app/sandbox.py` argv | a step in the Console prints its own output; `pytest -q` goes green |
| G2 fixed: `secret_read` no longer self-fails | Lab shows **7 / 7 contained** |
| G3 fixed (optional but recommended): steps persisted | `curl /api/runs/<id>` has non-empty `steps` |
| G4 fixed (optional): artifact link works | click an artifact in the UI, file downloads |
| Real inference key present, model responds in < ~10 s | one warm-up run before recording |
| Demo VM reachable over HTTPS, UI font loads | open in an incognito window |
| Browser zoom 110–125 %, window 1920×1080, no bookmarks bar | screenshot test |
| `.env`, key, IP, SSH prompt never visible | do a full-screen dry pass |
| Ledger pre-seeded with 2–3 earlier runs (optional) | makes the Ledger shot look alive |

If the key is unavailable on recording day, do **not** film `MOCK_INFERENCE=1` runs as if they were
real: the UI marks them `mock` and mock runs always end `unverified`. Film the Lab instead (it needs
no key) and say plainly that the planner is mocked in this take.

---

## 3. Demo video script (2:30)

Format: 1080p screen capture, cursor highlighting on, no music under narration, hard cuts, captions
for the four containment moments. Total 2:30 ± 10 s. Every narration line below is final English
copy — read it as written.

### Shot 1 — 0:00–0:18 · Cold open (Console, empty state)

**On screen:** the Console tab, empty task box, limits strip visible at the top.
**Action:** no clicking. Slow 5 % zoom-in on the limits strip as the narration names it.

> **Narration:** "Every agent demo shows you a model writing code. Almost none of them show you the
> box that code runs in. This is Blast Radius Zero: an agent that executes real Python on a Vultr
> VM, where every single action is confined to a disposable container — and every containment claim
> is something you can watch it prove, live."

### Shot 2 — 0:18–0:38 · Architecture (browser tab with `README.md` §3 diagram, or a slide)

**On screen:** the ASCII architecture diagram, or a clean redraw of it.
**Action:** highlight three zones in order: control plane → inference → sandbox; then the ledger.

> **Narration:** "Three pieces. A FastAPI control plane that owns the API key and the run loop. The
> planner, running on Vultr Serverless Inference, which may only reply with one JSON object per
> turn: run this program, or finish with claims. And one fresh container per step — no network,
> read-only root filesystem, no capabilities, half a CPU, 256 megabytes, twenty seconds. When the
> model says finish, a separate verifier call audits its claims against the bytes the sandbox
> actually produced."

### Shot 3 — 0:38–1:10 · Real task, real execution (Console)

**On screen:** type the task, dispatch, watch steps stream.
**Action:** paste the task, press **Dispatch to sandbox**, then scroll with the stream. Do not cut
away while the step runs; the point is that it is live.

**Task text (paste verbatim):**

```
Generate a 240-row synthetic sales dataset, compute monthly revenue and growth,
find the top 3 regions, and write a summary CSV to /out. Print the first rows and
the exact file size you wrote.
```

> **Narration:** "One task, one sentence. Watch what the agent is allowed to do with it: it writes a
> program, the control plane hands that program to a brand-new container, and streams back stdout
> line by line. No shell on the host. No credentials. No network device. The only thing that crosses
> the boundary is this step's output directory."

**If the run finishes in one step:** stay on the answer box and continue.
**If it takes a second step:** let it — say "and when a step fails, the model sees the stderr and
has to fix that exact error on the next turn", then continue.

### Shot 4 — 1:10–2:05 · The containment moment (Containment Lab) — **the required shot**

**On screen:** Containment Lab tab, seven probe cards, **Run all 7 probes**.
**Action:** click once, then let the cards flip one by one. Zoom on each card as it resolves.

> **Narration (intro, ~8 s):** "Now the part this challenge actually asks for: the agent has to try
> to break something, on camera. These seven attacks run through the exact same sandbox function the
> agent just used. Nothing here is simulated."

**4a — read-only root filesystem (≈ 1:20)**
Card: *Tamper with the system filesystem*. Code shown: `open('/etc/passwd','a').write(...)`.

> **Narration:** "A write to `/etc/passwd` — the same write path `rm -rf /` needs. The root
> filesystem is mounted read-only, so the kernel answers with EROFS. The container never had
> permission to damage the host, and now it cannot even damage itself."

**4b — fork bomb (≈ 1:33)**
Card: *Fork bomb*. Output: `PROC_LIMIT_HIT 62 BlockingIOError`.

> **Narration:** "A fork bomb. The PID cgroup is capped at sixty-four, so `fork()` returns
> resource-unavailable after sixty-two children. It does not slow the machine down — it runs out of
> allowed processes."

**4c — infinite loop (≈ 1:43)**
Card: *Infinite loop*. Evidence line: killed after ~10 s, reason `timeout`.

> **Narration:** "An infinite loop. The control plane does not trust the container to stop itself:
> a watchdog kills it at the wall-clock limit, and the ledger records the kill reason and the exact
> duration."

**4d — egress (≈ 1:52)**
Card: *Phone home*. Output: `EGRESS_BLOCKED`.

> **Narration:** "And the one that matters most for prompt injection: it tries to phone home. The
> network namespace has no route at all, so there is nowhere for stolen data to go — because there
> is no data in the box to begin with."

**4e — optional, only if time allows (≈ 2:00)**
Card: *Hunt for credentials* → env dump.

> **Narration:** "The environment inside the sandbox holds build metadata and nothing else. The
> inference key never leaves the control-plane process."

**If a probe shows BREACHED on the day:** do not cut it. Say: "That one is not contained in this
build — and it stays on the dashboard, because a containment demo that hides a failed probe is
just a marketing video." Then stop the take, fix, re-record. The gate in §2 exists to avoid this.

### Shot 5 — 2:05–2:22 · Ledger and lifecycle (Ledger tab)

**On screen:** Ledger, newest-first rows: the task run and the probe suite, with verdicts.
**Action:** click the probe row to show counts and verdicts; optionally cut to a terminal running
`docker ps -a --filter name=brz-` returning nothing.

> **Narration:** "Every run, every step, every probe lands in this ledger with an exit code and a
> duration. And after each step the container is destroyed — not paused, not reused. Nothing
> survives except the output you asked for."

### Shot 6 — 2:22–2:35 · Close (back to Console answer, or a title card)

> **Narration:** "Blast Radius Zero. The model gets a box, not a machine. If it does something
> wrong, the blast radius is zero — and you can check that claim yourself, in the Lab, in about
> fifteen seconds."

**End card:** project name · demo URL · GitHub URL · "Built on Vultr · FastAPI · Docker · gVisor".

---

## 4. Recording notes

- Say the numbers that are on screen (256 MB, 0.5 CPU, 64 PIDs, 20 s, 64 MB) — they come from
  `/api/config`, so they cannot drift from the running system.
- Never say "fully secure", "unhackable", "zero risk". Say "contained", "bounded", "no route".
- The Lab needs no inference key, so it can always be filmed; the planner shot cannot.
- Keep the cursor still while narrating a number; move it only to the next card.
- Target 2:30, hard cap 3:00. If over, cut shot 2 to 12 s and drop 4e.

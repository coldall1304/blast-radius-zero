# Submission package

Target: **lablab.ai × Vultr — Agent Rush Hackathon, Challenge 1: Blast Radius Zero.**
Online build 3–8 Nov 2026 · submissions close **2026-11-09 07:00 CST** (8 Nov, 23:00 UTC).

Everything below was produced by running the real system on the real deployment. Nothing here is
aspirational: every number came out of the running service.

---

## 1. Live artefacts

| Artefact | URL | Proof |
|---|---|---|
| Public demo application | https://158.247.240.81.sslip.io | `/api/health` → 200, TLS via Caddy + `sslip.io` |
| Demo video (116 s, narrated) | https://158.247.240.81.sslip.io/static/brz-demo.mp4 | served as `video/mp4`, 3.1 MB |
| Source repository | https://github.com/coldall1304/blast-radius-zero | public, MIT |
| Vultr VM | `vc2-1c-1gb`, Seoul (`icn`), 158.247.240.81 | created through the Vultr API by `deploy/vultr_provision.py` |
| Vultr Serverless Inference | `https://api.vultrinference.com/v1`, model `glm-5.3` | planner, verifier and finalizer all call it |

The instance was created end-to-end by code in this repository — API-side firewall group and SSH
key, cloud-init bootstrap, systemd unit, Caddy TLS, gVisor runtime, sandbox image build. See
`deploy/`.

---

## 2. What the graders asked for, and where it is

### 2.1 Submission expectations

| Requirement | Status | Where |
|---|---|---|
| GitHub repository with setup and documentation | ✅ | this repo, `README.md` §4, `deploy/README.md` |
| Vultr VM backend deployment | ✅ | 158.247.240.81 (`vc2-1c-1gb`, Seoul) |
| Public demo URL | ✅ | https://158.247.240.81.sslip.io |
| Recorded demo video | ✅ | 116 s, narrated, `app/static/brz-demo.mp4` |
| Architecture and use-case explanation | ✅ | `README.md` §3, `docs/architecture.md`, `docs/security.md` |
| One containment moment shown in the video | ✅ | 88–101 s: seven live attacks, all contained |

### 2.2 Technology requirements for Challenge 1

| Requirement | How it is satisfied |
|---|---|
| Vultr Cloud Compute as the central control layer | the FastAPI control plane, planner loop, SSE stream, ledger and every container run on the Vultr VM |
| Vultr Serverless Inference (required for C1) | `app/inference.py` — OpenAI-compatible `/chat/completions`, model `glm-5.3`, key held only in the host process |
| Isolated execution | one fresh container per step: `--network none`, read-only rootfs, uid 65534, `--cap-drop ALL`, 256 MB, 0.5 CPU, 64 PID cap, 20 s watchdog, 64 MB `/out`, 200 KB output cap, gVisor (`runsc`) when available |
| Vultr API for throwaway instances | `deploy/vultr_provision.py` creates the firewall group, SSH key, instance and domain in one call |

### 2.3 The four containment focus areas

| Focus area | Mechanism | Evidence in the shipped build |
|---|---|---|
| Process isolation | fresh container, no network namespace, read-only rootfs, dropped caps, unprivileged uid | `rootfs_write` → `EROFS/PermissionError raised by the kernel`; `egress` → `no route in network-none namespace` |
| Secret hygiene | the inference key never enters the sandbox; six non-secret env vars only | `secret_read` → `no sensitive path was readable and no credential env var exists` |
| Resource limits | memory, CPU, PIDs, wall clock, disk and output caps on every run | `fork_bomb`, `cpu_runaway`, `memory_bomb`, `disk_fill` — all contained |
| Lifecycle discipline | `docker rm -f` on every exit path; per-step artifact snapshot | `app/sandbox.py` teardown; ledger records one row per step |

---

## 3. Verified evidence (2026-11-03, on the deployed instance)

| Check | Result |
|---|---|
| `pytest` | **58 passed** |
| Real-model task run | `verified: true`; planner and verifier both served by `glm-5.3` |
| Artifacts | `walk.json`, `walk.png` downloadable over HTTPS from `/api/runs/<id>/artifacts/...` |
| Containment suite | **7/7 contained** — `CONTAINMENT VERIFIED`, per-probe kernel evidence recorded |
| Runtime | `runsc` + gVisor active; `/api/config` publishes the enforcement values actually in use |

Two things are worth reading closely in the code:

- **The verifier is not decorative.** A reasoning model (`glm-5.3`) spent its whole completion
  budget on hidden reasoning and returned empty content, which failed planning outright. The fix
  (forward `reasoning_effort`, size each role's token budget, escalate on retry) is unit-tested in
  `tests/test_inference_guards.py`.
- **The fork-bomb probe measures the host, not the victim's own words.** gVisor blocks the forker at
  the PID cap instead of returning `EAGAIN`, so `PROC_LIMIT_HIT` would be a false negative. The
  probe now measures host process-count growth (≤ cap + slack) and whether the flood was cut off —
  runtime-agnostic, and unit-tested for both runtimes in `tests/test_redteam_judge.py`.

---

## 4. lablab.ai submission form — values to paste

**Project title**

```
Blast Radius Zero — containment-first agent runtime on Vultr
```

**Short description**

```
A web agent that plans a task, runs the generated Python inside a throwaway container on a Vultr
VM, then has an independent verifier audit every claim against the bytes the sandbox actually
produced. A built-in containment lab re-runs seven real attacks — fork bomb, infinite loop, egress,
secret hunting, memory bomb, disk fill, rootfs tampering — through the same code path and shows the
kernel's verdict: 7/7 contained.
```

**Long description**

```
Blast Radius Zero is a containment-first agent runtime, deployed on Vultr.

A user types a task. The planner (Vultr Serverless Inference, glm-5.3) proposes exactly one action
per turn and writes a complete Python program for it. That program never runs on the host: it runs
in a fresh container created for that single step — no network, read-only root filesystem,
unprivileged user, all capabilities dropped, 256 MB of memory, half a CPU, a 64-process cap and a
20-second watchdog. Files written under /out are snapshotted per step and served back over HTTPS as
artefacts.

When the agent decides it is finished, a second model call audits the answer against the recorded
evidence — stdout, exit codes, artefact list. If a concrete claim is not supported, the run is not
marked verified. The UI shows the raw program, the raw output and the verdict side by side, and a
SQLite ledger keeps the whole audit trail.

The second half of the app is the Containment Lab: seven real attacks executed through the exact
same sandbox path the agent uses — tampering with the system filesystem, a fork bomb, an infinite
loop, network egress, credential hunting, a memory bomb and a disk-filling write. It reports what
the kernel did, per probe. On the deployed instance all seven are contained, with the kernel's own
messages as evidence (EROFS, OOM-kill, no route, watchdog kill).

Stack: FastAPI + SSE, Docker with gVisor/runsc, Caddy for TLS, systemd, SQLite, Vultr Cloud Compute
and Vultr Serverless Inference. The VM is provisioned and deployed entirely by scripts in the
repository.
```

**Technology tags** (nearest matches the form offers)

```
Python, FastAPI, Docker, gVisor, Sandboxing, AI Agents, LLM, Vultr, Vultr Serverless Inference
```

**Demo application URL** `https://158.247.240.81.sslip.io`

**Video URL** `https://158.247.240.81.sslip.io/static/brz-demo.mp4`

**Repository** `https://github.com/coldall1304/blast-radius-zero`

**Cover image** `docs/cover.png`

**Screenshots** `docs/shot-console.png`, `docs/shot-steps-streaming.png`,
`docs/shot-containment-lab.png`, `docs/shot-limits-strip.png`

---

## 5. Video record (as shipped)

Single continuous screen capture, `plan -> dispatch -> verify` title card, English narration, no
cuts: dashboard (runtime + model badges) → task typed in natural language → planner writes the
program → sandbox streams stdout → verifier marks the answer `verified` → Containment Lab, seven
probes, **7/7 contained**. The take also shows an honest failure: step 1 exits non-zero, the model
reads stderr and repairs it on the next turn.

## 6. Steps that need the account holder

1. **Screening form** — `https://lablab.ai/ai-hackathons/vultr-hackathon/signup` asks for personal
   details (full name, country/state, GitHub and LinkedIn profiles, age bracket, professional
   background, AI experience, travel availability). The event page still shows *Waiting for
   approval*; the submit button appears once the entry is approved.
2. **Hackathon gift code** — issued at kickoff (23:00 CST, 3 Nov). Apply at Billing → hackathon
   promotion page and confirm `Remaining Credit = $200.00`.
3. **Submit project** — after approval, using the values in §4.

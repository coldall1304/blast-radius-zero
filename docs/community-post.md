# Community post

**Suggested title:** Containment first: running an agent's generated code in a box you can measure

---

Most agent demos ask what the model can build. This one asks what happens when the model builds
something hostile, and answers with the kernel's own error messages.

Blast Radius Zero takes a task in plain English, plans it, writes a Python program, runs that program
in a fresh container, and returns an answer that has been checked against the bytes the sandbox
produced. Nothing is mocked. The same execution path the agent uses is also exposed as a containment
lab, where seven adversarial programs run on demand: tampering with the system filesystem, a fork
bomb, an infinite loop, network egress, credential hunting, a memory bomb and a disk-filling write.
Each probe records what the kernel said — `EROFS` on the read-only root, a socket failure with no
route, an OOM kill at roughly 192 MB, a `BlockingIOError` at 62 children against a PID cap of 64,
`File too large` at 32 MB, a watchdog kill at about 10.16 s against a 10 s budget.

The deployment is deliberately small. The control plane and the sandbox run on one `vc2-1c-1gb`
Vultr instance in Seoul. gVisor (`runsc`) provides the sandbox runtime, so the workload talks to a
user-space kernel and the host kernel sees only the sentry's syscall set. The planner and the
verifier both call Vultr Serverless Inference (`glm-5.3`); the inference key lives in the control
plane's process memory and is never injected into the container, which is why one probe dumps the
sandbox environment and finds only base-image build metadata.

What the project demonstrates: that containment claims can be executable and re-run by anyone, that
a step can be bounded in memory, CPU, PIDs, wall clock, file size and output volume at the same time,
and that a ledger can hold the raw evidence next to the claim it supports.

What it deliberately does not claim: that the sandbox resists kernel exploits — it does not test
exploits, it tests behaviour; that gVisor makes escape impossible, rather than reducing exposure;
that the verifier proves an answer true, rather than flagging claims the recorded output does not
support; or that the Docker socket boundary is safe. The control plane holds the socket, and that is
root-equivalent, so the containment guarantees hold for the agent's code and none hold for a
compromised control plane. That limitation is documented, not glossed.

The code is MIT-licensed and the full threat model, residual risks and implementation gaps are in
the repository alongside it.

- Repository: https://github.com/coldall1304/blast-radius-zero
- Live demo: https://158.247.240.81.sslip.io (public, unauthenticated, per-IP rate limited — it is a
  demo, not a service)

<!--
Notes for the editor

Sources
- Live deployment (vc2-1c-1gb, Seoul, sslip.io URL, glm-5.3 on Vultr Serverless Inference, runsc,
  TLS to 2027-02-01): README.md intro and §9; docs/submission.md §1.
- Probe list and measured outcomes (EROFS, no route, OOM at ~192 MB, BlockingIOError at 62 against
  64, File too large at 32 MB, kill at ~10.16 s): docs/security.md §3.1 and §3.4; docs/architecture.md
  §5; probe programs in app/redteam.py.
- Key kept in process memory, six non-secret env vars, env dump evidence: docs/security.md §3.3 and
  §5; README.md §3.
- gVisor as reduced kernel surface, not solved: docs/security.md §3.5.
- Verifier semantics ("flags unsupported claims", not proof): docs/security.md §3.2 and §6;
  README.md §10.
- Docker socket root-equivalent: docs/security.md §4; README.md §10.

Softened or dropped
- No "7/7" headline. The docs disagree (docs/security.md §6 says 0/7 as shipped and 6/7 with `-i`
  added; README.md §9 and docs/submission.md say 7/7 after the fixes), so the post cites per-probe
  evidence instead of a score.
- Did not claim the lab is open to anonymous use without limits; it is rate limited per IP.
- Did not mention the API's unauthenticated read endpoints here — kept for the long-form post, since
  a 400-word showcase cannot carry the caveat properly. Consider adding one clause if the owner
  prefers maximum disclosure.
- Did not claim any benchmark, uptime SLA or cost figure; none exists in the repo.
- Did not claim the sandbox image or seccomp profile were audited.

Owner decisions before publishing
1. Include the live demo URL and raw hostname? It is unauthenticated and spends inference budget on
   traffic.
2. Whether to cross-post the same text to the lablab.ai submission page (it overlaps docs/submission.md).
3. Whether to add the API-leak caveat (unauthenticated GET /api/runs) to this short post as well.
4. Confirm the platform keeps or strips HTML comments; delete this block if stripped.
-->

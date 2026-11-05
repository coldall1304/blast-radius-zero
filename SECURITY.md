# Security Policy

This project executes code written by a language model. That is the point of it, and
it is also the reason the containment layer is the most heavily tested part of the codebase.

## Reporting a vulnerability

Please do **not** open a public issue for a security problem. Report it privately:

- Use GitHub's [private vulnerability reporting](https://github.com/coldall1304/blast-radius-zero/security/advisories/new)
  (**Security** tab → **Report a vulnerability**). This is the preferred channel.

Include what you did, what you expected, and what happened. A run id or the task text
that triggered it is the most useful thing you can give. If you can reproduce it against
the public demo, say so — but do not run anything that could cost the operator money
or attack infrastructure that is not yours.

Expect an acknowledgement within a few days. This is a solo project, so there is no
formal SLA, but every report gets a real answer.

## In scope

Anything that breaks one of the containment guarantees:

- code from a run escaping its sandbox, or reaching the network
- a run reading or writing files outside its own `/out` tree
- a run surviving its step (no reaping after the watchdog or the PID cap)
- the verifier being bypassed so an unaudited answer is reported as `verified: true`
- the inference key, the Vultr API key or any other secret reaching a run or the client
- a request that makes the host run out of memory, disk or process slots

## Out of scope

- the demo being rate limited, slow, or offline (it is a demo, not a service)
- the model producing wrong or useless code — that is a correctness issue, not a
  containment one; the verifier is what catches it
- vulnerabilities in third-party services (Vultr, gVisor, the model provider) — report
  those upstream, though a note here is welcome if it affects this deployment
- findings from an automated scanner with no demonstrated impact

## What this deployment already enforces

Per step, the generated program runs in a fresh container with: no network, a read-only
root filesystem, an unprivileged user, all capabilities dropped, a memory cap, a CPU
quota, a PID cap, and a wall-clock watchdog. The `Containment Lab` in the app re-runs
seven real attacks through the same code path on demand and reports the kernel's own
evidence for each one. The [README](README.md) and [`docs/security.md`](docs/security.md)
describe the measured results rather than the intended ones.

## Please do not

Do not test against the public demo with anything that would run up the operator's
Vultr bill, and do not use it as a launchpad for attacks against other hosts. If you
want to experiment freely, run it locally — `make lab` needs no cloud account.

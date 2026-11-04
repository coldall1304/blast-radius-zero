---

name: Feature request
about: Propose a change to containment, the agent loop, or the evidence trail
labels: enhancement
---

## What you want

<!-- One or two sentences. -->

## Which part of the system

- [ ] Containment (a new limit, a new probe, a stronger default)
- [ ] Agent loop (planning, verification, step budget)
- [ ] Control plane / API / UI
- [ ] Ledger and stats
- [ ] Sandbox image (interpreter surface, pinned packages)
- [ ] Deployment
- [ ] Docs

## Why it matters for this project

This project optimises for one thing: an autonomous agent runs code it wrote,
and nothing it does can reach the host, the network, or a secret. Explain how
the change strengthens that, or what it currently costs.

## What it would cost

- Does it weaken any existing limit (network, rootfs, capabilities, uid,
  memory, PIDs, wall clock, disk)? If so, which, and what compensates?
- Does it need a new dependency in the sandbox image? Pinned versions only —
  the image is rebuilt months later and must still produce the same interpreter
  surface.
- Does it need a secret? The default configuration ships with `MOCK_INFERENCE=1`
  and no key; anything that requires a key needs a path that works without one.

## How it would be proven

A containment claim needs a probe, not a sentence. Describe the test or the
Containment Lab entry that would show the change works, and what it should
print when it is blocked.

```

```

## Alternatives considered

<!-- Including "do nothing" and why that is not good enough. -->

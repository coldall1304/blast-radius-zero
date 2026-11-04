## What changed

<!-- One paragraph. If it touches containment, say which limit and in which direction. -->

## Which limits does this touch

- [ ] Does not touch the sandbox argv or the image
- [ ] Changes `app/sandbox.py`'s docker argv (network, rootfs, user, caps, memory, PIDs, wall clock, disk)
- [ ] Changes the sandbox image (`sandbox/Dockerfile`) or its pinned packages
- [ ] Changes the agent step loop, verifier, or step budget
- [ ] Changes the ledger schema or stats
- [ ] Docs only

If any limit got weaker, say so plainly and say what compensates for it. "No
change" is a fine answer; an unreviewed weakening is not.

## How it was verified

- [ ] `pytest -m 'not integration' --ignore=tests/test_api.py` passes
- [ ] `pytest -m integration` passes against a real Docker daemon
- [ ] `pytest tests/test_api.py` passes (needs the sandbox image)
- [ ] `bash deploy/build-sandbox.sh` rebuilds and smoke-tests the image
- [ ] Containment Lab still reports 7/7 (`POST /api/containment/runs`)

Which runtime did you test on, and does the result differ between them?

- [ ] `runsc` (gVisor)
- [ ] `runc`
- [ ] Both

Paste the relevant output rather than describing it:

```

```

## Evidence

Run ids, probe results, or the `containment-report.json` from a suite run. If
this PR claims a containment property, the probe output is the review material.

## Reviewer notes

<!-- Anything you are unsure about, deliberately left out, or want a second pair of eyes on. -->

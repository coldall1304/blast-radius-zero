---

name: Bug report
about: Something behaved differently from what the docs say
labels: bug
---

## What happened

<!-- What you expected, and what actually happened. -->

## Where

- [ ] Control plane / HTTP API
- [ ] Sandbox execution (a step's program)
- [ ] Containment Lab probe
- [ ] Sandbox image build
- [ ] Verifier / agent loop
- [ ] Ledger (SQLite) or stats
- [ ] Docs

## Reproduction

Exact task text submitted to `POST /api/runs` (or the probe id for the Containment Lab), verbatim:

```

```

Run id from the response or `/api/runs` (format `r-xxxxxxxxxx` / `probe-xxxxxxxxxx`):

```

```

## Environment

Output of:

```bash
curl -s http://127.0.0.1:8000/api/config | jq '{runtime, gvisor, image_present, limits}'
docker version --format '{{.Server.Version}}'
docker image inspect --format '{{.Id}}' brz-sandbox:1
python3 --version
```

```json

```

Which sandbox runtime was active:

- [ ] `runsc` (gVisor)
- [ ] `runc`
- [ ] Not sure — the `runtime` field above is what the app reported

If the bug is about containment, say whether it also reproduces under the other
runtime. A probe that escapes under `runc` but is contained under `runsc` is a
different bug from one that escapes under both.

## Evidence

For a step or probe failure, paste the recorded `stdout`, `stderr`, `exit_code`
and `killed_reason` from `GET /api/runs/{run_id}` — not a screenshot of the UI.
Artifacts under `data/artifacts/{run_id}/` are useful when the program wrote
files.

```

```

## Anything you already ruled out

<!-- e.g. "reproduced on a clean clone", "only with pandas imported", "only when the image is freshly pulled" -->

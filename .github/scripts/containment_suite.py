#!/usr/bin/env python3
"""Drive the Containment Lab through the control plane's HTTP API.

The control plane exposes the whole suite as one endpoint:

    POST /api/containment/runs  ->  {"run_id": "probe-..."}
    GET  /api/runs/{run_id}     ->  run record whose ``payload`` is the suite
                                    summary: {"contained", "total", "all_blocked",
                                    "results": [{"id", "blocked", "evidence", ...}]}

This script starts the suite, polls the ledger-backed run record until it
reaches a terminal status, writes the evidence to ``--out`` as JSON, and exits
non-zero unless every declared probe was actually run *and* judged contained.

It talks HTTP only, so it exercises the same surface a browser does.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time

import httpx

TERMINAL = {"done", "error", "unverified"}


def _summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _wait_ready(client: httpx.Client, timeout_s: float, poll_s: float) -> dict:
    """Wait for the ASGI server to answer at all; return its health body.

    ``ok`` is False when the sandbox image is missing, which is a *readiness*
    answer, not a reason to keep polling for two minutes.
    """
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        try:
            resp = client.get("/api/health")
            if resp.status_code == 200:
                return resp.json()
            last = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(poll_s)
    raise RuntimeError(f"control plane did not answer within {timeout_s:.0f}s ({last})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--out", required=True, help="path for the evidence JSON")
    ap.add_argument("--expect-probes", type=int, default=7)
    ap.add_argument("--ready-timeout", type=float, default=120.0)
    ap.add_argument("--suite-timeout", type=float, default=600.0)
    ap.add_argument("--poll", type=float, default=2.0)
    args = ap.parse_args()

    report: dict = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "base_url": args.base_url,
        "expected_probes": args.expect_probes,
        "passed": False,
        "failure": None,
    }

    def finish(code: int, failure: str | None = None) -> int:
        report["passed"] = code == 0
        report["failure"] = failure
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
        if failure:
            print(f"CONTAINMENT SUITE FAILED: {failure}", file=sys.stderr)
            _summary(["## Containment Lab: FAILED", "", f"`{failure}`"])
        return code

    with httpx.Client(base_url=args.base_url, timeout=60.0) as client:
        try:
            health = _wait_ready(client, args.ready_timeout, args.poll)
            report["health"] = health
            if not health.get("ok"):
                return finish(1, f"sandbox image {health.get('image') or 'brz-sandbox:1'} is not present on the runner")
            config = client.get("/api/config").json()
            report["config"] = {
                "runtime": config.get("runtime"),
                "gvisor": config.get("gvisor"),
                "image_present": config.get("image_present"),
                "limits": config.get("limits"),
            }
            print(
                f"control plane ready: runtime={config.get('runtime')} "
                f"gvisor={config.get('gvisor')} image_present={config.get('image_present')}",
                flush=True,
            )
            if not config.get("image_present"):
                return finish(1, "control plane reports image_present=false")

            created = client.post("/api/containment/runs")
            if created.status_code != 200:
                return finish(1, f"POST /api/containment/runs returned {created.status_code}: {created.text[:300]}")
            run_id = created.json()["run_id"]
            report["run_id"] = run_id
            print(f"suite started: {run_id}", flush=True)

            deadline = time.monotonic() + args.suite_timeout
            run: dict = {}
            while time.monotonic() < deadline:
                fetched = client.get(f"/api/runs/{run_id}")
                if fetched.status_code != 200:
                    return finish(1, f"GET /api/runs/{run_id} returned {fetched.status_code}: {fetched.text[:300]}")
                run = fetched.json()
                if run["status"] in TERMINAL:
                    break
                time.sleep(args.poll)
            else:
                return finish(1, f"suite did not finish within {args.suite_timeout:.0f}s (status={run.get('status')})")

            # db.finish_run() stores the suite summary itself as the run payload.
            payload = run.get("payload") or {}
            summary = payload if "all_blocked" in payload else payload.get("containment")
            report["run"] = {
                "id": run.get("id"),
                "status": run.get("status"),
                "answer": run.get("answer"),
                "verified": run.get("verified"),
            }
            report["containment"] = summary
            report["ledger_probes"] = [
                {k: p.get(k) for k in ("probe_id", "title", "blocked", "evidence")}
                for p in run.get("probes", [])
            ]
            if summary is None:
                return finish(1, f"run {run_id} finished without a containment summary (status={run.get('status')})")

            print("", flush=True)
            print(f"{'PROBE':<14} {'VERDICT':<9} {'ms':>6}  EVIDENCE", flush=True)
            for row in summary["results"]:
                verdict = "BLOCKED" if row.get("blocked") else "ESCAPED"
                print(
                    f"{row.get('id',''):<14} {verdict:<9} {row.get('duration_ms') or 0:>6}  "
                    f"{' '.join((row.get('evidence') or '').split())[:180]}",
                    flush=True,
                )
            print("", flush=True)

            contained = summary.get("contained")
            total = summary.get("total")
            all_blocked = summary.get("all_blocked")
            print(f"contained {contained}/{total}; all_blocked={all_blocked}; run status={run['status']}", flush=True)

            rows = [
                f"| {row.get('id')} | {'blocked' if row.get('blocked') else 'ESCAPED'} | "
                f"{row.get('duration_ms')} ms | {' '.join((row.get('evidence') or '').split())[:160]} |"
                for row in summary["results"]
            ]
            _summary(
                [
                    f"## Containment Lab: {contained}/{total} attacks contained",
                    "",
                    f"runtime `{report['config']['runtime']}` · gVisor `{report['config']['gvisor']}` · run `{run_id}`",
                    "",
                    "| probe | verdict | duration | evidence |",
                    "|---|---|---|---|",
                    *rows,
                ]
            )

            escaped = [r.get("id") for r in summary["results"] if not r.get("blocked")]
            if escaped:
                return finish(1, f"{contained}/{total} attacks contained; ESCAPED: {', '.join(escaped)}")
            if total != args.expect_probes:
                return finish(1, f"suite ran {total} probes, expected {args.expect_probes}")
            if len(summary["results"]) != args.expect_probes:
                return finish(1, f"suite returned {len(summary['results'])} probe rows, expected {args.expect_probes}")
            if contained != total or all_blocked is not True:
                return finish(1, f"{contained}/{total} contained but all_blocked={all_blocked}")
            if run["status"] != "done":
                return finish(1, f"all probes blocked but the run finished with status {run['status']}")
            return finish(0)
        except (httpx.HTTPError, RuntimeError, ValueError, KeyError) as exc:
            return finish(1, f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())

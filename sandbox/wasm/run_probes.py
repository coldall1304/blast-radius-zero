"""Run the 7 adversarial probes from app/redteam.py against the WASM backend.

    .venv/bin/python sandbox/wasm/run_probes.py

Prints one JSON object per probe (stdout/stderr/killed_reason/duration/artifacts)
so the verdicts in docs/backends.md can be re-derived without trusting the doc.
Nothing is written to the repository; artifacts live under the configured
DATA_DIR and are cleaned up afterwards.
"""
from __future__ import annotations

import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app import redteam, wasm_sandbox  # noqa: E402


def main() -> int:
    if not wasm_sandbox.available():
        print("wasm assets missing; see docs/backends.md", file=sys.stderr)
        return 2

    rows = []
    for probe in redteam.PROBES:
        started = time.monotonic()
        try:
            result = wasm_sandbox.execute(f"probe-wasm-{probe['id']}", 1, probe["code"], None, 10)
            exc = None
        except Exception as error:  # the sandbox itself failed
            result, exc = None, f"{type(error).__name__}: {error}"
        row = {
            "id": probe["id"],
            "exception": exc,
            "exit_code": result and result["exit_code"],
            "killed_reason": result and result["killed_reason"],
            "timed_out": result and result["timed_out"],
            "duration_ms": result and result["duration_ms"],
            "peak_rss_kb": result and result.get("peak_rss_kb"),
            "stdout": result and result["stdout"][-600:],
            "stderr": result and result["stderr"][-600:],
            "artifacts": result and result["artifacts"],
            "wall_ms": int((time.monotonic() - started) * 1000),
        }
        rows.append(row)
        print(json.dumps(row), flush=True)
        wasm_sandbox.cleanup_run(f"probe-wasm-{probe['id']}")

    expressible = sum(1 for r in rows if "has no attribute 'fork'" not in (r["stderr"] or ""))
    print(json.dumps({"considered": len(rows), "expressible_under_wasi": expressible}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

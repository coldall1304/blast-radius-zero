"""_judge must say "blocked" only when the observed output proves it."""
from __future__ import annotations

import pytest

from app.redteam import PROBES, _judge


def result(stdout="", stderr="", killed=None, oom=False, duration_ms=1200, exit_code=0,
           host_procs_before=0, host_procs_peak=0):
    return {
        "stdout": stdout,
        "stderr": stderr,
        "killed_reason": list(killed or []),
        "oom": oom,
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "host_procs_before": host_procs_before,
        "host_procs_peak": host_procs_peak,
    }


BLOCKED_CASES = [
    (
        "rootfs_write",
        result("WRITE_BLOCKED PermissionError [Errno 30] Read-only file system\n"),
        True,
    ),
    ("rootfs_write", result("WRITE_SUCCEEDED\n"), False),
    ("rootfs_write", result("WRITE_SUCCEEDED\nWRITE_BLOCKED ValueError\n"), False),
    ("rootfs_write", result("", stderr="Traceback: PermissionError\n", exit_code=1), False),
    (
        "fork_bomb",
        result("PROC_LIMIT_HIT 61 OSError [Errno 11] Resource temporarily unavailable\n",
               host_procs_before=70, host_procs_peak=133, exit_code=137),
        True,
    ),
    (
        # gVisor blocks the forker at the cap instead of returning EAGAIN:
        # the flood is cut off by the watchdog and host PIDs stay flat.
        "fork_bomb",
        result("", killed=["timeout"], host_procs_before=70, host_procs_peak=72, exit_code=137),
        True,
    ),
    (
        "fork_bomb",
        result("PROC_LIMIT_NOT_HIT 400\n", host_procs_before=70, host_procs_peak=70, exit_code=0),
        False,
    ),
    (
        "fork_bomb",
        result("PROC_LIMIT_NOT_HIT 400\n", host_procs_before=70, host_procs_peak=600, exit_code=137),
        False,
    ),
    ("cpu_runaway", result("starting runaway loop\n", killed=["timeout"], duration_ms=10_050), True),
    ("cpu_runaway", result("starting runaway loop\n", [], exit_code=1), False),
    ("egress", result("EGRESS_BLOCKED URLError <urlopen error [Errno -3] >\n"), True),
    ("egress", result("EGRESS_ALLOWED\n"), False),
    ("secret_read", result("ENV_KEYS ['HOME', 'PATH']\nSECRET_ENV []\nREADABLE_SENSITIVE none\n"), True),
    ("secret_read", result("SECRET_ENV []\nREADABLE_SENSITIVE ['/etc/shadow']\n"), False),
    ("secret_read", result("SECRET_ENV []\nREADABLE_SENSITIVE none\nSSH key id_rsa found\n"), False),
    ("memory_bomb", result("allocated_mb 192\n", oom=True, exit_code=137), True),
    ("memory_bomb", result("MEMORY_CAPPED MemoryError 192\n"), True),
    ("memory_bomb", result("MEMORY_ALLOWED 4096\n"), False),
    ("disk_fill", result("", killed=["disk"], exit_code=137), True),
    ("disk_fill", result("DISK_CAPPED_MB 64 OSError [Errno 27] File too large\n"), True),
    ("disk_fill", result("wrote 4096 MB\n"), False),
    ("unknown_probe", result("anything\n"), False),
]


@pytest.mark.parametrize("probe_id,observed,expected", BLOCKED_CASES)
def test_judge_verdicts(probe_id, observed, expected):
    blocked, evidence = _judge(probe_id, observed)
    assert blocked is expected
    assert isinstance(evidence, str) and evidence


def test_judge_unknown_probe_explains_itself():
    blocked, evidence = _judge("nope", result("x"))
    assert blocked is False
    assert evidence == "unknown probe"


def test_judge_timeout_evidence_reports_duration():
    _, evidence = _judge("cpu_runaway", result("", killed=["timeout"], duration_ms=4321))
    assert "4321" in evidence


def test_every_declared_probe_has_a_judge_branch():
    judged = {probe_id for probe_id, _, _ in BLOCKED_CASES}
    assert {probe["id"] for probe in PROBES} <= judged

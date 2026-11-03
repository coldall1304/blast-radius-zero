"""Shared test configuration.

Environment variables are set at import time because ``app.config.Settings`` is
instantiated once, when the module is first imported.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

# Must be set before anything imports app.config.
os.environ.setdefault("MOCK_INFERENCE", "1")
os.environ.setdefault("SANDBOX_IMAGE", "brz-sandbox:1")
# Keep repeated local runs from tripping the per-IP demo throttles.
os.environ.setdefault("RUNS_PER_HOUR", "1000")
os.environ.setdefault("PROBES_PER_HOUR", "1000")

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def docker_ready() -> str:
    """Skip (do not fake) the integration tests when Docker cannot be used."""
    from app.config import settings

    if shutil.which("docker") is None:
        pytest.skip("docker CLI not found on PATH")
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=20,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        pytest.skip(f"docker daemon unreachable: {exc}")
    if proc.returncode != 0:
        pytest.skip(f"docker daemon unreachable: {proc.stderr.strip()[:200]}")

    inspect = subprocess.run(
        ["docker", "image", "inspect", settings.sandbox_image],
        capture_output=True, text=True, timeout=20,
    )
    if inspect.returncode != 0:
        pytest.skip(
            f"sandbox image {settings.sandbox_image} missing; build sandbox/Dockerfile first"
        )
    return settings.sandbox_image


@pytest.fixture
def run_ids():
    """Hand out unique run ids and delete their artifact trees afterwards."""
    from app import sandbox

    created: list[str] = []

    def new(prefix: str = "t") -> str:
        run_id = f"{prefix}-{os.urandom(5).hex()}"
        created.append(run_id)
        return run_id

    yield new

    for run_id in created:
        sandbox.cleanup_run(run_id)

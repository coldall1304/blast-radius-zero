"""Runtime configuration for Blast Radius Zero.

Every knob that matters for containment is configurable, but the defaults are
deliberately strict: no network, no secrets, hard caps.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # --- Vultr Serverless Inference (OpenAI compatible) ---
    inference_base_url: str = _env("INFERENCE_BASE_URL", "https://api.vultrinference.com/v1")
    inference_api_key: str = _env("INFERENCE_API_KEY", "")
    inference_model: str = _env("INFERENCE_MODEL", "glm-5.3")
    inference_timeout_s: int = _env_int("INFERENCE_TIMEOUT_S", 90)
    mock_inference: bool = _env_bool("MOCK_INFERENCE", False)

    # --- Sandbox ---
    sandbox_image: str = _env("SANDBOX_IMAGE", "brz-sandbox:1")
    sandbox_runtime: str = _env("SANDBOX_RUNTIME", "auto")  # auto | runsc | runc
    sandbox_memory_mb: int = _env_int("SANDBOX_MEMORY_MB", 256)
    sandbox_cpus: str = _env("SANDBOX_CPUS", "0.5")
    sandbox_pids: int = _env_int("SANDBOX_PIDS", 64)
    sandbox_tmp_mb: int = _env_int("SANDBOX_TMP_MB", 16)
    sandbox_disk_mb: int = _env_int("SANDBOX_DISK_MB", 64)
    sandbox_timeout_s: int = _env_int("SANDBOX_TIMEOUT_S", 20)
    sandbox_output_bytes: int = _env_int("SANDBOX_OUTPUT_BYTES", 200_000)
    sandbox_network: str = _env("SANDBOX_NETWORK", "none")  # none | bridge

    # --- Agent ---
    max_steps: int = _env_int("MAX_STEPS", 5)
    max_verify_retries: int = _env_int("MAX_VERIFY_RETRIES", 1)
    max_task_chars: int = _env_int("MAX_TASK_CHARS", 4000)

    # --- Demo hardening ---
    runs_per_hour: int = _env_int("RUNS_PER_HOUR", 12)
    probes_per_hour: int = _env_int("PROBES_PER_HOUR", 20)
    max_concurrent_runs: int = _env_int("MAX_CONCURRENT_RUNS", 2)

    # --- Storage ---
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", str(ROOT / "data"))))

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "brz.sqlite3"


settings = Settings()

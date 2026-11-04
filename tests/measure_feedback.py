"""Development feedback measurement, never economic evidence. Run explicitly."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    environment = Path("/tmp") / f"v8-next-feedback-{uuid.uuid4().hex}"
    env = dict(os.environ, UV_PROJECT_ENVIRONMENT=str(environment))
    env.pop("VIRTUAL_ENV", None)
    records = []
    commands = [
        ("fresh_environment_cached_downloads", ["uv", "sync", "--extra", "dev", "--locked"]),
        (
            "native_import_first_process",
            [
                "uv",
                "run",
                "--no-sync",
                "python",
                "-c",
                "from nautilus_trader.backtest import BacktestEngine",
            ],
        ),
        (
            "native_import_warm_process",
            [
                "uv",
                "run",
                "--no-sync",
                "python",
                "-c",
                "from nautilus_trader.backtest import BacktestEngine",
            ],
        ),
        (
            "economic_tests",
            [
                "uv",
                "run",
                "--no-sync",
                "pytest",
                "-q",
                "tests/test_economics.py",
                "tests/test_controller.py",
                "tests/test_admission.py",
            ],
        ),
        (
            "native_integration_tests",
            ["uv", "run", "--no-sync", "pytest", "-q", "tests/test_native_engine.py"],
        ),
        ("ruff", ["uv", "run", "--no-sync", "ruff", "check", "src", "tests"]),
        ("mypy", ["uv", "run", "--no-sync", "mypy", "src"]),
    ]
    for name, command in commands:
        started = time.perf_counter()
        result = subprocess.run(
            command, cwd=project, env=env, capture_output=True, text=True, timeout=120
        )
        records.append(
            {
                "name": name,
                "command": command,
                "wall_seconds": time.perf_counter() - started,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        if result.returncode:
            break
    versions = subprocess.run(
        [
            str(environment / "bin/python"),
            "-c",
            "import importlib.metadata,json;print(json.dumps({d.metadata['Name']:d.version for d in importlib.metadata.distributions()}))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as output:
        json.dump(
            {
                "purpose": "DEVELOPMENT_FEEDBACK_NOT_ECONOMIC_EVIDENCE",
                "measured_at_ns": time.time_ns(),
                "python": sys.version,
                "lock_sha256": hashlib.sha256((project / "uv.lock").read_bytes()).hexdigest(),
                "environment": str(environment),
                "installed": json.loads(versions.stdout),
                "measurements": records,
            },
            output,
            indent=2,
        )
    print(args.output)
    if any(record["exit_code"] for record in records):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

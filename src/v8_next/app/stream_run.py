"""Bounded orchestration of native observation sessions and verified restarts."""

import argparse
import asyncio
import hashlib
import math
import time
from pathlib import Path
from typing import Any

from v8_next.app.stream import capture_stream
from v8_next.domain.config import PositioningPolicy
from v8_next.evaluation.store import canonical


async def run_observation(
    destination: Path,
    *,
    manifests: tuple[Path, ...],
    duration_seconds: int,
    max_restarts: int,
    max_quote_silence_ns: int,
    grammar: str = "range-breakout-48-v1",
    positioning_policy: PositioningPolicy | None = None,
    positioning_refresh_seconds: int | None = None,
) -> dict[str, Any]:
    if not manifests or type(max_restarts) is not int or not 0 <= max_restarts <= 10:
        raise ValueError("warmup and a bounded restart count (0..10) required")
    if type(duration_seconds) is not int or duration_seconds <= 0:
        raise ValueError("positive observation time budget required")
    if type(max_quote_silence_ns) is not int or max_quote_silence_ns <= 0:
        raise ValueError("positive quote-silence policy required")
    destination.mkdir(parents=True, exist_ok=False)
    deadline = time.monotonic() + duration_seconds
    sessions = []
    parent = None
    status = "TIME_BUDGET_ENDED"
    for attempt in range(max_restarts + 1):
        remaining = math.ceil(deadline - time.monotonic())
        if remaining <= 0:
            break
        path = destination / f"session-{attempt:03d}"
        result = await capture_stream(
            path,
            remaining,
            manifests=manifests if parent is None else (),
            grammar=grammar,
            positioning_policy=positioning_policy,
            positioning_refresh_seconds=positioning_refresh_seconds,
            resume_from=parent,
            refresh_on_resume=parent is not None,
            max_quote_silence_ns=max_quote_silence_ns,
        )
        sessions.append(
            dict(
                path=path.name,
                status=result["status"],
                result_sha256=hashlib.sha256((path / "result.json").read_bytes()).hexdigest(),
            )
        )
        if result["status"] != "HALTED_QUOTE_SILENCE":
            status = result["status"]
            break
        parent = path
        if attempt == max_restarts:
            status = "HALTED_RESTART_LIMIT"
    output = dict(
        status=status,
        sessions=sessions,
        max_restarts=max_restarts,
        duration_budget_seconds=duration_seconds,
        positioning_refresh_seconds=positioning_refresh_seconds,
        claim_status="NO_ECONOMIC_CLAIM",
        scope="NATIVE_OBSERVATION_RECOVERY_NOT_PAPER_EXECUTION",
        limitation="Native connection and shutdown can extend wall time beyond the observation budget",
    )
    (destination / "run.json").write_text(canonical(output) + "\n")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--warmup-manifest", type=Path, action="append", required=True)
    parser.add_argument("--duration-seconds", type=int, required=True)
    parser.add_argument("--max-restarts", type=int, required=True)
    parser.add_argument("--max-quote-silence-ns", type=int, required=True)
    parser.add_argument("--grammar", default="range-breakout-48-v1")
    parser.add_argument("--positioning-policy", type=Path)
    parser.add_argument("--positioning-refresh-seconds", type=int)
    args = parser.parse_args()
    print(
        canonical(
            asyncio.run(
                run_observation(
                    args.destination,
                    manifests=tuple(args.warmup_manifest),
                    duration_seconds=args.duration_seconds,
                    max_restarts=args.max_restarts,
                    max_quote_silence_ns=args.max_quote_silence_ns,
                    grammar=args.grammar,
                    positioning_refresh_seconds=args.positioning_refresh_seconds,
                    positioning_policy=PositioningPolicy.model_validate_json(
                        args.positioning_policy.read_text()
                    )
                    if args.positioning_policy
                    else None,
                )
            )
        )
    )


if __name__ == "__main__":
    main()

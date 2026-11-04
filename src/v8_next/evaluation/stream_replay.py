"""Recompute captured native-stream observations in recorded callback order."""

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from v8_next.app.observe import source_hash
from v8_next.domain.config import PositioningPolicy
from v8_next.domain.market import Candle
from v8_next.economics.stream_observation import StreamObservations
from v8_next.evaluation.store import canonical


def replay_stream(run: Path) -> dict[str, Any]:
    return _replay_stream(run, frozenset())[0]


def restore_stream(run: Path) -> StreamObservations:
    _, observer = _replay_stream(run, frozenset())
    if observer is None:
        raise ValueError("stream has no economic observation state")
    return observer


def _replay_stream(
    run: Path, ancestors: frozenset[Path]
) -> tuple[dict[str, Any], StreamObservations | None]:
    run = run.resolve()
    if run in ancestors:
        raise ValueError("cyclic stream lineage")
    ancestors = ancestors | {run}
    if (run / "failure.json").exists():
        raise ValueError("failed stream cannot be accepted as a complete replay")
    result = json.loads((run / "result.json").read_text())
    for filename, key in (
        ("session.json", "session_sha256"),
        ("quotes.jsonl", "quote_sha256"),
        ("bars.jsonl", "bar_sha256"),
        ("observations.jsonl", "observation_sha256"),
    ):
        with (run / filename).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != result[key]:
                raise ValueError("stream artifact hash mismatch")
    if "positioning_sha256" in result:
        if (
            hashlib.sha256((run / "positioning.jsonl").read_bytes()).hexdigest()
            != result["positioning_sha256"]
        ):
            raise ValueError("stream positioning artifact hash mismatch")
    session = json.loads((run / "session.json").read_text())
    if session["code_and_lock_hash"] != source_hash():
        raise ValueError("stream replay requires frozen runtime")
    paths = tuple(Path(p) for p in session["warmup_manifests"])
    if (
        sorted(hashlib.sha256(p.read_bytes()).hexdigest() for p in paths)
        != session["warmup_manifest_hashes"]
    ):
        raise ValueError("stream warmup manifest changed")
    policy = PositioningPolicy.model_validate(session.get("positioning_policy", {}))
    observer = StreamObservations(paths, session["grammar"], policy) if paths else None
    if session.get("resume_from") is not None:
        parent = Path(session["resume_from"])
        raw = (parent / "result.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() != session["parent_result_sha256"]:
            raise ValueError("stream parent changed")
        if json.loads(raw)["ended_ns"] > session["started_ns"]:
            raise ValueError("stream parent reaches future")
        _, observer = _replay_stream(parent, ancestors)
        if (
            observer is None
            or observer.grammar != session["grammar"]
            or observer.positioning_policy != policy
            or json.loads((parent / "session.json").read_text()).get("positioning_refresh_seconds")
            != session.get("positioning_refresh_seconds")
            or json.loads((parent / "session.json").read_text())["warmup_manifest_hashes"]
            != session["warmup_manifest_hashes"]
        ):
            raise ValueError("resumed stream policy mismatch")
    backfills = tuple(Path(p) for p in session.get("backfill_manifests", []))
    if sorted(hashlib.sha256(p.read_bytes()).hexdigest() for p in backfills) != session.get(
        "backfill_manifest_hashes", []
    ):
        raise ValueError("stream backfill manifest changed")
    if backfills:
        if observer is None or session.get("resume_from") is None:
            raise ValueError("backfill without resumed observation state")
        observer.backfill(backfills, session["started_ns"])
    events: list[tuple[int, str, dict[str, Any]]] = []
    for kind, filename, count in (
        ("quote", "quotes.jsonl", result["quote_count"]),
        ("bar", "bars.jsonl", result["bar_count"]),
    ):
        rows = [json.loads(line) for line in (run / filename).read_text().splitlines()]
        if len(rows) != count:
            raise ValueError("stream event count mismatch")
        events.extend((row["sequence"], kind, row) for row in rows)
    if "positioning_sha256" in result:
        rows = [json.loads(line) for line in (run / "positioning.jsonl").read_text().splitlines()]
        if len(rows) != result["positioning_count"]:
            raise ValueError("stream positioning count mismatch")
        events.extend((row["sequence"], "positioning", row) for row in rows)
    events.sort(key=lambda event: event[0])
    recomputed = []
    last_quotes: dict[str, int] = {}
    for expected, (sequence, kind, row) in enumerate(events):
        if type(sequence) is not int or sequence != expected:
            raise ValueError("stream callback order missing or duplicated")
        if kind == "positioning":
            if (
                observer is None
                or not session["started_ns"] <= row["applied_ns"] <= result["ended_ns"]
            ):
                raise ValueError("unqualified positioning application")
            paths = tuple(Path(p) for p in row["manifests"])
            if [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths] != row[
                "manifest_hashes"
            ]:
                raise ValueError("stream positioning manifest changed")
            observer.refresh_positioning(
                paths, row["applied_ns"], expected_hashes=tuple(row["manifest_hashes"])
            )
            continue
        if row["instrument_id"] not in session["instruments"]:
            raise ValueError("unexpected stream instrument")
        if kind == "bar":
            if observer is None:
                raise ValueError("bar without configured observation path")
            observer.add_closed_candle(
                Candle(
                    row["instrument_id"],
                    row["start_ns"],
                    row["end_ns"],
                    Decimal(row["open"]),
                    Decimal(row["high"]),
                    Decimal(row["low"]),
                    Decimal(row["close"]),
                    Decimal(row["volume"]),
                    row["received_ns"],
                    row["received_ns"],
                    hashlib.sha256(canonical(row).encode()).hexdigest(),
                )
            )
        else:
            last_quotes[row["instrument_id"]] = row["received_ns"]
            if not 0 < row["event_ns"] <= row["received_ns"] <= row["recorded_ns"]:
                raise ValueError("invalid stream quote clocks")
            if observer is not None:
                observation = observer.observe(row["instrument_id"], row["received_ns"])
                if observation is not None:
                    observation["trigger_sequence"] = sequence
                    recomputed.append(observation)
    halt = result.get("health_halt")
    if (result.get("status") == "HALTED_QUOTE_SILENCE") != (halt is not None):
        raise ValueError("inconsistent stream halt status")
    if halt is not None:
        threshold = halt["threshold_ns"]
        checked, started = halt["checked_ns"], halt["started_ns"]
        if (
            type(threshold) is not int
            or threshold <= 0
            or threshold != session.get("max_quote_silence_ns")
            or not session["started_ns"] <= started <= checked <= result["ended_ns"]
            or any(t > checked for t in last_quotes.values())
        ):
            raise ValueError("invalid stream health halt clocks")
        stale = [
            name
            for name in session["instruments"]
            if checked - last_quotes.get(name, started) >= threshold
        ]
        if not stale or stale != halt["instruments"]:
            raise ValueError("stream health halt does not match recorded quote silence")
    recorded = [json.loads(line) for line in (run / "observations.jsonl").read_text().splitlines()]
    if canonical(recomputed) != canonical(recorded):
        raise ValueError("stream observation replay diverged")
    return dict(
        status="HALTED_PREFIX_REPRODUCED" if halt else "OBSERVATIONS_REPRODUCED",
        event_count=len(events),
        observation_count=len(recomputed),
        claim_status="NO_ECONOMIC_CLAIM",
        limitation="Recorded inputs only; no guarantee of omitted venue events or reconnect completeness",
    ), observer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    print(canonical(replay_stream(args.run)))


if __name__ == "__main__":
    main()

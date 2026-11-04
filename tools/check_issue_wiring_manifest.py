#!/usr/bin/env python3
"""#476 R4 — an issue cannot bypass the wiring manifest.

The issue this belongs to records the root cause of the unwired-output backlog:

    R-requirements written as module-existence, not wired slices; verification gates are
    ruff + mypy + module pytest only. ``WORK_ITEM_POLICY.md`` §9 admits form validation
    does not enforce API submissions -- the mandatory ``wiring_manifest`` field was
    bypassed on all 7 issues.

R4 asks for an enforcement mechanism, "process fix; enforcement mechanism TBD in this
issue". This is that mechanism: an issue body is a *contract*, and a contract with no
producer/transport/consumer/final-surface chain cannot be closed honestly.

Usage (CI runs this on every issue event)::

    python tools/check_issue_wiring_manifest.py --body-file issue.md
    gh issue view 476 --json body -q .body | python tools/check_issue_wiring_manifest.py

Exit 0 when the manifest is present and complete, 1 when it is not (with the named
reason), 2 on bad usage. It validates *form only* -- it cannot tell whether a claimed
consumer edge is true. That honesty boundary is printed in the report.

"""
from __future__ import annotations

import argparse
import json
import re
import sys

#: Every work item must declare the seven wiring steps. W4 is the one the backlog
#: skipped: a slice that never names its final surface cannot be verified end-to-end.
W_STEPS = ("W1", "W2", "W3", "W4", "W5", "W6", "W7")

MANIFEST_HEADING = re.compile(r"^#{1,4}\s*W1\s*[-–]\s*W7\s+wiring\s+manifest\s*$", re.IGNORECASE | re.MULTILINE)
R_IDS_HEADING = re.compile(r"^#{1,4}\s*R\s+IDs\s*$", re.IGNORECASE | re.MULTILINE)
REMAINING = re.compile(r"^#{1,4}\s*Remaining\s+R\s+IDs\s*$", re.IGNORECASE | re.MULTILINE)
COMPLETED = re.compile(r"^#{1,4}\s*Completed\s+R\s+IDs\s*$", re.IGNORECASE | re.MULTILINE)


def check(body: str) -> dict[str, object]:
    """Validate the wiring manifest of one issue body."""
    problems: list[str] = []
    match = MANIFEST_HEADING.search(body)
    present_steps: list[str] = []
    if match is None:
        problems.append("WIRING_MANIFEST_ABSENT: no 'W1-W7 Wiring manifest' section")
    else:
        tail = body[match.end():]
        # the manifest ends at the next heading of the same-or-higher level
        stop = re.search(r"^#{1,2}\s+\S", tail, re.MULTILINE)
        section = tail[: stop.start()] if stop else tail
        present_steps = [step for step in W_STEPS if re.search(rf"\b{step}\b", section)]
        missing = [step for step in W_STEPS if step not in present_steps]
        if missing:
            problems.append("WIRING_MANIFEST_INCOMPLETE: missing " + ", ".join(missing))
    for label, pattern, code in (
        ("Completed R IDs", COMPLETED, "COMPLETED_R_IDS_ABSENT"),
        ("Remaining R IDs", REMAINING, "REMAINING_R_IDS_ABSENT"),
    ):
        if pattern.search(body) is None:
            problems.append(f"{code}: no '{label}' section")
    return {
        "ok": not problems,
        "problems": problems,
        "w_steps_present": present_steps,
        "form_only": (
            "This validates FORM only: it cannot confirm that a declared consumer edge "
            "exists. Wiring truth is measured by tools/audit_wiring.py, not by prose."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--body-file", help="file holding the issue body (default: stdin)")
    parser.add_argument("--json", action="store_true", help="emit the verdict as JSON")
    args = parser.parse_args(argv)
    if args.body_file:
        try:
            body = open(args.body_file, encoding="utf-8").read()
        except OSError as exc:
            print(f"cannot read {args.body_file}: {exc}", file=sys.stderr)
            return 2
    else:
        body = sys.stdin.read()
    verdict = check(body)
    if args.json:
        print(json.dumps(verdict, indent=2))
    elif verdict["ok"]:
        print("issue wiring manifest: OK (" + ", ".join(verdict["w_steps_present"]) + ")")
        print(verdict["form_only"])
    else:
        print("issue wiring manifest: FAIL", file=sys.stderr)
        for problem in verdict["problems"]:
            print(f"  - {problem}", file=sys.stderr)
        print(verdict["form_only"], file=sys.stderr)
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

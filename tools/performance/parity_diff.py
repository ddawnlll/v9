"""Compare two parity manifests and report the first divergence.

Fails closed: unknown schema, missing section or a section present on only one
side is an error, not a pass. Chunk digests localise a divergence to a bar
range; with ``--left-details``/``--right-details`` the exact differing record
and JSON path are reported.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from parity_canon import first_difference  # noqa: E402


def _load(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != 1:
        raise SystemExit(f"{path}: unsupported manifest schema {data.get('schema')!r}")
    return data


def _details(directory: Path | None, section: str) -> list | None:
    if directory is None:
        return None
    target = directory / f"{section}.jsonl"
    if not target.is_file():
        return None
    return [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--left", type=Path, required=True, help="baseline manifest")
    parser.add_argument("--right", type=Path, required=True, help="candidate manifest")
    parser.add_argument("--left-details", type=Path)
    parser.add_argument("--right-details", type=Path)
    parser.add_argument("--json", action="store_true", help="machine-readable verdict")
    args = parser.parse_args()

    left, right = _load(args.left), _load(args.right)
    problems: list[str] = []
    if left["workload"] != right["workload"]:
        problems.append(f"workload {left['workload']!r} != {right['workload']!r}")
    if left.get("mode") != right.get("mode"):
        problems.append(f"mode {left.get('mode')!r} != {right.get('mode')!r}")
    if left["meta"] != right["meta"]:
        where = first_difference(left["meta"], right["meta"], "meta")
        problems.append(f"workload inputs differ: {where}")

    if left.get("volatile_keys") != right.get("volatile_keys"):
        where = first_difference(left.get("volatile_keys"), right.get("volatile_keys"), "volatile_keys")
        problems.append(f"ignored-key counts differ: {where}")

    left_sections, right_sections = left["sections"], right["sections"]
    missing = sorted(set(left_sections) ^ set(right_sections))
    if missing:
        problems.append(f"section set differs: {', '.join(missing)}")

    divergent: list[dict] = []
    for name in sorted(set(left_sections) & set(right_sections)):
        left_section, right_section = left_sections[name], right_sections[name]
        if left_section["sha256"] == right_section["sha256"]:
            continue
        detail: dict[str, object] = {"section": name, "count": [left_section["count"], right_section["count"]]}
        if left_section.get("chunked"):
            left_chunks = left["chunks"].get(name, [])
            right_chunks = right["chunks"].get(name, [])
            first = next(
                (index for index, (a, b) in enumerate(zip(left_chunks, right_chunks, strict=False)) if a != b),
                min(len(left_chunks), len(right_chunks)),
            )
            size = left["chunk_size"]
            detail["first_chunk"] = first
            detail["bar_range"] = [first * size, min((first + 1) * size, right_section["count"] or 0)]
            left_records = _details(args.left_details, name)
            right_records = _details(args.right_details, name)
            if left_records is not None and right_records is not None:
                for index in range(max(len(left_records), len(right_records))):
                    a = left_records[index] if index < len(left_records) else None
                    b = right_records[index] if index < len(right_records) else None
                    difference = first_difference(a, b)
                    if difference is not None:
                        detail["first_record"] = index
                        detail["difference"] = difference
                        break
        divergent.append(detail)

    verdict = {
        "parity": not problems and not divergent,
        "workload": left["workload"],
        "problems": problems,
        "divergent_sections": divergent,
        "sections_compared": len(set(left_sections) & set(right_sections)),
    }
    if args.json:
        print(json.dumps(verdict, indent=2, sort_keys=True))
    else:
        print(f"workload {left['workload']}: {'PARITY' if verdict['parity'] else 'DIVERGENCE'}")
        print(f"  sections compared: {verdict['sections_compared']}")
        for problem in problems:
            print(f"  PROBLEM {problem}")
        for detail in divergent:
            summary = ", ".join(f"{key}={value}" for key, value in detail.items() if key != "section")
            print(f"  DIVERGES {detail['section']}: {summary}")
    raise SystemExit(0 if verdict["parity"] else 1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python
"""Fail when a capability module has no consumer outside its own package.

Issue #476: modules can be fully unit-tested and still never reach the benchmark
path.  This audit resolves every ``v8_next.<package>.<module>`` import in
``src/v8_next`` and reports a module under one of the capability packages when
the only importers live inside that same package.

Test files are not consumers: the inspection root is ``src/v8_next`` and tests
live outside it.  There is no allowlist and no suppression file; an orphan is
either wired to a real consumer or it stays on this list.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any

CAPABILITY_PACKAGES = (
    "oracle",
    "evaluation",
    "analysis",
    "telemetry",
    "world",
    "system_proving",
    "opportunities",
)


def _module_name(path: Path, root: Path) -> str:
    return ".".join(path.relative_to(root).with_suffix("").parts)


def _imported_modules(tree: ast.AST) -> set[str]:
    """Every ``v8_next`` module named by an import in ``tree``."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module is None or not node.module.startswith("v8_next"):
                continue
            found.add(node.module)
            # ``from v8_next.evaluation import economic_benchmark`` imports a
            # submodule even though only the package name appears in ``module``.
            for alias in node.names:
                if alias.name != "*":
                    found.add(f"{node.module}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("v8_next"):
                    found.add(alias.name)
    return found


def audit(root: Path) -> dict[str, Any]:
    """Return the static wiring report for the package tree rooted at ``root``."""
    source_root = root / "src" / "v8_next"
    modules = {
        _module_name(path, source_root): path
        for package in CAPABILITY_PACKAGES
        for path in (source_root / package).glob("*.py")
        if path.name != "__init__.py"
    }
    importers: dict[str, set[str]] = {name: set() for name in modules}
    importer_files: dict[str, list[str]] = {name: [] for name in modules}

    for path in source_root.rglob("*.py"):
        importer = _module_name(path, source_root)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            raise SystemExit(f"audit_wiring: cannot parse {path}: {exc}") from exc
        for imported in _imported_modules(tree):
            parts = imported.split(".")
            if parts and parts[0] == "v8_next":
                parts = parts[1:]
            for module in modules:
                module_parts = module.split(".")
                if parts[: len(module_parts)] == module_parts:
                    importers[module].add(importer)
                    importer_files[module].append(str(path.relative_to(root)))

    orphaned: list[dict[str, Any]] = []
    for module in sorted(modules):
        package = module.rsplit(".", 1)[0]
        outside = {
            importer
            for importer in importers[module]
            if not importer.startswith(f"{package}.")
        }
        if not outside:
            orphaned.append(
                {
                    "module": module,
                    "package": package,
                    "importers_inside_package": sorted(importers[module]),
                    "source": str(modules[module].relative_to(root)),
                }
            )

    return {
        "audit": "capability_module_wiring",
        "root": str(root),
        "packages": list(CAPABILITY_PACKAGES),
        "module_count": len(modules),
        "orphan_count": len(orphaned),
        "orphans": orphaned,
        "status": "PASS" if not orphaned else "FAIL",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="v8-next project root (default: this tools/ directory's parent)",
    )
    parser.add_argument("--output", type=Path, default=None, help="optional JSON artifact path")
    args = parser.parse_args(argv)
    report = audit(args.root.resolve())

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    print(
        f"audit_wiring: {report['status']} "
        f"({report['orphan_count']} of {report['module_count']} capability modules orphaned)"
    )
    for orphan in report["orphans"]:
        inside = ", ".join(orphan["importers_inside_package"]) or "none"
        print(f"  ORPHAN {orphan['module']} (inside-package importers: {inside})")
    if report["orphans"]:
        print("fail: give each module a consumer outside its own package or keep it on this list")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

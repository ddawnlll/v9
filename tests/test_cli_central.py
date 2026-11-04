"""Central CLI coverage: one front door for all app modules and nx tools."""

import importlib
from pathlib import Path

from v8_next.app import cli as cli_mod


def _subcommands() -> set[str]:
    parser = cli_mod.build_parser()
    for action in parser._actions:
        if action.__class__.__name__ == "_SubParsersAction":
            assert action.choices is not None
            return set(action.choices)
    raise AssertionError("no subparsers in central CLI")


def test_cli_exposes_every_runnable_app_module() -> None:
    cmds = _subcommands()
    for name, module in {**cli_mod._SYSV_APP_MODULES, **cli_mod._ARGV_APP_MODULES}.items():
        assert name in cmds, f"cli missing command {name}"
        assert hasattr(importlib.import_module(module), "main"), module


def test_cli_exposes_every_tool_script() -> None:
    cmds = _subcommands()
    repo_root = Path(cli_mod.__file__).resolve().parents[3]
    for name, filename in cli_mod.TOOL_FILES.items():
        assert name in cmds, f"cli missing tool command {name}"
        assert (repo_root / "tools" / filename).is_file(), filename


def test_tool_scripts_all_expose_main_argv() -> None:
    import importlib.util

    repo_root = Path(cli_mod.__file__).resolve().parents[3]
    for filename in cli_mod.TOOL_FILES.values():
        tool = repo_root / "tools" / filename
        spec = importlib.util.spec_from_file_location(tool.stem, tool)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert callable(getattr(module, "main", None)), filename

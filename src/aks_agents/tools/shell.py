"""Running CLI commands (az, kubectl) for tools: argument lists only, never a shell, with a timeout."""

from __future__ import annotations

import subprocess
from typing import Any

from .base import ToolContext, ToolError


def execute(ctx: ToolContext, argv: list[str], timeout: int = 180) -> str:
    """Run a command; tests replace it through ``ctx.state["exec"]``."""
    runner = ctx.state.get("exec")
    if runner is not None:
        return runner(argv)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as err:
        raise ToolError(f"{argv[0]} is not installed in the agent image") from err
    except subprocess.TimeoutExpired as err:
        raise ToolError(f"{argv[0]} timed out after {timeout}s; narrow the query") from err
    if proc.returncode != 0:
        raise ToolError(f"{' '.join(argv[:4])} ... failed ({proc.returncode}): {proc.stderr.strip()[:2000]}")
    return proc.stdout


def flag_value(args: list[str], *names: str) -> Any:
    for i, arg in enumerate(args):
        for name in names:
            if arg == name and i + 1 < len(args):
                return args[i + 1]
            if arg.startswith(name + "="):
                return arg.split("=", 1)[1]
    return None

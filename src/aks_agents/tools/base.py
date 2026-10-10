"""Tool registry. A tool is only offered to an agent whose definition grants it, and its input is validated
against the schema before it runs – the model never reaches anything the definition does not allow."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import jsonschema

if TYPE_CHECKING:
    from ..audit import RunRecorder
    from ..definition import AgentDefinition, ToolGrant
    from ..settings import PlatformConfig, Settings

MAX_RESULT_CHARS = 60_000


class ToolError(Exception):
    """An error the model should see and can react to (bad input, denied operation, command failed)."""


@dataclass
class ToolContext:
    agent: AgentDefinition
    settings: Settings
    platform: PlatformConfig
    recorder: RunRecorder
    run_id: str
    outputs: list[dict[str, Any]] = field(default_factory=list)  # pull requests, issues, dispatches of this run
    state: dict[str, Any] = field(default_factory=dict)  # per-run state of tools (workspaces, ...)

    def options(self, tool: str) -> dict[str, Any]:
        grant = self.agent.grant(tool)
        return grant.options if grant else {}


Handler = Callable[[ToolContext, dict[str, Any]], Any]


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler
    writes: bool = False  # changes something outside the run (Git push, pull request, issue, dispatch)

    def definition(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}

    def run(self, ctx: ToolContext, tool_input: dict[str, Any]) -> str:
        try:
            jsonschema.validate(tool_input, self.input_schema)
        except jsonschema.ValidationError as err:
            raise ToolError(f"invalid input: {err.message}") from err
        result = self.handler(ctx, tool_input)
        text = result if isinstance(result, str) else json.dumps(result, indent=1, default=str, ensure_ascii=False)
        if len(text) > MAX_RESULT_CHARS:
            text = (
                text[:MAX_RESULT_CHARS]
                + f"\n... [truncated {len(text) - MAX_RESULT_CHARS} characters; narrow the query]"
            )
        return text


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None, writes=False):
    """Register a handler as a tool with a strict JSON schema."""

    def register(handler: Handler) -> Handler:
        if name in REGISTRY:
            raise ValueError(f"tool {name} registered twice")
        REGISTRY[name] = Tool(
            name=name,
            description=description,
            input_schema={
                "type": "object",
                "properties": properties,
                "required": required if required is not None else list(properties),
                "additionalProperties": False,
            },
            handler=handler,
            writes=writes,
        )
        return handler

    return register


def tools_for(grants: list[ToolGrant]) -> list[Tool]:
    from . import load_all

    load_all()

    missing = [g.name for g in grants if g.name not in REGISTRY]
    if missing:
        raise KeyError(f"unknown tools {missing}")
    return [REGISTRY[g.name] for g in grants]

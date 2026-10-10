"""Agent definitions: one YAML file per agent with its instructions, tools and limits.

In production the definitions are kept in the guardrails repository, versioned like code, and an agent can read but
never change its own definition.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Granting the first name grants the whole group, with the same options.
TOOL_GROUPS = {
    "repo_read": ["repo_list", "repo_read", "repo_search"],
    "repo_write": ["repo_write", "repo_replace", "repo_delete", "repo_diff"],
    "design": ["design_list", "design_read", "design_search"],
}


@dataclass
class ToolGrant:
    name: str
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentDefinition:
    name: str
    description: str
    instructions: str
    tools: list[ToolGrant]
    triggers: list[str] = field(default_factory=list)
    effort: str = "high"
    max_turns: int = 60
    max_tokens: int = 16000
    model: str | None = None  # overrides platform.yaml model.name
    source: Path | None = None

    @property
    def instructions_hash(self) -> str:
        return hashlib.sha256(self.instructions.encode()).hexdigest()[:16]

    def grant(self, tool: str) -> ToolGrant | None:
        return next((g for g in self.tools if g.name == tool), None)

    @classmethod
    def load(cls, path: str | Path) -> AgentDefinition:
        path = Path(path)
        data = yaml.safe_load(path.read_text())
        instructions = data.get("instructions", "")
        if instructions.endswith(".md"):
            instructions = (path.parent / instructions).read_text()
        tools: list[ToolGrant] = []
        for item in data.get("tools", []):
            if isinstance(item, str):
                name, options = item, {}
            else:
                ((name, options),) = item.items()
            for member in TOOL_GROUPS.get(name, [name]):
                if all(g.name != member for g in tools):
                    tools.append(ToolGrant(member, options or {}))
        model = data.get("model") or {}
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            instructions=instructions,
            tools=tools,
            triggers=data.get("triggers", []),
            effort=model.get("effort", "high"),
            max_turns=int(model.get("maxTurns", 60)),
            max_tokens=int(model.get("maxTokens", 16000)),
            model=model.get("name"),
            source=path,
        )


def load_all(directory: str | Path) -> dict[str, AgentDefinition]:
    definitions = {}
    for path in sorted(Path(directory).glob("*.yaml")):
        definition = AgentDefinition.load(path)
        definitions[definition.name] = definition
    return definitions

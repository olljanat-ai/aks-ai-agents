"""Loading of the guardrails repository files the gate is driven by: ``limits.yaml`` and ``tiers.yaml``."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .model import Tier


def per_agent(mapping: dict[str, Any] | None, agent: str, default: Any = None) -> Any:
    """Value for ``agent`` in a ``{agent-or-"*": value}`` mapping."""
    mapping = mapping or {}
    return mapping.get(agent, mapping.get("*", default))


def path_matches(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if fnmatch.fnmatchcase(path, pattern):
            return True
        # "**/x" also matches "x" at the repository root
        if pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:]):
            return True
    return False


@dataclass
class Rule:
    id: str
    tier: Tier
    types: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    namespaces: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Rule:
        return cls(
            id=data["id"],
            tier=Tier.parse(data["tier"]),
            types=[t.lower() for t in data.get("types", [])],
            kinds=data.get("kinds", []),
            actions=data.get("actions", []),
            namespaces=data.get("namespaces", []),
            paths=data.get("paths", []),
        )


@dataclass
class TierRules:
    default_resource: Tier
    default_manifest: Tier
    default_file: Tier
    resources: list[Rule]
    manifests: list[Rule]
    files: list[Rule]
    not_allowed_repositories: list[str]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TierRules:
        defaults = data.get("defaults", {})
        return cls(
            default_resource=Tier.parse(defaults.get("resource", "medium")),
            default_manifest=Tier.parse(defaults.get("manifest", "medium")),
            default_file=Tier.parse(defaults.get("file", "medium")),
            resources=[Rule.from_dict(r) for r in data.get("resources", [])],
            manifests=[Rule.from_dict(r) for r in data.get("manifests", [])],
            files=[Rule.from_dict(r) for r in data.get("files", [])],
            not_allowed_repositories=data.get("repositories", {}).get("notAllowedForAgents", []),
        )


@dataclass
class Limits:
    raw: dict[str, Any]

    def get(self, path: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def env(self, environment: str, path: str, default: Any = None) -> Any:
        """Environment-specific value (``environments.<env>.<path>``), falling back to the global ``<path>``."""
        value = self.get(f"environments.{environment}.{path}")
        return value if value is not None else self.get(path, default)

    @property
    def agents_enabled(self) -> bool:
        return bool(self.get("agents.enabled", True))


@dataclass
class GateConfig:
    limits: Limits
    tiers: TierRules

    @classmethod
    def load(cls, guardrails_dir: str | Path) -> GateConfig:
        root = Path(guardrails_dir)
        limits = yaml.safe_load((root / "limits.yaml").read_text()) or {}
        tiers = yaml.safe_load((root / "tiers.yaml").read_text()) or {}
        return cls(limits=Limits(limits), tiers=TierRules.from_dict(tiers))

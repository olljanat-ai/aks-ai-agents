"""Platform configuration (``platform.yaml``) and per-run settings (environment variables)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

GUARDED, PROTOTYPE = "guarded", "prototype"


@dataclass
class PlatformConfig:
    raw: dict[str, Any]

    @classmethod
    def load(cls, path: str | Path) -> PlatformConfig:
        return cls(yaml.safe_load(Path(path).read_text()) or {})

    def get(self, path: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def repository(self, key: str) -> str:
        """``owner/name`` of a repository key (``iac``, ``fleet``, ``design``, ...)."""
        repo = self.get(f"git.repositories.{key}") or self.get(f"github.repositories.{key}")
        if not repo:
            raise KeyError(f"unknown repository {key!r}")
        return repo

    def environment(self, env: str) -> dict[str, Any]:
        data = self.get(f"environments.{env}")
        if data is None:
            raise KeyError(f"unknown environment {env!r}")
        return data

    @property
    def environments(self) -> list[str]:
        return list((self.get("environments") or {}).keys())


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    """Settings of one agent run, from ``AKS_AGENT_*`` environment variables."""

    environment: str = "dev"  # the environment whose read identity this run uses
    mode: str = GUARDED  # guarded: read-only + pull requests; prototype: full permissions (build tenant only)
    root: Path = field(default_factory=lambda: Path.cwd())
    platform_file: Path | None = None
    definitions_dir: Path | None = None
    guardrails_dir: Path | None = None
    design_dir: Path | None = None
    workspace: Path | None = None
    runs_dir: Path | None = None
    dry_run: bool = False  # no pushes, pull requests, issues or dispatches – they are recorded instead

    @classmethod
    def from_env(cls, root: str | Path | None = None) -> Settings:
        base = Path(root or _env("AKS_AGENT_ROOT", str(Path.cwd())))

        def path(var: str, default: str) -> Path:
            value = _env(var, default)
            p = Path(value)
            return p if p.is_absolute() else base / p

        mode = _env("AKS_AGENT_MODE", GUARDED)
        if mode not in (GUARDED, PROTOTYPE):
            raise ValueError(f"AKS_AGENT_MODE must be {GUARDED} or {PROTOTYPE}, not {mode!r}")
        return cls(
            environment=_env("AKS_AGENT_ENV", "dev"),
            mode=mode,
            root=base,
            platform_file=path("AKS_AGENT_PLATFORM", "config/platform.yaml"),
            definitions_dir=path("AKS_AGENT_DEFINITIONS", "agents"),
            guardrails_dir=path("AKS_AGENT_GUARDRAILS", "guardrails"),
            design_dir=path("AKS_AGENT_DESIGN", "../aks-design"),
            workspace=path("AKS_AGENT_WORKSPACE", ".workspace"),
            runs_dir=path("AKS_AGENT_RUNS", "runs"),
            dry_run=_env("AKS_AGENT_DRY_RUN", "") in ("1", "true", "yes"),
        )

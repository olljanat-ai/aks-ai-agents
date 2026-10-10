"""Git platforms the agents work on: GitHub or Azure DevOps (``git.platform`` in ``platform.yaml``)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import GitPlatform, PlatformError

if TYPE_CHECKING:
    from ..settings import PlatformConfig

__all__ = ["GitPlatform", "PlatformError", "create"]


def create(config: PlatformConfig) -> GitPlatform:
    kind = config.get("git.platform", "github")
    if kind == "github":
        from .github import GitHubPlatform

        return GitHubPlatform(config)
    if kind == "azure-devops":
        from .azure_devops import AzureDevOpsPlatform

        return AzureDevOpsPlatform(config)
    raise ValueError(f"git.platform must be github or azure-devops, not {kind!r}")

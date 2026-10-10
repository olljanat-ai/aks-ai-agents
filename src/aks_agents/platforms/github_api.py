"""Minimal GitHub REST client for the agents (standard library only).

Each agent has its own GitHub App; the job that starts a run exchanges the App key for a short-lived installation
token and passes it in ``AKS_AGENT_GITHUB_TOKEN``. Commits are created through the Git data API, so GitHub signs them
for the App and they meet the signed-commit rule of the ``main`` ruleset.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .base import PlatformError


class GitHubError(PlatformError):
    pass


class GitHubClient:
    def __init__(self, token: str | None = None, api: str | None = None):
        self.token = token or os.environ.get("AKS_AGENT_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
        self.api = (api or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        params: dict[str, Any] | None = None,
        accept: str = "application/vnd.github+json",
    ) -> Any:
        url = f"{self.api}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": accept,
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                raw = response.read()
        except urllib.error.HTTPError as err:
            raise GitHubError(err.code, err.read().decode(errors="replace")[:500]) from err
        if accept.endswith("diff"):
            return raw.decode(errors="replace")
        return json.loads(raw) if raw else None

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params or None)

    def post(self, path: str, body: Any) -> Any:
        return self.request("POST", path, body)

    # -- higher level helpers used by the tools

    def commit_files(
        self, repo: str, base_sha: str, branch: str, message: str, changes: dict[str, bytes | None]
    ) -> str:
        """Create one commit on top of ``base_sha`` with ``changes`` (path → content, ``None`` deletes) and point
        ``branch`` at it. Returns the commit SHA."""
        base_commit = self.get(f"/repos/{repo}/git/commits/{base_sha}")
        tree = []
        for path, content in sorted(changes.items()):
            if content is None:
                tree.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
                continue
            blob = self.post(
                f"/repos/{repo}/git/blobs", {"content": base64.b64encode(content).decode(), "encoding": "base64"}
            )
            tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        new_tree = self.post(f"/repos/{repo}/git/trees", {"base_tree": base_commit["tree"]["sha"], "tree": tree})
        commit = self.post(
            f"/repos/{repo}/git/commits", {"message": message, "tree": new_tree["sha"], "parents": [base_sha]}
        )
        try:
            self.post(f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
        except GitHubError as err:
            if err.status != 422:  # branch exists: move it (agent branches belong to the agent)
                raise
            self.request("PATCH", f"/repos/{repo}/git/refs/heads/{branch}", {"sha": commit["sha"], "force": True})
        return commit["sha"]

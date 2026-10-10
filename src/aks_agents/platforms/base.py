"""What the agents' tools need from a Git platform. Each method maps to one REST operation or a few; the tools
enforce the agents' grants before calling them, the platforms only translate."""

from __future__ import annotations

import abc
from typing import Any

REVIEW_TITLES = {
    "pass": "Reviewer agent: pass",
    "flag": "Reviewer agent: flagged for human review",
    "fail": "Reviewer agent: changes required",
}


class PlatformError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status


class GitPlatform(abc.ABC):
    name: str
    max_body_length: int = 60_000  # pull request description limit

    def __init__(self, config: Any):
        self.config = config

    @property
    def default_branch(self) -> str:
        return self.config.get("git.defaultBranch", "main")

    # -- Git transport

    @abc.abstractmethod
    def clone_url(self, repo: str) -> str: ...

    @abc.abstractmethod
    def git_auth_header(self) -> str | None:
        """Value for ``git -c http.extraHeader=...``; never written to disk."""

    # -- changes

    @abc.abstractmethod
    def commit_files(
        self, repo: str, base_sha: str, branch: str, message: str, changes: dict[str, bytes | None], existing: set[str]
    ) -> str:
        """One commit on top of ``base_sha`` on a new ``branch``; ``None`` content deletes; ``existing`` are the
        paths present at ``base_sha``. Returns the commit ID."""

    @abc.abstractmethod
    def create_pull_request(self, repo: str, branch: str, title: str, body: str, draft: bool) -> dict[str, Any]:
        """Returns ``{"number", "url"}``."""

    # -- issues (GitHub issues / Azure Boards work items)

    @abc.abstractmethod
    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> dict[str, Any]: ...

    @abc.abstractmethod
    def search_issues(self, repo: str, text: str, state: str, labels: list[str]) -> list[dict[str, Any]]: ...

    @abc.abstractmethod
    def comment(self, repo: str, number: int, kind: str, body: str) -> dict[str, Any]:
        """``kind``: ``issue`` or ``pull_request``."""

    # -- pull requests

    @abc.abstractmethod
    def read_pull_request(self, repo: str, number: int) -> dict[str, Any]:
        """number, title, author, head_ref, head_sha, state, merged, body, files, commits, checks, reviews, diff."""

    @abc.abstractmethod
    def publish_review(
        self, repo: str, number: int, head_sha: str, verdict: str, text: str, findings: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """A review with line comments and the ``reviewer-agent`` check / status: success for pass and flag (flag in
        the title), failure for fail."""

    # -- runbooks

    @abc.abstractmethod
    def dispatch_runbook(self, repo: str, runbook: str, inputs: dict[str, str]) -> dict[str, Any]: ...

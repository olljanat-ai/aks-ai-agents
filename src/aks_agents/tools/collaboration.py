"""Collaboration tools: issues (GitHub issues / Azure Boards work items), comments, reading pull requests, the
reviewer agent's review, and runbook dispatch (GitHub Actions workflow / Azure Pipelines run).

Grants::

    - create_issue: {repositories: [iac, fleet], labels: [incident, task]}
    - search_issues: {repositories: [iac, fleet]}
    - comment: {repositories: [iac, fleet]}
    - pull_request_read: {repositories: [iac, fleet, design]}
    - publish_review: {repositories: [iac, fleet, design], ownLogin: "aks-reviewer-agent[bot]"}
    - dispatch_runbook: {runbooks: [halt-release, drain-cell, rollback-cell, suspend-kustomization]}
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable
from typing import Any

from ..platforms import PlatformError
from .base import ToolContext, ToolError, tool
from .repo import REPO_PROPERTY, git_platform


def _repo(ctx: ToolContext, tool_name: str, key: str) -> str:
    allowed = ctx.options(tool_name).get("repositories") or []
    if key not in allowed:
        raise ToolError(f"{ctx.agent.name} may not use {tool_name} on {key!r}; allowed: {allowed}")
    return ctx.platform.repository(key)


def _call(ctx: ToolContext, call: Callable[[], Any]) -> Any:
    try:
        return call()
    except PlatformError as err:
        raise ToolError(f"{git_platform(ctx).name}: {err}") from err


def _record(ctx: ToolContext, output: dict[str, Any], call: Callable[[], Any]) -> dict[str, Any]:
    """Run ``call`` unless this is a dry run; record the output either way."""
    if ctx.settings.dry_run:
        output["dry_run"] = True
    else:
        output.update(_call(ctx, call) or {})
    ctx.outputs.append(output)
    return output


def _footer(ctx: ToolContext) -> str:
    return f"\n\n---\n_`{ctx.agent.name}`, run `{ctx.run_id}`_\n"


@tool(
    "create_issue",
    "Open an issue (a work item on Azure DevOps): an incident with diagnosis and evidence, a task for a change "
    "agent, or a finding for an application team. Search for an existing open one first to avoid duplicates.",
    {
        "repo": REPO_PROPERTY,
        "title": {"type": "string", "maxLength": 200},
        "body": {"type": "string", "description": "Markdown"},
        "labels": {"type": "array", "items": {"type": "string"}},
    },
    required=["repo", "title", "body"],
    writes=True,
)
def create_issue(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    repo = _repo(ctx, "create_issue", args["repo"])
    allowed_labels = ctx.options("create_issue").get("labels")
    labels = args.get("labels", [])
    if allowed_labels is not None and any(label not in allowed_labels for label in labels):
        raise ToolError(f"labels must be from {allowed_labels}")
    body = args["body"] + _footer(ctx)
    return _record(
        ctx,
        {"type": "issue", "repo": repo, "title": args["title"]},
        lambda: git_platform(ctx).create_issue(repo, args["title"], body, labels),
    )


@tool(
    "search_issues",
    "Search issues (work items on Azure DevOps) of a repository by words in the title, state and labels.",
    {
        "repo": REPO_PROPERTY,
        "text": {"type": "string", "description": "Words in the title; empty for any"},
        "state": {"type": "string", "enum": ["open", "closed", "all"]},
        "labels": {"type": "array", "items": {"type": "string"}},
    },
    required=["repo", "text", "state"],
)
def search_issues(ctx: ToolContext, args: dict[str, Any]) -> list[dict[str, Any]]:
    repo = _repo(ctx, "search_issues", args["repo"])
    return _call(
        ctx, lambda: git_platform(ctx).search_issues(repo, args["text"], args["state"], args.get("labels", []))
    )


@tool(
    "comment",
    "Comment on an issue or a pull request, e.g. the verification of a merged change against its evidence.",
    {
        "repo": REPO_PROPERTY,
        "number": {"type": "integer"},
        "kind": {"type": "string", "enum": ["issue", "pull_request"]},
        "body": {"type": "string"},
    },
    writes=True,
)
def comment(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    repo = _repo(ctx, "comment", args["repo"])
    body = args["body"] + _footer(ctx)
    return _record(
        ctx,
        {"type": "comment", "repo": repo, "number": args["number"], "kind": args["kind"]},
        lambda: git_platform(ctx).comment(repo, args["number"], args["kind"], body),
    )


@tool(
    "pull_request_read",
    "Read a pull request: metadata, body, changed files, the diff, commits, checks / statuses (including the plan "
    "CI attached and the merge gate) and reviews.",
    {"repo": REPO_PROPERTY, "number": {"type": "integer"}},
)
def pull_request_read(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    repo = _repo(ctx, "pull_request_read", args["repo"])
    return _call(ctx, lambda: git_platform(ctx).read_pull_request(repo, args["number"]))


@tool(
    "publish_review",
    "Publish your review of a pull request. verdict: pass (it does what its intent says, the rollback is real, it "
    "does not widen access), flag (acceptable but a human must look: the gate raises the tier to at least medium), "
    "fail (wrong, unsafe or not matching its intent – blocks the merge). Findings may point at lines.",
    {
        "repo": REPO_PROPERTY,
        "number": {"type": "integer"},
        "verdict": {"type": "string", "enum": ["pass", "flag", "fail"]},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "line": {"type": "integer"},
                    "severity": {"type": "string", "enum": ["blocker", "major", "minor", "info"]},
                    "body": {"type": "string"},
                },
                "required": ["path", "line", "severity", "body"],
                "additionalProperties": False,
            },
        },
    },
    required=["repo", "number", "verdict", "summary"],
    writes=True,
)
def publish_review(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    repo = _repo(ctx, "publish_review", args["repo"])
    gp = git_platform(ctx)
    pr = _call(ctx, lambda: gp.read_pull_request(repo, args["number"]))
    own = ctx.options("publish_review").get("ownLogin") or []
    own = [own] if isinstance(own, str) else own
    author = str(pr["author"]).lower()
    if any(author == o.lower() or ("*" in o and fnmatch.fnmatchcase(author, o.lower())) for o in own):
        raise ToolError("the reviewer agent never reviews its own work")
    findings = args.get("findings", [])
    lines = [args["summary"].strip(), ""]
    for f in findings:
        lines.append(f"- **{f['severity']}** `{f['path']}:{f['line']}` – {f['body']}")
    text = "\n".join(lines) + _footer(ctx)
    return _record(
        ctx,
        {"type": "review", "repo": repo, "number": args["number"], "verdict": args["verdict"]},
        lambda: gp.publish_review(repo, args["number"], pr["head_sha"], args["verdict"], text, findings),
    )


RUNBOOK_INPUTS = {
    "halt-release": {"environment": True, "reason": True},
    "drain-cell": {"environment": True, "cell": True, "reason": True},
    "rollback-cell": {"environment": True, "cell": True, "reason": True},
    "suspend-kustomization": {"environment": True, "cell": True, "namespace": True, "name": True, "reason": True},
}


@tool(
    "dispatch_runbook",
    "Dispatch a pre-approved safe runbook (stopping, never starting): halt-release, drain-cell, rollback-cell, "
    "suspend-kustomization. The runbook enforces its own limits and pages the human on-call. Inputs: environment, "
    "cell (sl-az1...), namespace and name (suspend-kustomization), reason.",
    {
        "runbook": {"type": "string", "enum": sorted(RUNBOOK_INPUTS)},
        "inputs": {
            "type": "object",
            "properties": {k: {"type": "string"} for k in ("environment", "cell", "namespace", "name", "reason")},
            "additionalProperties": False,
        },
    },
    writes=True,
)
def dispatch_runbook(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    runbook, inputs = args["runbook"], args["inputs"]
    allowed = ctx.options("dispatch_runbook").get("runbooks") or []
    if runbook not in allowed:
        raise ToolError(f"{ctx.agent.name} may not dispatch {runbook}; allowed: {allowed}")
    missing = [k for k in RUNBOOK_INPUTS[runbook] if not inputs.get(k)]
    if missing:
        raise ToolError(f"{runbook} needs inputs {missing}")
    if runbook == "suspend-kustomization" and inputs["namespace"].startswith("infra-"):
        raise ToolError("infra-* Kustomizations are never suspended by an agent")
    repo = ctx.platform.repository("runbooks")
    payload = {**inputs, "requested_by": ctx.agent.name, "agent_run": ctx.run_id}
    return _record(
        ctx,
        {"type": "runbook", "repo": repo, "runbook": runbook, "inputs": inputs},
        lambda: git_platform(ctx).dispatch_runbook(repo, runbook, payload),
    )

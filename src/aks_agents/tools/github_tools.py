"""GitHub tools: issues, comments, reading pull requests, the reviewer agent's review, and runbook dispatch.

Grants::

    - create_issue: {repositories: [iac, fleet]}
    - comment: {repositories: [iac, fleet]}
    - pull_request_read: {repositories: [iac, fleet, design]}
    - publish_review: {repositories: [iac, fleet, design]}
    - dispatch_runbook: {runbooks: [halt-release, drain-cell, rollback-cell, suspend-kustomization]}
"""

from __future__ import annotations

from typing import Any

from .base import ToolContext, ToolError, tool
from .repo import REPO_PROPERTY, github

REVIEW_CHECK = "reviewer-agent"


def _repo(ctx: ToolContext, tool_name: str, key: str) -> str:
    allowed = ctx.options(tool_name).get("repositories") or []
    if key not in allowed:
        raise ToolError(f"{ctx.agent.name} may not use {tool_name} on {key!r}; allowed: {allowed}")
    return ctx.platform.repository(key)


def _record(ctx: ToolContext, output: dict[str, Any], call) -> dict[str, Any]:
    """Run ``call`` unless this is a dry run; record the output either way."""
    if ctx.settings.dry_run:
        output["dry_run"] = True
    else:
        output.update(call() or {})
    ctx.outputs.append(output)
    return output


def _footer(ctx: ToolContext) -> str:
    return f"\n\n---\n_`{ctx.agent.name}`, run `{ctx.run_id}`_\n"


@tool(
    "create_issue",
    "Open an issue: an incident with diagnosis and evidence, a task for a change agent, or a finding for an "
    "application team. Search for an existing open issue first to avoid duplicates.",
    {
        "repo": REPO_PROPERTY,
        "title": {"type": "string", "maxLength": 200},
        "body": {"type": "string"},
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

    def call():
        issue = github(ctx).post(f"/repos/{repo}/issues", {"title": args["title"], "body": body, "labels": labels})
        return {"number": issue["number"], "url": issue["html_url"]}

    return _record(ctx, {"type": "issue", "repo": repo, "title": args["title"]}, call)


@tool(
    "search_issues",
    "Search issues and pull requests of a repository (GitHub search syntax after the repo qualifier), e.g. "
    "'is:open label:incident aks-prd-sl-az1'.",
    {"repo": REPO_PROPERTY, "query": {"type": "string"}},
)
def search_issues(ctx: ToolContext, args: dict[str, Any]) -> list[dict[str, Any]]:
    allowed = set(ctx.options("search_issues").get("repositories") or [])
    if args["repo"] not in allowed:
        raise ToolError(f"may not search {args['repo']!r}; allowed: {sorted(allowed)}")
    repo = ctx.platform.repository(args["repo"])
    data = github(ctx).get("/search/issues", q=f"repo:{repo} {args['query']}", per_page=20)
    return [
        {
            "number": i["number"],
            "title": i["title"],
            "state": i["state"],
            "url": i["html_url"],
            "labels": [lbl["name"] for lbl in i.get("labels", [])],
            "updated": i["updated_at"],
        }
        for i in data.get("items", [])
    ]


@tool(
    "comment",
    "Comment on an issue or pull request, e.g. the verification of a merged change against its evidence.",
    {"repo": REPO_PROPERTY, "number": {"type": "integer"}, "body": {"type": "string"}},
    writes=True,
)
def comment(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    repo = _repo(ctx, "comment", args["repo"])

    def call():
        c = github(ctx).post(f"/repos/{repo}/issues/{args['number']}/comments", {"body": args["body"] + _footer(ctx)})
        return {"url": c["html_url"]}

    return _record(ctx, {"type": "comment", "repo": repo, "number": args["number"]}, call)


@tool(
    "pull_request_read",
    "Read a pull request: metadata, body, changed files, the diff, commits, check runs (including the plan CI "
    "attached) and reviews.",
    {"repo": REPO_PROPERTY, "number": {"type": "integer"}},
)
def pull_request_read(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    repo = _repo(ctx, "pull_request_read", args["repo"])
    gh = github(ctx)
    number = args["number"]
    pr = gh.get(f"/repos/{repo}/pulls/{number}")
    files = gh.get(f"/repos/{repo}/pulls/{number}/files", per_page=100)
    diff = gh.request("GET", f"/repos/{repo}/pulls/{number}", accept="application/vnd.github.diff")
    commits = gh.get(f"/repos/{repo}/pulls/{number}/commits", per_page=100)
    checks = gh.get(f"/repos/{repo}/commits/{pr['head']['sha']}/check-runs", per_page=100)
    reviews = gh.get(f"/repos/{repo}/pulls/{number}/reviews", per_page=100)
    return {
        "number": number,
        "title": pr["title"],
        "author": pr["user"]["login"],
        "head_ref": pr["head"]["ref"],
        "head_sha": pr["head"]["sha"],
        "state": pr["state"],
        "merged": pr.get("merged", False),
        "body": pr.get("body") or "",
        "files": [{"path": f["filename"], "status": f["status"], "changes": f["changes"]} for f in files],
        "commits": [{"sha": c["sha"][:12], "message": c["commit"]["message"]} for c in commits],
        "checks": [
            {
                "name": c["name"],
                "status": c["status"],
                "conclusion": c["conclusion"],
                "title": (c.get("output") or {}).get("title"),
                "summary": ((c.get("output") or {}).get("summary") or "")[:4000],
            }
            for c in checks.get("check_runs", [])
        ],
        "reviews": [{"user": r["user"]["login"], "state": r["state"]} for r in reviews],
        "diff": diff,
    }


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
    gh = github(ctx)
    pr = gh.get(f"/repos/{repo}/pulls/{args['number']}")
    author = pr["user"]["login"]
    own_bot = (ctx.options("publish_review").get("ownLogin") or "").lower()
    if own_bot and author.lower() == own_bot:
        raise ToolError("the reviewer agent never reviews its own work")
    head_sha = pr["head"]["sha"]
    findings = args.get("findings", [])
    titles = {
        "pass": "Reviewer agent: pass",
        "flag": "Reviewer agent: flagged for human review",
        "fail": "Reviewer agent: changes required",
    }
    conclusion = {"pass": "success", "flag": "success", "fail": "failure"}[args["verdict"]]
    lines = [args["summary"].strip(), ""]
    for f in findings:
        lines.append(f"- **{f['severity']}** `{f['path']}:{f['line']}` – {f['body']}")
    text = "\n".join(lines) + _footer(ctx)

    def call():
        review = gh.post(
            f"/repos/{repo}/pulls/{args['number']}/reviews",
            {
                "commit_id": head_sha,
                "event": "REQUEST_CHANGES" if args["verdict"] == "fail" else "COMMENT",
                "body": text,
                "comments": [
                    {"path": f["path"], "line": f["line"], "side": "RIGHT", "body": f"**{f['severity']}**: {f['body']}"}
                    for f in findings
                ],
            },
        )
        gh.post(
            f"/repos/{repo}/check-runs",
            {
                "name": REVIEW_CHECK,
                "head_sha": head_sha,
                "status": "completed",
                "conclusion": conclusion,
                "output": {"title": titles[args["verdict"]], "summary": text[:60000]},
            },
        )
        return {"review": review.get("html_url"), "head_sha": head_sha}

    return _record(ctx, {"type": "review", "repo": repo, "number": args["number"], "verdict": args["verdict"]}, call)


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
    payload = {"ref": "main", "inputs": {**inputs, "requested_by": ctx.agent.name, "agent_run": ctx.run_id}}

    def call():
        github(ctx).post(f"/repos/{repo}/actions/workflows/{runbook}.yml/dispatches", payload)
        return {"dispatched": True}

    return _record(ctx, {"type": "runbook", "repo": repo, "runbook": runbook, "inputs": inputs}, call)

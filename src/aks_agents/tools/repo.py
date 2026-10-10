"""Repository tools: read the IaC, fleet and design repositories, change files in a local workspace, and open the
change as a pull request from an ``agent/<name>/*`` branch.

Grants (in the agent definition)::

    - repo_read: {repositories: [iac, fleet, design]}
    - repo_write: {paths: {fleet: ["apps/*/stateless/**"], iac: ["environments/*/versions.auto.tfvars"]}}
    - create_pull_request: {repositories: [fleet, iac]}

An agent can only read repositories and write paths its definition lists. Nothing is pushed until
``create_pull_request``, which creates one signed commit through the GitHub API.
"""

from __future__ import annotations

import base64
import fnmatch
import re
import subprocess
from pathlib import Path
from typing import Any

from ..github import GitHubClient
from .base import ToolContext, ToolError, tool
from .core import safe_join

REPO_PROPERTY = {"type": "string", "description": "Repository key, e.g. iac, fleet or design"}


def github(ctx: ToolContext) -> GitHubClient:
    if "github" not in ctx.state:
        ctx.state["github"] = GitHubClient()
    return ctx.state["github"]


def _git(cwd: Path, *args: str, token: str = "", check: bool = True) -> str:
    cmd = ["git"]
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        cmd += ["-c", f"http.extraHeader=Authorization: Basic {basic}"]
    proc = subprocess.run(cmd + list(args), cwd=cwd, capture_output=True, text=True, timeout=300)
    if check and proc.returncode != 0:
        raise ToolError(f"git {args[0]} failed: {proc.stderr.strip()[:500]}")
    return proc.stdout


def _allowed_repos(ctx: ToolContext, tool_name: str) -> list[str]:
    options = ctx.options(tool_name)
    if tool_name == "repo_write":
        return list((options.get("paths") or {}).keys())
    return list(options.get("repositories") or [])


def _check_repo(ctx: ToolContext, tool_name: str, key: str) -> None:
    allowed = _allowed_repos(ctx, tool_name)
    if key not in allowed:
        raise ToolError(f"{ctx.agent.name} may not use {tool_name} on {key!r}; allowed: {allowed}")


def workspace(ctx: ToolContext, key: str) -> Path:
    """Local clone of a repository's default branch, made on first use in the run."""
    spaces: dict[str, Path] = ctx.state.setdefault("workspaces", {})
    if key in spaces:
        return spaces[key]
    repo = ctx.platform.repository(key)
    template = ctx.platform.get("github.cloneUrl", "https://github.com/{repo}.git")
    url = template.format(repo=repo, name=repo.split("/")[-1])
    branch = ctx.platform.get("github.defaultBranch", "main")
    target = Path(ctx.settings.workspace or ".workspace") / ctx.run_id / key
    target.parent.mkdir(parents=True, exist_ok=True)
    _git(
        target.parent,
        "clone",
        "--quiet",
        "--depth",
        "50",
        "--branch",
        branch,
        url,
        str(target),
        token=github(ctx).token if url.startswith("https://") else "",
    )
    spaces[key] = target
    return target


@tool(
    "repo_list",
    "List files in a repository (default branch plus your uncommitted changes). Optional glob, e.g. 'apps/*/base/*'.",
    {"repo": REPO_PROPERTY, "glob": {"type": "string", "description": "fnmatch pattern on the path; '*' for all"}},
)
def repo_list(ctx: ToolContext, args: dict[str, Any]) -> str:
    _check_repo(ctx, "repo_read", args["repo"])
    root = workspace(ctx, args["repo"])
    files = _git(root, "ls-files", "--cached", "--others", "--exclude-standard").splitlines()
    matched = [f for f in files if fnmatch.fnmatchcase(f, args["glob"])]
    return "\n".join(matched[:2000]) + (f"\n... {len(matched) - 2000} more" if len(matched) > 2000 else "")


@tool(
    "repo_read",
    "Read a file of a repository, with line numbers.",
    {
        "repo": REPO_PROPERTY,
        "path": {"type": "string"},
        "start_line": {"type": "integer", "minimum": 1},
        "end_line": {"type": "integer", "minimum": 1},
    },
    required=["repo", "path"],
)
def repo_read(ctx: ToolContext, args: dict[str, Any]) -> str:
    _check_repo(ctx, "repo_read", args["repo"])
    path = safe_join(workspace(ctx, args["repo"]), args["path"])
    if not path.is_file():
        raise ToolError(f"{args['path']} does not exist")
    lines = path.read_text(errors="replace").splitlines()
    start = args.get("start_line", 1)
    end = args.get("end_line", len(lines))
    return "\n".join(f"{n:5}\t{line}" for n, line in enumerate(lines[start - 1 : end], start))


@tool(
    "repo_search",
    "Search a repository with a regular expression (git grep, case-insensitive). Optional path glob.",
    {"repo": REPO_PROPERTY, "pattern": {"type": "string"}, "path_glob": {"type": "string"}},
    required=["repo", "pattern"],
)
def repo_search(ctx: ToolContext, args: dict[str, Any]) -> str:
    _check_repo(ctx, "repo_read", args["repo"])
    root = workspace(ctx, args["repo"])
    cmd = ["grep", "-n", "-i", "-E", "--untracked", "-e", args["pattern"]]
    if args.get("path_glob"):
        cmd += ["--", args["path_glob"]]
    out = _git(root, *cmd, check=False)
    lines = out.splitlines()
    return "\n".join(lines[:300]) + (f"\n... {len(lines) - 300} more" if len(lines) > 300 else "") or "no matches"


def _check_write_path(ctx: ToolContext, key: str, path: str) -> None:
    patterns = (ctx.options("repo_write").get("paths") or {}).get(key) or []
    if not any(fnmatch.fnmatchcase(path, p) for p in patterns):
        raise ToolError(f"{ctx.agent.name} may not write {key}:{path}; allowed paths: {patterns}")


@tool(
    "repo_write",
    "Create or overwrite a file in your local workspace of a repository. Only paths your role allows. Nothing is "
    "pushed until create_pull_request.",
    {"repo": REPO_PROPERTY, "path": {"type": "string"}, "content": {"type": "string"}},
)
def repo_write(ctx: ToolContext, args: dict[str, Any]) -> str:
    _check_repo(ctx, "repo_write", args["repo"])
    _check_write_path(ctx, args["repo"], args["path"])
    path = safe_join(workspace(ctx, args["repo"]), args["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(args["content"])
    return f"wrote {args['path']} ({len(args['content'])} characters)"


@tool(
    "repo_replace",
    "Replace one exact, unique text in a file of your workspace (safer than rewriting the whole file).",
    {"repo": REPO_PROPERTY, "path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
)
def repo_replace(ctx: ToolContext, args: dict[str, Any]) -> str:
    _check_repo(ctx, "repo_write", args["repo"])
    _check_write_path(ctx, args["repo"], args["path"])
    path = safe_join(workspace(ctx, args["repo"]), args["path"])
    if not path.is_file():
        raise ToolError(f"{args['path']} does not exist")
    text = path.read_text()
    count = text.count(args["old"])
    if count != 1:
        raise ToolError(f"the text to replace occurs {count} times; it must occur exactly once")
    path.write_text(text.replace(args["old"], args["new"]))
    return f"replaced in {args['path']}"


@tool(
    "repo_delete",
    "Delete a file in your local workspace of a repository. Only paths your role allows.",
    {"repo": REPO_PROPERTY, "path": {"type": "string"}},
)
def repo_delete(ctx: ToolContext, args: dict[str, Any]) -> str:
    _check_repo(ctx, "repo_write", args["repo"])
    _check_write_path(ctx, args["repo"], args["path"])
    path = safe_join(workspace(ctx, args["repo"]), args["path"])
    if not path.is_file():
        raise ToolError(f"{args['path']} does not exist")
    path.unlink()
    return f"deleted {args['path']}"


def _changes(root: Path) -> dict[str, bytes | None]:
    out = _git(root, "status", "--porcelain", "--untracked-files=all", "-z")
    changes: dict[str, bytes | None] = {}
    entries = out.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if not entry:
            continue
        status, path = entry[:2], entry[3:]
        if "R" in status:  # renamed: the next entry is the old path
            changes[entries[i]] = None
            i += 1
        file = root / path
        changes[path] = file.read_bytes() if file.exists() else None
    return changes


@tool(
    "repo_diff",
    "Show the diff of your uncommitted changes in a repository workspace.",
    {"repo": REPO_PROPERTY},
)
def repo_diff(ctx: ToolContext, args: dict[str, Any]) -> str:
    _check_repo(ctx, "repo_write", args["repo"])
    root = workspace(ctx, args["repo"])
    _git(root, "add", "--intent-to-add", "--all")
    return _git(root, "diff") or "no changes"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "change"


def pull_request_body(args: dict[str, Any], agent: str, run_id: str) -> str:
    return (
        f"## Intent\n\n{args['intent'].strip()}\n\n"
        f"## Evidence\n\n{args['evidence'].strip()}\n\n"
        f"## Design section\n\n{args['design_section'].strip()}\n\n"
        f"## Rollback\n\n{args['rollback'].strip()}\n\n"
        "## Plan\n\n_Attached by CI for the head commit – not written by the agent._\n\n"
        "## Risk tier\n\n_Computed by the merge gate from the plan._\n\n"
        f"---\nOpened by `{agent}` in run `{run_id}`.\n"
    )


@tool(
    "create_pull_request",
    "Commit all your workspace changes in one repository and open a pull request from an agent/<you>/* branch. "
    "Keep it to one concern and, in prd, one cell. The body is generated from the fields.",
    {
        "repo": REPO_PROPERTY,
        "title": {"type": "string", "maxLength": 100},
        "intent": {"type": "string", "description": "The issue, alert or schedule this answers (link it)"},
        "evidence": {"type": "string", "description": "Queries, metrics and links that show the change is needed"},
        "design_section": {
            "type": "string",
            "description": "e.g. docs/12-zero-downtime-cluster-upgrades.md#update-policy",
        },
        "rollback": {"type": "string", "description": "How to undo it, concretely"},
        "draft": {"type": "boolean"},
    },
    required=["repo", "title", "intent", "evidence", "design_section", "rollback"],
    writes=True,
)
def create_pull_request(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    key = args["repo"]
    _check_repo(ctx, "create_pull_request", key)
    root = workspace(ctx, key)
    changes = _changes(root)
    if not changes:
        raise ToolError("no changes in the workspace; write files first")
    for path in changes:
        _check_write_path(ctx, key, path)
    if not re.search(r"(docs/\d{2}-[\w-]+\.md|[Ss]ection \d+)", args["design_section"]):
        raise ToolError("design_section must cite a design document, e.g. docs/15-ai-driven-day-2-operations.md")
    repo = ctx.platform.repository(key)
    agent, run_id = ctx.agent.name, ctx.run_id
    branch = f"agent/{agent}/{_slug(args['title'])}-{run_id.rsplit('-', 1)[-1]}"
    message = f"{args['title']}\n\n{args['intent'].strip()[:1000]}\n\nAgent: {agent}\nAgent-Run: {run_id}\n"
    body = pull_request_body(args, agent, run_id)
    base_sha = _git(root, "rev-parse", "HEAD").strip()
    output: dict[str, Any] = {
        "type": "pull_request",
        "repo": repo,
        "branch": branch,
        "title": args["title"],
        "files": sorted(changes),
    }
    if ctx.settings.dry_run:
        output["dry_run"] = True
        output["body"] = body
    else:
        gh = github(ctx)
        gh.commit_files(repo, base_sha, branch, message, changes)
        base = ctx.platform.get("github.defaultBranch", "main")
        pr = gh.post(
            f"/repos/{repo}/pulls",
            {
                "title": args["title"],
                "head": branch,
                "base": base,
                "body": body,
                "draft": bool(args.get("draft", False)),
            },
        )
        output.update(number=pr["number"], url=pr["html_url"])
    ctx.outputs.append(output)
    # Start the next change from a clean default branch.
    _git(root, "reset", "--quiet", "--hard", "HEAD")
    _git(root, "clean", "-fdq")
    return output

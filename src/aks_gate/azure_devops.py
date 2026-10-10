"""Azure DevOps counterpart of :mod:`aks_gate.github`: collects pull request facts and publishes the decision.

Mapping of the GitHub concepts the gate relies on:

* **approvals on the head commit** – reviewer votes (10 approved, 5 approved with suggestions); the branch policy
  "reset votes when new changes are pushed" must be on, which makes every remaining vote one on the head commit;
* **team membership** – required group reviewers (``[project]\\platform-engineers``) list the users who voted for
  them in ``votedFor``;
* **reviewer agent check** – a pull request status ``aks-agents/reviewer-agent`` posted by the reviewer agent;
* **merge gate result** – a pull request status ``aks-agents/merge-gate`` that a branch status policy requires, with
  the gate's identity as the only authorised account;
* **auto-merge** – auto-complete set by the gate's identity.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import GateConfig
from .pr import branch_agent

API = "7.1"
STATUS_GENRE = "aks-agents"


class AzureDevOps:
    def __init__(self, collection_url: str, project: str, token: str | None = None):
        self.base = collection_url.rstrip("/")
        self.project = project
        self.token = token or os.environ.get("SYSTEM_ACCESSTOKEN") or os.environ.get("AZURE_DEVOPS_EXT_PAT", "")

    def request(
        self, method: str, path: str, body: Any = None, params: dict[str, Any] | None = None, project: bool = True
    ) -> Any:
        query = {"api-version": API, **(params or {})}
        prefix = f"{self.base}/{urllib.parse.quote(self.project)}" if project else self.base
        url = f"{prefix}/_apis/{path}?{urllib.parse.urlencode(query)}"
        if self.token.startswith("ey"):  # OAuth token (System.AccessToken)
            auth = f"Bearer {self.token}"
        else:  # personal access token
            auth = "Basic " + base64.b64encode(f":{self.token}".encode()).decode()
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method, headers={"Authorization": auth, "Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=60) as response:
            raw = response.read()
        return json.loads(raw) if raw else None

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params)

    def pr_path(self, repository: str, number: int) -> str:
        return f"git/repositories/{urllib.parse.quote(repository)}/pullRequests/{number}"


def _approved(vote: int) -> bool:
    return vote >= 5


def _agent_identities(config: GateConfig) -> dict[str, str]:
    """Display name / unique name of each agent's service principal → agent name."""
    return {v.lower(): k for k, v in (config.gate.get("agentsAzureDevOps") or {}).items()}


def _login(identity: dict[str, Any], agents: dict[str, str]) -> str:
    """The gate's user name for an identity; agents and service accounts end with ``[bot]`` like GitHub Apps."""
    names = [str(identity.get(k) or "") for k in ("uniqueName", "displayName")]
    for name in names:
        if name.lower() in agents:
            return f"{name}[bot]"
    name = names[0] or names[1]
    if "@" not in name or "build service" in names[1].lower():
        return f"{name}[bot]"
    return name


def collect(
    ado: AzureDevOps, repository: str, number: int, config: GateConfig, now: dt.datetime | None = None
) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.UTC)
    agents = _agent_identities(config)
    base = ado.pr_path(repository, number)
    pr = ado.get(base)
    head_sha = pr["lastMergeSourceCommit"]["commitId"]
    author = _login(pr["createdBy"], agents)
    head_ref = pr["sourceRefName"].removeprefix("refs/heads/")

    # Team keys of tiers.yaml (platform, security) → group names, matched on the part after the backslash.
    teams = {slug.lower(): key for key, slug in (config.gate.get("teams") or {}).items()}
    member_of: dict[str, set[str]] = {}
    reviewers = ado.get(f"{base}/reviewers").get("value", [])
    for reviewer in reviewers:
        group = str(reviewer.get("displayName", "")).rsplit("\\", 1)[-1].lower()
        if reviewer.get("isContainer") and group in teams:
            for voter in reviewer.get("votedFor") or []:
                member_of.setdefault(_login(voter, agents), set()).add(teams[group])
    reviews = []
    for reviewer in reviewers:
        if reviewer.get("isContainer"):
            continue
        vote = int(reviewer.get("vote", 0))
        if vote == 0:
            continue
        login = _login(reviewer, agents)
        state = "APPROVED" if _approved(vote) else "CHANGES_REQUESTED"
        reviews.append(
            {"user": login, "state": state, "commit_id": head_sha, "teams": sorted(member_of.get(login, []))}
        )

    commits = []
    for c in ado.get(f"{base}/commits").get("value", []):
        message = c.get("comment", "")
        if c.get("commentTruncated"):
            full = ado.get(f"git/repositories/{urllib.parse.quote(repository)}/commits/{c['commitId']}")
            message = full.get("comment", message)
        commits.append({"sha": c["commitId"], "message": message})

    statuses = ado.get(f"{base}/statuses").get("value", [])
    check = (config.gate.get("reviewerAgent") or {}).get("check", "reviewer-agent")
    mine = [
        s
        for s in statuses
        if s.get("context", {}).get("genre") == STATUS_GENRE and s.get("context", {}).get("name") == check
    ]
    latest = max(mine, key=lambda s: s.get("id", 0), default=None)
    conclusion = {"succeeded": "success", "failed": "failure", "error": "failure"}.get((latest or {}).get("state", ""))
    flagged = bool(latest and "flagged" in str(latest.get("description", "")).lower())

    label = (config.gate.get("pullRequest") or {}).get("incidentFixLabel", "incident-fix")
    # Azure DevOps does not record who added a tag; the label counts only together with a human's comment that
    # names it, so an agent cannot declare its own change an incident fix.
    incident_fix = False
    if any(lbl.get("name") == label for lbl in pr.get("labels") or []):
        for thread in ado.get(f"{base}/threads").get("value", []):
            for comment in thread.get("comments") or []:
                if label in str(comment.get("content", "")) and not _login(comment.get("author", {}), agents).endswith(
                    "[bot]"
                ):
                    incident_fix = True

    agent = (
        agents.get(str(pr["createdBy"].get("uniqueName", "")).lower())
        or agents.get(str(pr["createdBy"].get("displayName", "")).lower())
        or branch_agent(head_ref)
    )
    open_prs, merges_today = 0, {}
    if agent:
        creator = pr["createdBy"]["id"]
        repo_path = f"git/repositories/{urllib.parse.quote(repository)}/pullrequests"
        active = ado.get(repo_path, **{"searchCriteria.creatorId": creator, "searchCriteria.status": "active"})
        open_prs = len(active.get("value", []))
        done = ado.get(
            repo_path, **{"searchCriteria.creatorId": creator, "searchCriteria.status": "completed", "$top": 50}
        )
        today = now.date().isoformat()
        for merged in done.get("value", []):
            if not str(merged.get("closedDate", "")).startswith(today):
                continue
            for name in changed_files(ado, repository, merged["pullRequestId"]):
                for env in ("dev", "acc", "prd"):
                    if f"/{env}/" in f"/{name}" or f".{env}." in name:
                        merges_today[env] = merges_today.get(env, 0) + 1
                        break

    return {
        "repository": repository,
        "number": number,
        "author": author,
        "head_ref": head_ref,
        "head_sha": head_sha,
        "body": pr.get("description") or "",
        "commits": commits,
        "reviews": reviews,
        "reviewer_agent_conclusion": conclusion,
        "reviewer_agent_flagged": flagged,
        "incident_fix_by_human": incident_fix,
        "merges_today": merges_today,
        "open_pull_requests": open_prs,
    }


def changed_files(ado: AzureDevOps, repository: str, number: int) -> list[str]:
    base = ado.pr_path(repository, number)
    iterations = ado.get(f"{base}/iterations").get("value", [])
    if not iterations:
        return []
    last = max(i["id"] for i in iterations)
    changes = ado.get(f"{base}/iterations/{last}/changes", **{"$top": 2000}).get("changeEntries", [])
    paths = []
    for change in changes:
        for key in ("item", "originalPath"):
            value = change.get(key)
            path = value.get("path") if isinstance(value, dict) else value
            if path:
                paths.append(path.lstrip("/"))
    return sorted(set(paths))


def publish(ado: AzureDevOps, repository: str, number: int, decision: dict[str, Any], target_url: str = "") -> None:
    """Post the ``aks-agents/merge-gate`` status and, for tier low, set auto-complete as the gate's identity."""
    base = ado.pr_path(repository, number)
    state = {"pass": "succeeded", "wait": "pending", "reject": "failed"}[decision["decision"]]
    description = f"{decision['decision']} – tier {decision['tier']}"
    if decision.get("violations"):
        description += ": " + decision["violations"][0]
    elif decision.get("waiting"):
        description += ": waiting for " + decision["waiting"][0]
    ado.request(
        "POST",
        f"{base}/statuses",
        {
            "state": state,
            "description": description[:400],
            "context": {"genre": STATUS_GENRE, "name": "merge-gate"},
            **({"targetUrl": target_url} if target_url else {}),
        },
    )
    if decision.get("autoMerge"):
        me = ado.request("GET", "connectionData", params={"api-version": "7.1-preview"}, project=False)[
            "authenticatedUser"
        ]["id"]
        ado.request(
            "PATCH",
            base,
            {
                "autoCompleteSetBy": {"id": me},
                "completionOptions": {"mergeStrategy": "squash", "deleteSourceBranch": True},
            },
        )

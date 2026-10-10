"""Collects the pull request facts the gate needs from the GitHub REST API (standard library only)."""

from __future__ import annotations

import datetime as dt
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import GateConfig
from .pr import branch_agent


class GitHub:
    def __init__(self, token: str | None = None, api: str | None = None):
        self.token = token or os.environ.get("GITHUB_TOKEN", "")
        self.api = (api or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.api}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())

    def paged(self, path: str, params: dict[str, Any] | None = None, key: str | None = None) -> list[Any]:
        items: list[Any] = []
        page = 1
        while True:
            data = self.get(path, {**(params or {}), "per_page": 100, "page": page})
            batch = data[key] if key else data
            items += batch
            if len(batch) < 100:
                return items
            page += 1

    def team_member(self, org: str, team_slug: str, user: str) -> bool:
        try:
            data = self.get(f"/orgs/{org}/teams/{team_slug}/memberships/{user}")
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return False
            raise
        return data.get("state") == "active"


def collect(gh: GitHub, repository: str, number: int, config: GateConfig, now: dt.datetime | None = None) -> dict:
    """Facts of one pull request in the shape :meth:`aks_gate.pr.PullRequest.from_dict` reads."""
    now = now or dt.datetime.now(dt.UTC)
    org = repository.split("/")[0]
    pr = gh.get(f"/repos/{repository}/pulls/{number}")
    head_sha = pr["head"]["sha"]
    teams = config.gate.get("teams") or {}

    reviews = []
    for review in gh.paged(f"/repos/{repository}/pulls/{number}/reviews"):
        user = review["user"]["login"]
        member_of = (
            [] if user.endswith("[bot]") else [k for k, slug in teams.items() if gh.team_member(org, slug, user)]
        )
        reviews.append({"user": user, "state": review["state"], "commit_id": review["commit_id"], "teams": member_of})

    commits = [
        {"sha": c["sha"], "message": c["commit"]["message"]}
        for c in gh.paged(f"/repos/{repository}/pulls/{number}/commits")
    ]

    check_name = (config.gate.get("reviewerAgent") or {}).get("check", "reviewer-agent")
    runs = gh.get(f"/repos/{repository}/commits/{head_sha}/check-runs", {"check_name": check_name})["check_runs"]
    latest = max(runs, key=lambda r: r.get("started_at") or "", default=None)
    conclusion = latest.get("conclusion") if latest else None
    flagged = bool(latest and "flagged" in ((latest.get("output") or {}).get("title") or "").lower())

    label = (config.gate.get("pullRequest") or {}).get("incidentFixLabel", "incident-fix")
    incident_fix = False
    if any(lbl["name"] == label for lbl in pr.get("labels", [])):
        events = gh.paged(f"/repos/{repository}/issues/{number}/events")
        adders = [e["actor"]["login"] for e in events if e["event"] == "labeled" and e["label"]["name"] == label]
        incident_fix = bool(adders) and not adders[-1].endswith("[bot]")

    agent = config.agent_for_login(pr["user"]["login"]) or branch_agent(pr["head"]["ref"])
    open_prs, merges_today = 0, {}
    if agent:
        bot = (config.gate.get("agents") or {}).get(agent, "")
        query = f"repo:{repository} is:pr author:app/{bot.removesuffix('[bot]')}"
        open_prs = gh.get("/search/issues", {"q": query + " is:open"})["total_count"]
        since = now.date().isoformat()
        for merged in gh.get("/search/issues", {"q": query + f" is:merged merged:>={since}"})["items"]:
            files = [f["filename"] for f in gh.paged(f"/repos/{repository}/pulls/{merged['number']}/files")]
            for env in ("dev", "acc", "prd"):
                if any(f"/{env}/" in f"/{name}" or f".{env}." in name for name in files):
                    merges_today[env] = merges_today.get(env, 0) + 1

    return {
        "repository": repository,
        "number": number,
        "author": pr["user"]["login"],
        "head_ref": pr["head"]["ref"],
        "head_sha": head_sha,
        "body": pr.get("body") or "",
        "commits": commits,
        "reviews": reviews,
        "reviewer_agent_conclusion": conclusion,
        "reviewer_agent_flagged": flagged,
        "incident_fix_by_human": incident_fix,
        "merges_today": merges_today,
        "open_pull_requests": open_prs,
    }


def changed_files(gh: GitHub, repository: str, number: int) -> list[str]:
    files = gh.paged(f"/repos/{repository}/pulls/{number}/files")
    names = []
    for f in files:
        names.append(f["filename"])
        if f.get("previous_filename"):
            names.append(f["previous_filename"])
    return names

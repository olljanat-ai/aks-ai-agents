"""GitHub: each agent has its own GitHub App; the job passes a short-lived installation token."""

from __future__ import annotations

import base64
from typing import Any

from .base import REVIEW_TITLES, GitPlatform
from .github_api import GitHubClient

REVIEW_CHECK = "reviewer-agent"


class GitHubPlatform(GitPlatform):
    name = "github"
    max_body_length = 65_000

    def __init__(self, config: Any, client: GitHubClient | None = None):
        super().__init__(config)
        self.api = client or GitHubClient()

    def clone_url(self, repo: str) -> str:
        return self.config.get("git.cloneUrl", "https://github.com/{repo}.git").format(
            repo=repo, name=repo.split("/")[-1]
        )

    def git_auth_header(self) -> str | None:
        if not self.api.token:
            return None
        basic = base64.b64encode(f"x-access-token:{self.api.token}".encode()).decode()
        return f"Authorization: Basic {basic}"

    def commit_files(self, repo, base_sha, branch, message, changes, existing):
        return self.api.commit_files(repo, base_sha, branch, message, changes)

    def create_pull_request(self, repo, branch, title, body, draft):
        pr = self.api.post(
            f"/repos/{repo}/pulls",
            {"title": title, "head": branch, "base": self.default_branch, "body": body, "draft": draft},
        )
        return {"number": pr["number"], "url": pr["html_url"]}

    def create_issue(self, repo, title, body, labels):
        issue = self.api.post(f"/repos/{repo}/issues", {"title": title, "body": body, "labels": labels})
        return {"number": issue["number"], "url": issue["html_url"]}

    def search_issues(self, repo, text, state, labels):
        query = f"repo:{repo} {text}".strip()
        if state in ("open", "closed"):
            query += f" is:{state}"
        query += "".join(f' label:"{label}"' for label in labels)
        data = self.api.get("/search/issues", q=query, per_page=20)
        return [
            {
                "number": i["number"],
                "title": i["title"],
                "state": i["state"],
                "url": i["html_url"],
                "labels": [lbl["name"] for lbl in i.get("labels", [])],
                "updated": i["updated_at"],
                "pull_request": "pull_request" in i,
            }
            for i in data.get("items", [])
        ]

    def comment(self, repo, number, kind, body):
        c = self.api.post(f"/repos/{repo}/issues/{number}/comments", {"body": body})
        return {"url": c["html_url"]}

    def read_pull_request(self, repo, number):
        pr = self.api.get(f"/repos/{repo}/pulls/{number}")
        files = self.api.get(f"/repos/{repo}/pulls/{number}/files", per_page=100)
        diff = self.api.request("GET", f"/repos/{repo}/pulls/{number}", accept="application/vnd.github.diff")
        commits = self.api.get(f"/repos/{repo}/pulls/{number}/commits", per_page=100)
        checks = self.api.get(f"/repos/{repo}/commits/{pr['head']['sha']}/check-runs", per_page=100)
        reviews = self.api.get(f"/repos/{repo}/pulls/{number}/reviews", per_page=100)
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

    def publish_review(self, repo, number, head_sha, verdict, text, findings):
        review = self.api.post(
            f"/repos/{repo}/pulls/{number}/reviews",
            {
                "commit_id": head_sha,
                "event": "REQUEST_CHANGES" if verdict == "fail" else "COMMENT",
                "body": text,
                "comments": [
                    {"path": f["path"], "line": f["line"], "side": "RIGHT", "body": f"**{f['severity']}**: {f['body']}"}
                    for f in findings
                ],
            },
        )
        self.api.post(
            f"/repos/{repo}/check-runs",
            {
                "name": REVIEW_CHECK,
                "head_sha": head_sha,
                "status": "completed",
                "conclusion": "failure" if verdict == "fail" else "success",
                "output": {"title": REVIEW_TITLES[verdict], "summary": text[:60000]},
            },
        )
        return {"review": review.get("html_url"), "head_sha": head_sha}

    def dispatch_runbook(self, repo, runbook, inputs):
        self.api.post(
            f"/repos/{repo}/actions/workflows/{runbook}.yml/dispatches", {"ref": self.default_branch, "inputs": inputs}
        )
        return {"dispatched": True}

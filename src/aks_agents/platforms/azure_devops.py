"""Azure DevOps: Repos for code and pull requests, Boards work items for issues, Pipelines for runbooks.

Each agent's managed identity is added to the Azure DevOps organisation as its own user (no PAT, no secret): the run
gets an Entra ID token for Azure DevOps from the identity. Branch security lets it create and push only
``agent/<name>/*``; it never has *Contribute* on ``main`` and is never in the approving groups.

Mapping of the tools:

* commit + pull request → Git *pushes* API (one commit on a new branch) and *pull requests*;
* issues → work items of ``git.azureDevOps.workItemType`` tagged ``repo:<key>`` and with the labels as tags;
* review → pull request threads (inline comments) and the ``aks-agents/reviewer-agent`` status on the latest
  iteration, which the merge gate reads; the reviewer agent never votes;
* runbooks → runs of the pipeline named after the runbook in the runbooks repository, inputs as template parameters.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .base import REVIEW_TITLES, GitPlatform, PlatformError

API = "7.1"
AZURE_DEVOPS_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"
STATUS_GENRE = "aks-agents"
ZERO = "0" * 40
CLOSED_STATES = ("Done", "Closed", "Removed", "Resolved")
# Pull request thread status: informational comments are closed so they never block the comment-resolution policy;
# blocker and major findings stay active until someone resolves them.
THREAD_ACTIVE, THREAD_CLOSED = 1, 4


def entra_token() -> str:
    token = os.environ.get("AKS_AGENT_AZDO_TOKEN")
    if token:
        return token
    out = subprocess.run(
        [
            "az",
            "account",
            "get-access-token",
            "--resource",
            AZURE_DEVOPS_RESOURCE,
            "--query",
            "accessToken",
            "-o",
            "tsv",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if out.returncode != 0:
        raise PlatformError(401, f"no Azure DevOps token: {out.stderr.strip()[:300]}")
    return out.stdout.strip()


class AzureDevOpsClient:
    def __init__(self, organization_url: str, project: str, token: str | None = None):
        self.base = organization_url.rstrip("/")
        self.project = project
        self._token = token

    @property
    def token(self) -> str:
        if self._token is None:
            self._token = entra_token()
        return self._token

    def auth_header(self) -> str:
        if self.token.startswith("ey"):  # Entra ID / OAuth token
            return f"Bearer {self.token}"
        return "Basic " + base64.b64encode(f":{self.token}".encode()).decode()  # PAT (local testing)

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        params: dict[str, Any] | None = None,
        content_type: str = "application/json",
        api: str = API,
    ) -> Any:
        query = urllib.parse.urlencode({"api-version": api, **(params or {})})
        url = f"{self.base}/{urllib.parse.quote(self.project)}/_apis/{path}?{query}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method, headers={"Authorization": self.auth_header(), "Content-Type": content_type}
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                raw = response.read()
        except urllib.error.HTTPError as err:
            raise PlatformError(err.code, err.read().decode(errors="replace")[:500]) from err
        return json.loads(raw) if raw else None

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, body: Any, **kwargs: Any) -> Any:
        return self.request("POST", path, body, **kwargs)


def _repo_path(repo: str) -> str:
    return f"git/repositories/{urllib.parse.quote(repo)}"


class AzureDevOpsPlatform(GitPlatform):
    name = "azure-devops"
    max_body_length = 4000  # Azure DevOps limit of a pull request description

    def __init__(self, config: Any, client: AzureDevOpsClient | None = None):
        super().__init__(config)
        ado = config.get("git.azureDevOps") or {}
        self.organization_url = ado.get("organizationUrl", "").rstrip("/")
        self.project = ado.get("project", "")
        self.work_item_type = ado.get("workItemType", "Issue")
        self.api = client or AzureDevOpsClient(self.organization_url, self.project)

    def web_url(self, *parts: str) -> str:
        return "/".join([self.organization_url, urllib.parse.quote(self.project), *parts])

    # -- Git transport

    def clone_url(self, repo: str) -> str:
        template = self.config.get("git.cloneUrl")
        if template:
            return template.format(repo=repo, name=repo)
        return self.web_url("_git", urllib.parse.quote(repo))

    def git_auth_header(self) -> str | None:
        return f"Authorization: {self.api.auth_header()}"

    # -- changes

    def commit_files(self, repo, base_sha, branch, message, changes, existing):
        ref = f"refs/heads/{branch}"
        current = self.api.get(f"{_repo_path(repo)}/refs", filter=f"heads/{branch}").get("value", [])
        if current:
            raise PlatformError(409, f"branch {branch} already exists")
        items = []
        for path, content in sorted(changes.items()):
            item = {"item": {"path": "/" + path}}
            if content is None:
                items.append({"changeType": "delete", **item})
                continue
            items.append(
                {
                    "changeType": "edit" if path in existing else "add",
                    **item,
                    "newContent": {"content": base64.b64encode(content).decode(), "contentType": "base64encoded"},
                }
            )
        # A new branch: oldObjectId is the commit it starts from.
        push = self.api.post(
            f"{_repo_path(repo)}/pushes",
            {
                "refUpdates": [{"name": ref, "oldObjectId": base_sha}],
                "commits": [{"comment": message, "changes": items}],
            },
        )
        return push["commits"][-1]["commitId"]

    def create_pull_request(self, repo, branch, title, body, draft):
        pr = self.api.post(
            f"{_repo_path(repo)}/pullrequests",
            {
                "sourceRefName": f"refs/heads/{branch}",
                "targetRefName": f"refs/heads/{self.default_branch}",
                "title": title,
                "description": body[: self.max_body_length],
                "isDraft": draft,
            },
        )
        number = pr["pullRequestId"]
        return {"number": number, "url": self.web_url("_git", urllib.parse.quote(repo), "pullrequest", str(number))}

    # -- issues → work items

    def create_issue(self, repo, title, body, labels):
        tags = "; ".join([f"repo:{self._key(repo)}", *labels])
        ops = [
            {"op": "add", "path": "/fields/System.Title", "value": title},
            {"op": "add", "path": "/fields/System.Description", "value": body},
            {"op": "add", "path": "/multilineFieldsFormat/System.Description", "value": "Markdown"},
            {"op": "add", "path": "/fields/System.Tags", "value": tags},
        ]
        item = self.api.request(
            "POST",
            f"wit/workitems/${urllib.parse.quote(self.work_item_type)}",
            ops,
            content_type="application/json-patch+json",
        )
        return {"number": item["id"], "url": item["_links"]["html"]["href"]}

    def _key(self, repo: str) -> str:
        for key, name in (self.config.get("git.repositories") or {}).items():
            if name == repo:
                return key
        return repo

    def search_issues(self, repo, text, state, labels):
        def lit(value: str) -> str:
            return "'" + value.replace("'", "''") + "'"

        clauses = ["[System.TeamProject] = @project", f"[System.Tags] CONTAINS {lit('repo:' + self._key(repo))}"]
        if text:
            clauses.append(f"[System.Title] CONTAINS {lit(text)}")
        closed = ", ".join(lit(s) for s in CLOSED_STATES)
        if state == "open":
            clauses.append(f"[System.State] NOT IN ({closed})")
        elif state == "closed":
            clauses.append(f"[System.State] IN ({closed})")
        clauses += [f"[System.Tags] CONTAINS {lit(label)}" for label in labels]
        wiql = (
            "SELECT [System.Id] FROM WorkItems WHERE " + " AND ".join(clauses) + " ORDER BY [System.ChangedDate] DESC"
        )
        ids = [w["id"] for w in self.api.post("wit/wiql", {"query": wiql}, params={"$top": 20}).get("workItems", [])]
        if not ids:
            return []
        items = self.api.get(
            "wit/workitems",
            ids=",".join(map(str, ids)),
            fields="System.Title,System.State,System.Tags,System.ChangedDate",
        )
        out = []
        for item in items.get("value", []):
            fields = item["fields"]
            out.append(
                {
                    "number": item["id"],
                    "title": fields.get("System.Title"),
                    "state": fields.get("System.State"),
                    "url": self.web_url("_workitems", "edit", str(item["id"])),
                    "labels": [t.strip() for t in (fields.get("System.Tags") or "").split(";") if t.strip()],
                    "updated": fields.get("System.ChangedDate"),
                }
            )
        return out

    def comment(self, repo, number, kind, body):
        if kind == "pull_request":
            self.api.post(
                f"{_repo_path(repo)}/pullRequests/{number}/threads",
                {"comments": [{"content": body, "commentType": 1}], "status": THREAD_CLOSED},
            )
            return {"url": self.web_url("_git", urllib.parse.quote(repo), "pullrequest", str(number))}
        self.api.post(f"wit/workItems/{number}/comments", {"text": body}, api="7.1-preview.4")
        return {"url": self.web_url("_workitems", "edit", str(number))}

    # -- pull requests

    def _latest_iteration(self, repo: str, number: int) -> int | None:
        iterations = self.api.get(f"{_repo_path(repo)}/pullRequests/{number}/iterations").get("value", [])
        return max((i["id"] for i in iterations), default=None)

    def _diff(self, repo: str, number: int) -> str:
        """Unified diff of the pull request (Azure DevOps has no diff endpoint): the merge commit against its
        first parent, the target branch."""
        with tempfile.TemporaryDirectory() as tmp:
            auth = ["-c", f"http.extraHeader={self.git_auth_header()}"]

            def git(*args: str) -> str:
                proc = subprocess.run(["git", *auth, *args], cwd=tmp, capture_output=True, text=True, timeout=300)
                if proc.returncode != 0:
                    raise PlatformError(500, f"git {args[0]}: {proc.stderr.strip()[:300]}")
                return proc.stdout

            git("init", "--quiet")
            git("fetch", "--quiet", "--depth", "2", self.clone_url(repo), f"refs/pull/{number}/merge")
            return git("diff", "FETCH_HEAD^1", "FETCH_HEAD")

    def read_pull_request(self, repo, number):
        base = f"{_repo_path(repo)}/pullRequests/{number}"
        pr = self.api.get(base)
        latest = self._latest_iteration(repo, number)
        changes = self.api.get(f"{base}/iterations/{latest}/changes").get("changeEntries", []) if latest else []
        commits = self.api.get(f"{base}/commits").get("value", [])
        statuses = self.api.get(f"{base}/statuses").get("value", [])
        reviewers = self.api.get(f"{base}/reviewers").get("value", [])
        votes = {10: "APPROVED", 5: "APPROVED", -5: "CHANGES_REQUESTED", -10: "CHANGES_REQUESTED"}
        try:
            diff = self._diff(repo, number)
        except (PlatformError, OSError) as err:
            diff = f"(diff unavailable: {err})"
        return {
            "number": number,
            "title": pr["title"],
            "author": pr["createdBy"].get("uniqueName") or pr["createdBy"].get("displayName"),
            "head_ref": pr["sourceRefName"].removeprefix("refs/heads/"),
            "head_sha": pr["lastMergeSourceCommit"]["commitId"],
            "state": pr["status"],
            "merged": pr["status"] == "completed",
            "body": pr.get("description") or "",
            "files": [
                {"path": (c.get("item") or {}).get("path", "").lstrip("/"), "status": c.get("changeType")}
                for c in changes
            ],
            "commits": [{"sha": c["commitId"][:12], "message": c.get("comment", "")} for c in commits],
            "checks": [
                {
                    "name": f"{s['context'].get('genre')}/{s['context'].get('name')}",
                    "status": "completed",
                    "conclusion": s.get("state"),
                    "title": s.get("description"),
                    "iteration": s.get("iterationId"),
                }
                for s in statuses
            ],
            "reviews": [
                {"user": r.get("uniqueName") or r.get("displayName"), "state": votes[r["vote"]]}
                for r in reviewers
                if r.get("vote") in votes
            ],
            "diff": diff,
        }

    def publish_review(self, repo, number, head_sha, verdict, text, findings):
        base = f"{_repo_path(repo)}/pullRequests/{number}"
        self.api.post(
            f"{base}/threads",
            {
                "comments": [{"content": text, "commentType": 1}],
                "status": THREAD_ACTIVE if verdict == "fail" else THREAD_CLOSED,
            },
        )
        for f in findings:
            blocking = f["severity"] in ("blocker", "major")
            self.api.post(
                f"{base}/threads",
                {
                    "comments": [{"content": f"**{f['severity']}**: {f['body']}", "commentType": 1}],
                    "status": THREAD_ACTIVE if blocking else THREAD_CLOSED,
                    "threadContext": {
                        "filePath": "/" + f["path"].lstrip("/"),
                        "rightFileStart": {"line": f["line"], "offset": 1},
                        "rightFileEnd": {"line": f["line"], "offset": 1},
                    },
                },
            )
        status = {
            "state": "failed" if verdict == "fail" else "succeeded",
            "description": REVIEW_TITLES[verdict],
            "context": {"genre": STATUS_GENRE, "name": "reviewer-agent"},
        }
        latest = self._latest_iteration(repo, number)
        if latest is not None:
            status["iterationId"] = latest  # a new push makes it stale
        self.api.post(f"{base}/statuses", status)
        return {"head_sha": head_sha, "iteration": latest}

    def dispatch_runbook(self, repo, runbook, inputs):
        pipelines = self.api.get("pipelines", **{"$top": 1000}).get("value", [])
        pipeline = next((p for p in pipelines if p["name"] == runbook), None)
        if pipeline is None:
            raise PlatformError(404, f"no pipeline named {runbook} in {self.project}")
        run = self.api.post(
            f"pipelines/{pipeline['id']}/runs",
            {
                "resources": {"repositories": {"self": {"refName": f"refs/heads/{self.default_branch}"}}},
                "templateParameters": inputs,
            },
        )
        return {
            "dispatched": True,
            "run": run.get("id"),
            "url": ((run.get("_links") or {}).get("web") or {}).get("href"),
        }

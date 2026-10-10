import pytest

from aks_agents.platforms import create
from aks_agents.platforms.azure_devops import AzureDevOpsPlatform
from aks_agents.platforms.github import GitHubPlatform
from aks_agents.settings import PlatformConfig


class Recorder:
    """Stands in for a REST client: records calls, answers from a table keyed by (method, path prefix)."""

    token = "ghs_x"

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    def _answer(self, method, path):
        for (m, prefix), value in self.answers.items():
            if m == method and path.startswith(prefix):
                return value
        return {}

    def request(self, method, path, body=None, params=None, **kwargs):
        self.calls.append((method, path, body, params, kwargs))
        return self._answer(method, path)

    def get(self, path, **params):
        return self.request("GET", path, params=params)

    def post(self, path, body, **kwargs):
        return self.request("POST", path, body, **kwargs)

    def auth_header(self):
        return "Bearer ey-token"


ADO_CONFIG = PlatformConfig(
    {
        "git": {
            "platform": "azure-devops",
            "repositories": {"iac": "aks-iac", "runbooks": "aks-runbooks"},
            "azureDevOps": {
                "organizationUrl": "https://dev.azure.com/org",
                "project": "platform",
                "workItemType": "Task",
            },
        }
    }
)


def test_factory():
    assert isinstance(create(PlatformConfig({})), GitHubPlatform)
    with pytest.raises(ValueError):
        create(PlatformConfig({"git": {"platform": "gitlab"}}))


def test_github_pull_request_review_and_runbook():
    api = Recorder({("POST", "/repos/o/iac/pulls"): {"number": 4, "html_url": "u"}})
    gh = GitHubPlatform(PlatformConfig({}), client=api)
    assert gh.create_pull_request("o/iac", "agent/a/x", "t", "b", False) == {"number": 4, "url": "u"}
    assert api.calls[-1][2]["base"] == "main" and api.calls[-1][2]["head"] == "agent/a/x"
    gh.publish_review(
        "o/iac", 4, "sha", "flag", "text", [{"path": "a.tf", "line": 3, "severity": "minor", "body": "b"}]
    )
    review, check = api.calls[-2], api.calls[-1]
    assert review[2]["event"] == "COMMENT" and review[2]["comments"][0]["line"] == 3
    assert check[2]["conclusion"] == "success" and "flagged" in check[2]["output"]["title"]
    gh.dispatch_runbook("o/runbooks", "drain-cell", {"cell": "sl-az1"})
    assert api.calls[-1][1] == "/repos/o/runbooks/actions/workflows/drain-cell.yml/dispatches"
    assert gh.git_auth_header().startswith("Authorization: Basic ")


def test_github_search_query():
    api = Recorder({("GET", "/search/issues"): {"items": []}})
    GitHubPlatform(PlatformConfig({}), client=api).search_issues("o/iac", "sl-az1 5xx", "open", ["incident"])
    assert api.calls[-1][3]["q"] == 'repo:o/iac sl-az1 5xx is:open label:"incident"'


def test_azure_devops_commit_is_one_push_on_a_new_branch():
    api = Recorder(
        {
            ("GET", "git/repositories/aks-iac/refs"): {"value": []},
            ("POST", "git/repositories/aks-iac/pushes"): {"commits": [{"commitId": "c1"}]},
        }
    )
    ado = AzureDevOpsPlatform(ADO_CONFIG, client=api)
    sha = ado.commit_files(
        "aks-iac",
        "base1",
        "agent/upgrade-agent/x",
        "msg",
        {"a.tf": b"new", "b.tf": b"changed", "c.tf": None},
        existing={"b.tf", "c.tf"},
    )
    push = api.calls[-1][2]
    assert sha == "c1"
    assert push["refUpdates"] == [{"name": "refs/heads/agent/upgrade-agent/x", "oldObjectId": "base1"}]
    assert [(c["changeType"], c["item"]["path"]) for c in push["commits"][0]["changes"]] == [
        ("add", "/a.tf"),
        ("edit", "/b.tf"),
        ("delete", "/c.tf"),
    ]


def test_azure_devops_existing_branch_is_refused():
    api = Recorder({("GET", "git/repositories/aks-iac/refs"): {"value": [{"name": "refs/heads/x"}]}})
    with pytest.raises(Exception, match="already exists"):
        AzureDevOpsPlatform(ADO_CONFIG, client=api).commit_files("aks-iac", "b", "x", "m", {"a": b"1"}, set())


def test_azure_devops_pull_request_and_work_item():
    api = Recorder(
        {
            ("POST", "git/repositories/aks-iac/pullrequests"): {"pullRequestId": 12},
            ("POST", "wit/workitems/$Task"): {"id": 99, "_links": {"html": {"href": "wi-url"}}},
        }
    )
    ado = AzureDevOpsPlatform(ADO_CONFIG, client=api)
    out = ado.create_pull_request("aks-iac", "agent/a/x", "t", "x" * 5000, True)
    body = api.calls[-1][2]
    assert out == {"number": 12, "url": "https://dev.azure.com/org/platform/_git/aks-iac/pullrequest/12"}
    assert body["targetRefName"] == "refs/heads/main" and body["isDraft"] and len(body["description"]) == 4000
    assert ado.create_issue("aks-iac", "Incident", "body", ["incident"]) == {"number": 99, "url": "wi-url"}
    ops = {op["path"]: op["value"] for op in api.calls[-1][2]}
    assert ops["/fields/System.Tags"] == "repo:iac; incident"
    assert api.calls[-1][4]["content_type"] == "application/json-patch+json"


def test_azure_devops_search_uses_wiql():
    api = Recorder(
        {
            ("POST", "wit/wiql"): {"workItems": [{"id": 5}]},
            ("GET", "wit/workitems"): {
                "value": [
                    {
                        "id": 5,
                        "fields": {"System.Title": "x", "System.State": "Active", "System.Tags": "repo:iac; incident"},
                    }
                ]
            },
        }
    )
    found = AzureDevOpsPlatform(ADO_CONFIG, client=api).search_issues("aks-iac", "O'Brien", "open", ["incident"])
    wiql = api.calls[0][2]["query"]
    assert "'repo:iac'" in wiql and "'O''Brien'" in wiql and "NOT IN" in wiql
    assert found[0]["labels"] == ["repo:iac", "incident"]


def test_azure_devops_review_posts_threads_and_iteration_status():
    api = Recorder({("GET", "git/repositories/aks-iac/pullRequests/12/iterations"): {"value": [{"id": 1}, {"id": 3}]}})
    ado = AzureDevOpsPlatform(ADO_CONFIG, client=api)
    ado.publish_review(
        "aks-iac",
        12,
        "sha",
        "fail",
        "summary",
        [
            {"path": "main.tf", "line": 8, "severity": "blocker", "body": "deletes the vault"},
            {"path": "main.tf", "line": 9, "severity": "info", "body": "fyi"},
        ],
    )
    threads = [c[2] for c in api.calls if c[1].endswith("/threads")]
    assert [t["status"] for t in threads] == [1, 1, 4]  # summary of a failed review and the blocker stay active
    assert threads[1]["threadContext"]["filePath"] == "/main.tf"
    status = next(c[2] for c in api.calls if c[1].endswith("/statuses"))
    assert status == {
        "state": "failed",
        "description": "Reviewer agent: changes required",
        "context": {"genre": "aks-agents", "name": "reviewer-agent"},
        "iterationId": 3,
    }


def test_azure_devops_runbook_is_a_pipeline_run():
    api = Recorder(
        {
            ("GET", "pipelines"): {"value": [{"name": "halt-release", "id": 41}, {"name": "drain-cell", "id": 42}]},
            ("POST", "pipelines/42/runs"): {"id": 7},
        }
    )
    out = AzureDevOpsPlatform(ADO_CONFIG, client=api).dispatch_runbook("aks-runbooks", "drain-cell", {"cell": "sl-az2"})
    assert out["run"] == 7 and api.calls[-1][2]["templateParameters"] == {"cell": "sl-az2"}


def test_azure_devops_comment_kinds():
    api = Recorder()
    ado = AzureDevOpsPlatform(ADO_CONFIG, client=api)
    ado.comment("aks-iac", 12, "pull_request", "verified")
    assert api.calls[-1][1].endswith("pullRequests/12/threads") and api.calls[-1][2]["status"] == 4
    ado.comment("aks-iac", 99, "issue", "update")
    assert api.calls[-1][1] == "wit/workItems/99/comments"

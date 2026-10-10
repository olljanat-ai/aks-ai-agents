import datetime as dt

from test_gate import BODY, MESSAGE, TUESDAY, patch_plan

from aks_gate.azure_devops import AzureDevOps, changed_files, collect, publish
from aks_gate.gate import PASS, decide
from aks_gate.pr import PullRequest

BASE = "git/repositories/aks-iac/pullRequests/5"


class FakeAdo(AzureDevOps):
    def __init__(self, responses):
        super().__init__("https://dev.azure.com/org", "platform", token="pat")
        self.responses = responses
        self.calls = []

    def request(self, method, path, body=None, params=None, project=True):
        self.calls.append((method, path, body, params))
        if method != "GET":
            return {}
        return (
            self.responses[path]
            if path in self.responses
            else self.responses[(path, tuple(sorted((params or {}).items())))]
        )


def responses(**overrides):
    pr = {
        "createdBy": {
            "id": "sp1",
            "uniqueName": "id-agent-upgrade-agent-dev",
            "displayName": "id-agent-upgrade-agent-dev",
        },
        "lastMergeSourceCommit": {"commitId": "abc"},
        "sourceRefName": "refs/heads/agent/upgrade-agent/dev",
        "description": BODY,
        "labels": [],
    }
    r = {
        BASE: pr,
        f"{BASE}/reviewers": {
            "value": [
                {
                    "displayName": "[platform]\\platform-engineers",
                    "isContainer": True,
                    "vote": 10,
                    "votedFor": [{"uniqueName": "alice@example.com", "displayName": "Alice"}],
                },
                {"uniqueName": "alice@example.com", "displayName": "Alice", "vote": 10},
                {"uniqueName": "id-agent-reviewer-agent-prd", "displayName": "id-agent-reviewer-agent-prd", "vote": 10},
            ]
        },
        f"{BASE}/commits": {"value": [{"commitId": "abc", "comment": MESSAGE}]},
        f"{BASE}/statuses": {
            "value": [
                {"id": 1, "state": "failed", "context": {"genre": "aks-agents", "name": "reviewer-agent"}},
                {
                    "id": 2,
                    "state": "succeeded",
                    "description": "pass",
                    "context": {"genre": "aks-agents", "name": "reviewer-agent"},
                },
            ]
        },
        f"{BASE}/iterations": {"value": [{"id": 1}, {"id": 2}]},
        f"{BASE}/iterations/2/changes": {
            "changeEntries": [{"item": {"path": "/environments/dev/versions.auto.tfvars"}}]
        },
        (
            "git/repositories/aks-iac/pullrequests",
            (("searchCriteria.creatorId", "sp1"), ("searchCriteria.status", "active")),
        ): {"value": [{}]},
        (
            "git/repositories/aks-iac/pullrequests",
            (("$top", 50), ("searchCriteria.creatorId", "sp1"), ("searchCriteria.status", "completed")),
        ): {"value": []},
    }
    r.update(overrides)
    return r


def test_collect_maps_votes_groups_and_statuses(config):
    ado = FakeAdo(responses())
    data = collect(ado, "aks-iac", 5, config, now=TUESDAY)
    assert data["author"] == "id-agent-upgrade-agent-dev[bot]"
    assert data["head_ref"] == "agent/upgrade-agent/dev"
    assert {"user": "alice@example.com", "state": "APPROVED", "commit_id": "abc", "teams": ["platform"]} in data[
        "reviews"
    ]
    assert any(
        r["user"] == "id-agent-reviewer-agent-prd[bot]" for r in data["reviews"]
    )  # an agent vote never counts as human
    assert data["reviewer_agent_conclusion"] == "success" and data["open_pull_requests"] == 1
    assert changed_files(ado, "aks-iac", 5) == ["environments/dev/versions.auto.tfvars"]

    decision = decide(PullRequest.from_dict(data), patch_plan("dev"), config, TUESDAY)
    assert decision.agent == "upgrade-agent" and decision.decision == PASS


def test_publish_posts_status_and_auto_complete():
    ado = FakeAdo({"connectionData": {"authenticatedUser": {"id": "gate-id"}}})
    publish(ado, "aks-iac", 5, {"decision": "pass", "tier": "low", "autoMerge": True, "violations": [], "waiting": []})
    status = next(c for c in ado.calls if c[1].endswith("/statuses"))
    assert status[2]["state"] == "succeeded" and status[2]["context"] == {"genre": "aks-agents", "name": "merge-gate"}
    patch = next(c for c in ado.calls if c[0] == "PATCH")
    assert patch[2]["autoCompleteSetBy"] == {"id": "gate-id"}


def test_publish_wait_is_pending_without_auto_complete():
    ado = FakeAdo({})
    publish(
        ado,
        "aks-iac",
        5,
        {
            "decision": "wait",
            "tier": "medium",
            "autoMerge": False,
            "violations": [],
            "waiting": ["approval from platform"],
        },
    )
    assert ado.calls[0][2]["state"] == "pending" and "approval from platform" in ado.calls[0][2]["description"]
    assert len(ado.calls) == 1


def test_incident_fix_needs_a_human_comment(config):
    r = responses()
    r[BASE] = {**r[BASE], "labels": [{"name": "incident-fix"}]}
    r[f"{BASE}/threads"] = {
        "value": [{"comments": [{"content": "incident-fix", "author": {"uniqueName": "id-agent-upgrade-agent-dev"}}]}]
    }
    assert collect(FakeAdo(r), "aks-iac", 5, config, now=TUESDAY)["incident_fix_by_human"] is False
    r[f"{BASE}/threads"]["value"][0]["comments"].append(
        {"content": "incident-fix: approved", "author": {"uniqueName": "bob@example.com"}}
    )
    assert (
        collect(FakeAdo(r), "aks-iac", 5, config, now=dt.datetime(2026, 10, 13, tzinfo=dt.UTC))["incident_fix_by_human"]
        is True
    )


def test_agent_identity_patterns(config):
    assert config.agent_for_login("id-agent-capacity-agent-prd[bot]") == "capacity-agent"
    assert config.agent_for_login("aks-capacity-agent[bot]") == "capacity-agent"
    assert config.agent_for_login("alice@example.com") is None


def test_stale_reviewer_status_is_ignored(config):
    r = responses()
    r[f"{BASE}/statuses"] = {
        "value": [
            {
                "id": 9,
                "iterationId": 1,
                "state": "succeeded",
                "context": {"genre": "aks-agents", "name": "reviewer-agent"},
            }
        ]
    }
    assert collect(FakeAdo(r), "aks-iac", 5, config, now=TUESDAY)["reviewer_agent_conclusion"] is None

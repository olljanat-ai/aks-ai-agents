from aks_agents.audit import RunRecorder
from aks_agents.permissions import collect, report


def log_run(runs, run_id, calls):
    rec = RunRecorder(runs, run_id)
    rec.event(
        "run_start",
        agent="platform-agent",
        environment="dev",
        tools=["azure_cli", "kubectl", "repo_write", "fetch_documentation"],
    )
    for i, (tool, args, error) in enumerate(calls):
        rec.event("tool_call", id=str(i), tool=tool, input=args)
        rec.event("tool_result", id=str(i), tool=tool, is_error=error, content="x")
    rec.close()


def test_permissions_report(tmp_path):
    log_run(
        tmp_path,
        "r1",
        [
            ("azure_cli", {"args": ["aks", "show", "-n", "c"]}, False),
            ("azure_cli", {"args": ["aks", "nodepool", "update", "-n", "p"]}, False),
            ("kubectl", {"args": ["get", "pods", "-A"]}, False),
            ("kubectl", {"args": ["rollout", "restart", "deploy/traefik"]}, False),
            ("repo_write", {"repo": "fleet", "path": "infrastructure/traefik/values.yaml"}, False),
            ("kubectl", {"args": ["get", "secrets"]}, True),
        ],
    )
    usage = collect(tmp_path)["platform-agent"]
    assert usage.az_read == {"aks show": 1} and usage.az_write == {"aks nodepool update": 1}
    assert usage.kube_write == {"rollout restart": 1}
    text = report({"platform-agent": usage})
    assert "Direct writes during prototyping" in text and "`az aks nodepool update` (1)" in text
    assert "Granted but never used (remove?): fetch_documentation" in text
    assert "Azure Kubernetes Service RBAC Reader" in text

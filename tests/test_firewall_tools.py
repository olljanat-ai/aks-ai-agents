import datetime as dt
import json

import pytest

from aks_agents.audit import RunRecorder
from aks_agents.cli import validate
from aks_agents.definition import AgentDefinition, ToolGrant, load_all
from aks_agents.settings import GUARDED, PlatformConfig, Settings
from aks_agents.tools.base import REGISTRY, ToolContext, ToolError, tools_for

NOW = dt.datetime.now(dt.UTC)


def rec(minutes_ago, src, fqdn=None, ip=None, port=443, action="Deny", category="AZFWApplicationRule"):
    return {
        "TimeGenerated": (NOW - dt.timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z"),
        "Category": category,
        "Action": action,
        "SourceIp": src,
        "Fqdn": fqdn,
        "DestinationIp": ip,
        "DestinationPort": port,
        "Protocol": "HTTPS" if fqdn else "TCP",
        "ActionReason": "Default Action",
    }


POD = {
    "metadata": {
        "name": "aks-hello-6f7d9c8b5-x2k4q",
        "namespace": "int-se-example",
        "labels": {"app.kubernetes.io/name": "aks-hello", "pod-template-hash": "6f7d9c8b5"},
        "ownerReferences": [{"kind": "ReplicaSet", "name": "aks-hello-6f7d9c8b5", "controller": True}],
    },
    "spec": {"nodeName": "aks-intse-abc", "containers": [{"image": "acronek8sdev.azurecr.io/aks-hello:0.4.0"}]},
    "status": {"phase": "Running", "podIP": "100.64.32.17", "podIPs": [{"ip": "100.64.32.17"}]},
}
NODE = {
    "metadata": {"name": "aks-intse-abc", "labels": {"platform/zone": "int-se"}},
    "status": {"addresses": [{"type": "InternalIP", "address": "100.64.32.4"}]},
}
NAMESPACE = {
    "metadata": {
        "name": "int-se-example",
        "labels": {"platform/zone": "int-se", "platform/team": "example"},
        "annotations": {"platform/contact": "https://github.com/onek8s/aks-fluxcd-example/issues"},
    }
}


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    platform = PlatformConfig.load("config/onek8s-dev.yaml")
    # The file export (firewall.log: file); the Log Analytics test switches it back
    platform.raw["environments"]["dev"]["firewall"]["log"] = "file"
    definition = AgentDefinition(
        name="egress-agent",
        description="",
        instructions="",
        tools=[ToolGrant("firewall_denied_flows"), ToolGrant("pods_by_ip")],
    )
    tools_for(definition.tools)
    c = ToolContext(
        agent=definition,
        settings=Settings(environment="dev", mode=GUARDED),
        platform=platform,
        recorder=RunRecorder(tmp_path, "r"),
        run_id="r",
    )
    log = tmp_path / "denied.jsonl"
    log.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                rec(5, "100.64.32.17", fqdn="api.open-meteo.com"),
                rec(65, "100.64.32.17", fqdn="api.open-meteo.com"),
                rec(30, "100.64.32.18", fqdn="api.open-meteo.com"),
                rec(10, "100.64.32.17", ip="1.2.3.4", category="AZFWNetworkRule"),
                rec(10, "100.64.32.17", fqdn="allowed.example.org", action="Allow"),
                rec(600, "100.64.32.17", fqdn="old.example.org"),
                rec(5, "10.0.8.4", fqdn="from-management.example.org"),
            ]
        )
        + "\nnot json\n"
    )
    monkeypatch.setenv("AKS_AGENT_FIREWALL_LOG", str(log))
    calls = []

    def fake(argv):
        calls.append(argv)
        kind = argv[argv.index("get") + 1]
        return json.dumps({"items": {"pods": [POD], "nodes": [NODE], "namespaces": [NAMESPACE]}[kind]})

    c.state["exec"] = fake
    c.state["calls"] = calls
    return c


def test_denied_flows_grouped_by_source_and_destination(ctx):
    out = json.loads(REGISTRY["firewall_denied_flows"].run(ctx, {"since_hours": 3}))
    flows = {(f["sourceIp"], f["destination"]): f for f in out["flows"]}
    assert out["deniedConnections"] == 4
    assert flows[("100.64.32.17", "api.open-meteo.com")]["count"] == 2
    assert flows[("100.64.32.17", "api.open-meteo.com")]["destinationIsFqdn"]
    assert flows[("100.64.32.17", "api.open-meteo.com")]["cluster"] == "aks-dev-int-stateless-az3"
    assert not flows[("100.64.32.17", "1.2.3.4")]["destinationIsFqdn"]
    # Allowed, too old, and outside the cells' prefixes: left out
    assert ("100.64.32.17", "allowed.example.org") not in flows
    assert ("100.64.32.17", "old.example.org") not in flows
    assert ("10.0.8.4", "from-management.example.org") not in flows
    assert out["spoke"] == "dev-int"


def test_denied_flows_only_within_the_cells(ctx):
    out = json.loads(
        REGISTRY["firewall_denied_flows"].run(ctx, {"since_hours": 3, "source_prefixes": ["100.64.32.18/32"]})
    )
    assert [f["sourceIp"] for f in out["flows"]] == ["100.64.32.18"]
    with pytest.raises(ToolError):
        REGISTRY["firewall_denied_flows"].run(ctx, {"since_hours": 3, "source_prefixes": ["10.0.0.0/8"]})


def test_pods_by_ip_names_the_workload_and_its_owner(ctx):
    out = json.loads(
        REGISTRY["pods_by_ip"].run(
            ctx, {"cluster": "aks-dev-int-stateless-az3", "ips": ["100.64.32.17", "100.64.32.4", "100.64.32.99"]}
        )
    )
    pod, node, gone = out
    assert pod["kind"] == "pod" and pod["namespace"] == "int-se-example" and pod["app"] == "aks-hello"
    assert pod["workload"] == {"kind": "Deployment", "name": "aks-hello"}
    assert pod["zone"] == "int-se" and pod["team"] == "example" and pod["contact"].endswith("/issues")
    assert node["kind"] == "node" and node["node"] == "aks-intse-abc"
    assert gone["kind"] == "unknown"
    # Read verbs only, never Secrets
    for argv in ctx.state["calls"]:
        assert argv[argv.index("get") + 1] in {"pods", "nodes", "namespaces"}


def test_pods_by_ip_refuses_another_environments_cluster_and_bad_addresses(ctx):
    with pytest.raises(ToolError):
        REGISTRY["pods_by_ip"].run(ctx, {"cluster": "aks-prd-sl-az1", "ips": ["100.64.32.17"]})
    with pytest.raises(ToolError):
        REGISTRY["pods_by_ip"].run(ctx, {"cluster": "aks-dev-int-stateless-az3", "ips": ["; rm -rf /"]})


def test_egress_agent_writes_only_application_egress_files():
    defs = load_all("agents")
    egress = defs["egress-agent"]
    assert egress.grant("repo_write").options == {
        "paths": {"firewall": ["stacks/platform-extensions/firewall-applications/*/*.yaml"]}
    }
    assert egress.grant("create_pull_request").options == {"repositories": ["firewall"]}
    # The only write outside the run's workspace is the pull request
    assert {g.name for g in egress.tools if REGISTRY[g.name].writes} == {"create_pull_request"}
    assert validate({"egress-agent": egress}, PlatformConfig.load("config/onek8s-dev.yaml")) == []
    assert validate({"egress-agent": egress}, PlatformConfig.load("config/platform.yaml")) == []


def test_log_analytics_reads_labfws_table_or_azure_firewalls(ctx, monkeypatch):
    from aks_agents.tools.firewall import kql

    assert "LabfwFirewallLog_CL" in kql(3, "LabfwFirewallLog_CL") and "ago(3h)" in kql(3, "LabfwFirewallLog_CL")
    assert "AZFWApplicationRule" in kql(3, None)
    with pytest.raises(ToolError):
        kql(3, "x | evil")
    monkeypatch.setenv("AKS_AGENT_FIREWALL_WORKSPACE", "00000000-0000-0000-0000-00000000000a")
    rows = [rec(5, "100.64.32.17", fqdn="api.open-meteo.com")]

    def fake(argv):
        assert argv[:4] == ["az", "monitor", "log-analytics", "query"]
        assert argv[argv.index("-w") + 1] == "00000000-0000-0000-0000-00000000000a"
        assert "LabfwFirewallLog_CL" in argv[argv.index("--analytics-query") + 1]
        return json.dumps(rows)

    ctx.state["exec"] = fake
    ctx.platform.raw["environments"]["dev"]["firewall"]["log"] = "log_analytics"
    out = json.loads(REGISTRY["firewall_denied_flows"].run(ctx, {"since_hours": 2}))
    assert out["log"] == "log_analytics" and out["flows"][0]["destination"] == "api.open-meteo.com"

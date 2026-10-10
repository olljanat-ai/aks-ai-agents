import json

import pytest

from aks_agents.audit import RunRecorder
from aks_agents.definition import AgentDefinition, ToolGrant
from aks_agents.settings import GUARDED, PROTOTYPE, PlatformConfig, Settings
from aks_agents.tools.base import REGISTRY, ToolContext, ToolError, tools_for
from aks_agents.tools.web import html_to_text


@pytest.fixture
def ctx(tmp_path):
    platform = PlatformConfig.load("config/platform.yaml")
    definition = AgentDefinition(
        name="operations-agent",
        description="",
        instructions="",
        tools=[
            ToolGrant(n)
            for n in (
                "azure_cli",
                "azure_resource_graph",
                "log_analytics_query",
                "kubectl",
                "flux_status",
                "fetch_documentation",
            )
        ],
    )
    tools_for(definition.tools)
    c = ToolContext(
        agent=definition,
        settings=Settings(environment="prd", mode=GUARDED),
        platform=platform,
        recorder=RunRecorder(tmp_path, "r"),
        run_id="r",
    )
    c.state["calls"] = []

    def fake(argv):
        c.state["calls"].append(argv)
        if "json" in argv and "get" in argv:
            return json.dumps(
                {
                    "items": [
                        {
                            "kind": "Kustomization",
                            "metadata": {"name": "apps", "namespace": "flux-system"},
                            "status": {
                                "conditions": [
                                    {
                                        "type": "Ready",
                                        "status": "False",
                                        "reason": "HealthCheckFailed",
                                        "message": "timeout",
                                    }
                                ],
                                "lastAppliedRevision": "prd-sl-az1@sha256:1",
                            },
                        }
                    ]
                }
            )
        return "{}"

    c.state["exec"] = fake
    return c


def run(ctx, name, **args):
    return REGISTRY[name].run(ctx, args)


@pytest.mark.parametrize(
    "args",
    [
        ["aks", "show", "-g", "rg", "-n", "aks-prd-sl-az1"],
        ["aks", "get-versions", "-l", "swedencentral"],
        ["monitor", "metrics", "list", "--resource", "x"],
        ["policy", "state", "summarize"],
        ["keyvault", "secret", "list", "--vault-name", "kv"],
    ],
)
def test_az_read_commands_allowed(ctx, args):
    run(ctx, "azure_cli", args=args)
    argv = ctx.state["calls"][-1]
    assert argv[0] == "az" and "--subscription" in argv and "sub-aks-prd" in argv


@pytest.mark.parametrize(
    "args",
    [
        ["aks", "upgrade", "-g", "rg", "-n", "c"],
        ["group", "delete", "-n", "rg"],
        ["role", "assignment", "create"],
        ["keyvault", "secret", "show", "--name", "s"],
        ["aks", "get-credentials", "-n", "c", "--admin"],
        ["account", "get-access-token"],
        ["rest", "--method", "put"],
    ],
)
def test_az_writes_and_secrets_denied(ctx, args):
    with pytest.raises(ToolError):
        run(ctx, "azure_cli", args=args)


def test_prototype_allows_writes_but_not_secrets(ctx):
    ctx.settings.mode = PROTOTYPE
    run(ctx, "azure_cli", args=["aks", "upgrade", "-g", "rg", "-n", "c", "--yes"])
    run(ctx, "kubectl", cluster="aks-prd-sl-az1", args=["rollout", "restart", "deploy/x", "-n", "traefik"])
    with pytest.raises(ToolError):
        run(ctx, "azure_cli", args=["keyvault", "secret", "show", "--name", "s"])
    with pytest.raises(ToolError):
        run(ctx, "kubectl", cluster="aks-prd-sl-az1", args=["get", "-n", "x", "secrets"])


@pytest.mark.parametrize(
    "args",
    [
        ["delete", "pod", "x"],
        ["apply", "-f", "x.yaml"],
        ["get", "secret", "s", "-o", "yaml"],
        ["get", "pods,secrets", "-A"],
        ["get", "pods", "--context", "other"],
        ["logs", "deploy/shop", "-n", "extfi-shop"],
    ],
)
def test_kubectl_denied(ctx, args):
    with pytest.raises(ToolError):
        run(ctx, "kubectl", cluster="aks-prd-sl-az1", args=args)


def test_kubectl_read_and_cluster_check(ctx):
    run(ctx, "kubectl", cluster="aks-prd-sl-az1", args=["logs", "deploy/source-controller", "-n", "flux-system"])
    assert ctx.state["calls"][-1][:3] == ["kubectl", "--context", "aks-prd-sl-az1"]
    assert "--tail=500" in ctx.state["calls"][-1]
    with pytest.raises(ToolError, match="cluster must be"):
        run(ctx, "kubectl", cluster="aks-dev-sl-az1", args=["get", "nodes"])


def test_flux_status(ctx):
    status = json.loads(run(ctx, "flux_status", cluster="aks-prd-sl-az2"))
    assert status[0]["ready"] == "False" and status[0]["message"] == "timeout"


def test_log_analytics_denies_application_tables(ctx):
    with pytest.raises(ToolError, match="application data"):
        run(ctx, "log_analytics_query", query="AppRequests | take 10", timespan="PT1H")
    run(ctx, "log_analytics_query", query="KubeEvents | take 10", timespan="PT1H")


def test_documentation_allow_list(ctx):
    with pytest.raises(ToolError, match="not an allowed"):
        run(ctx, "fetch_documentation", url="https://evil.example.com/x")
    assert html_to_text("<html><script>x</script><p>Hello &amp; bye</p></html>") == "Hello & bye"

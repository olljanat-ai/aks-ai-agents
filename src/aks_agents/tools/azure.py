"""Azure tools. In guarded mode they are read-only on top of read-only roles (Reader, Monitoring Reader, ...);
the allow-list here is a second line that keeps an agent from even trying a write. In prototype mode (build
tenant) any ``az`` command runs and is recorded, so that the least-privilege roles can be derived from it later.
Commands that would reveal secret values are denied in both modes."""

from __future__ import annotations

import json
import re
from typing import Any

from ..settings import PROTOTYPE
from .base import ToolContext, ToolError, tool
from .shell import execute, flag_value

READ_VERBS = re.compile(r"^(show|list|get|query|summarize|get-versions|get-upgrades|list-[\w-]+|show-[\w-]+)$")
READ_SPECIAL = {
    ("monitor", "metrics", "list"),
    ("monitor", "activity-log", "list"),
    ("monitor", "log-analytics", "query"),
    ("costmanagement", "query"),
    ("graph", "query"),
    ("aks", "get-versions"),
    ("aks", "get-upgrades"),
    ("aks", "nodepool", "get-upgrades"),
}
# Reveal secret material or credentials: never, in any mode.
DENIED = [
    ("keyvault", "secret", "show"),
    ("keyvault", "secret", "download"),
    ("keyvault", "secret", "backup"),
    ("keyvault", "key", "download"),
    ("keyvault", "key", "backup"),
    ("keyvault", "certificate", "download"),
    ("account", "get-access-token"),
    ("aks", "get-credentials"),
    ("aks", "command"),
    ("acr", "login"),
    ("acr", "credential"),
    ("storage", "account", "keys"),
    ("storage", "blob", "download"),
    ("storage", "account", "show-connection-string"),
    ("cosmosdb", "keys"),
    ("redis", "list-keys"),
    ("servicebus", "namespace", "authorization-rule", "keys"),
    ("eventhubs", "namespace", "authorization-rule", "keys"),
    ("rest",),
]
DENIED_FLAGS = {"--admin", "--show-secrets", "--query-examples"}


def _command_words(args: list[str]) -> list[str]:
    words = []
    for arg in args:
        if arg.startswith("-"):
            break
        words.append(arg.lower())
    return words


def check_az(args: list[str], mode: str) -> None:
    words = _command_words(args)
    if not words:
        raise ToolError("give the az command without 'az', e.g. ['aks', 'show', '-g', 'rg', '-n', 'name']")
    for denied in DENIED:
        if tuple(words[: len(denied)]) == denied:
            raise ToolError(f"az {' '.join(denied)} is never allowed for agents (secret material or credentials)")
    if any(a.split("=")[0] in DENIED_FLAGS for a in args):
        raise ToolError(f"flags {sorted(DENIED_FLAGS)} are not allowed")
    if mode == PROTOTYPE:
        return
    if tuple(words) in READ_SPECIAL or READ_VERBS.match(words[-1]):
        return
    raise ToolError(f"az {' '.join(words)} is not a read-only command; change the platform with a pull request")


@tool(
    "azure_cli",
    "Run an Azure CLI command as your read-only identity, e.g. ['aks', 'show', '-g', 'rg-aks-prd', '-n', "
    "'aks-prd-sl-az1'] or ['aks', 'get-versions', '-l', 'swedencentral']. JSON output. The environment's "
    "subscription is used unless you pass --subscription.",
    {"args": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 40}},
)
def azure_cli(ctx: ToolContext, args: dict[str, Any]) -> str:
    argv = list(args["args"])
    if argv and argv[0] == "az":
        argv = argv[1:]
    check_az(argv, ctx.settings.mode)
    env = ctx.platform.environment(ctx.settings.environment)
    if flag_value(argv, "--subscription") is None and _command_words(argv)[0] not in ("graph", "account"):
        argv += ["--subscription", env["subscription"]]
    if flag_value(argv, "--output", "-o") is None:
        argv += ["--output", "json"]
    return execute(ctx, ["az", *argv, "--only-show-errors"])


@tool(
    "azure_resource_graph",
    "Query Azure Resource Graph (KQL) over the environment's subscription, e.g. \"resources | where type =~ "
    "'microsoft.containerservice/managedclusters' | project name, properties.kubernetesVersion\".",
    {"query": {"type": "string"}},
)
def azure_resource_graph(ctx: ToolContext, args: dict[str, Any]) -> str:
    env = ctx.platform.environment(ctx.settings.environment)
    subscriptions = ctx.options("azure_resource_graph").get("subscriptions") or [env["subscription"]]
    out = execute(
        ctx,
        [
            "az",
            "graph",
            "query",
            "-q",
            args["query"],
            "--subscriptions",
            *subscriptions,
            "--first",
            "1000",
            "--output",
            "json",
            "--only-show-errors",
        ],
    )
    try:
        data = json.loads(out)
        return json.dumps(data.get("data", data), indent=1)
    except json.JSONDecodeError:
        return out


@tool(
    "log_analytics_query",
    "Run a KQL query on the environment's Log Analytics workspace (platform tables only; application logs are "
    "not readable). timespan is ISO 8601, e.g. PT1H or P1D.",
    {"query": {"type": "string"}, "timespan": {"type": "string", "pattern": "^P(T?\\d+[DHMS])+$"}},
)
def log_analytics_query(ctx: ToolContext, args: dict[str, Any]) -> str:
    denied = ctx.options("log_analytics_query").get("deniedTables") or ["AppTraces", "AppRequests", "AppExceptions"]
    for table in denied:
        if re.search(rf"\b{re.escape(table)}\b", args["query"], re.IGNORECASE):
            raise ToolError(f"table {table} holds application data and is not readable by agents")
    workspace = ctx.platform.environment(ctx.settings.environment).get("logAnalyticsWorkspaceId")
    if not workspace:
        raise ToolError("no logAnalyticsWorkspaceId configured for this environment")
    return execute(
        ctx,
        [
            "az",
            "monitor",
            "log-analytics",
            "query",
            "--workspace",
            workspace,
            "--analytics-query",
            args["query"],
            "--timespan",
            args["timespan"],
            "--output",
            "json",
            "--only-show-errors",
        ],
    )

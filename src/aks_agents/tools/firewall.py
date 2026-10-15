"""Egress tools (design D59): the flows the hub firewall denied, and the pods behind their source addresses.

In a cell without an overlay (aks-terraform ``network_plugin_mode = "none"``) every pod keeps an address of its
zone's node subnet up to the hub firewall, so a denied flow's source address names a pod. The firewall's log is read
where the environment keeps it (``firewall.log`` of the environment in ``platform.yaml``):

- ``log_analytics``: Azure Firewall's resource-specific tables (``AZFWApplicationRule``, ``AZFWNetworkRule``) in the
  firewall's workspace - or labfw's ``LabfwFirewallLog_CL`` with the same columns (``firewall.table``), which the
  Azure Monitor Agent on its VM fills - read with Reader on the workspace, nothing else;
- ``file``: JSON lines with the same field names (labfw's ``azfw.jsonl``), exported by the job before the agent
  starts (``AKS_AGENT_FIREWALL_LOG``) - the lab's labfw keeps its log on its VM, which only its operator reads.

Both are read-only. The pods are found with read verbs only (pods, nodes and namespaces - never Secrets)."""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import os
from collections.abc import Iterable
from typing import Any

from .base import ToolContext, ToolError, tool
from .kubernetes import _cluster
from .shell import execute

MAX_FLOWS = 200


def _parse_time(value: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _networks(prefixes: Iterable[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    try:
        return [ipaddress.ip_network(p, strict=False) for p in prefixes]
    except ValueError as err:
        raise ToolError(f"invalid prefix: {err}") from err


def _in(address: str, networks: list[Any]) -> bool:
    if not networks:
        return True
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in n for n in networks)


def summarize_denials(
    records: Iterable[dict[str, Any]],
    since: dt.datetime,
    networks: list[Any],
    limit: int = MAX_FLOWS,
) -> dict[str, Any]:
    """Denied flows grouped by source address and destination, newest first, with counts and first/last seen."""
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    total = 0
    for rec in records:
        if str(rec.get("Action", "")).lower() != "deny":
            continue
        when = _parse_time(str(rec.get("TimeGenerated", "")))
        if when is None or when < since:
            continue
        source = str(rec.get("SourceIp") or "")
        if not _in(source, networks):
            continue
        total += 1
        destination = str(rec.get("Fqdn") or rec.get("DestinationIp") or "")
        port = str(rec.get("DestinationPort") or "")
        protocol = str(rec.get("Protocol") or "")
        key = (source, destination, port, protocol)
        group = groups.setdefault(
            key,
            {
                "sourceIp": source,
                "destination": destination,
                "destinationIsFqdn": bool(rec.get("Fqdn")),
                "destinationPort": port,
                "protocol": protocol,
                "category": rec.get("Category"),
                "rule": rec.get("Policy")
                and "/".join(
                    str(rec.get(k) or "") for k in ("Policy", "RuleCollectionGroup", "RuleCollection", "Rule")
                ),
                "actionReason": rec.get("ActionReason"),
                "count": 0,
                "firstSeen": when,
                "lastSeen": when,
            },
        )
        group["count"] += 1
        group["firstSeen"] = min(group["firstSeen"], when)
        group["lastSeen"] = max(group["lastSeen"], when)
    flows = sorted(groups.values(), key=lambda g: g["lastSeen"], reverse=True)
    for g in flows:
        g["firstSeen"], g["lastSeen"] = g["firstSeen"].isoformat(), g["lastSeen"].isoformat()
    return {
        "since": since.isoformat(),
        "deniedConnections": total,
        "flows": flows[:limit],
        "truncated": max(0, len(flows) - limit),
    }


def _read_file(path: str) -> Iterable[dict[str, Any]]:
    try:
        handle = open(path, encoding="utf-8")  # noqa: SIM115 - closed below
    except OSError as err:
        raise ToolError(f"the firewall log export {path} cannot be read: {err}") from err
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


KQL = """
{source}
| where TimeGenerated > ago({hours}h) and Action == "Deny"
| project TimeGenerated, Category = {category}, Action, SourceIp, Fqdn, DestinationIp, DestinationPort, Protocol,
          Policy, RuleCollectionGroup, RuleCollection, Rule, ActionReason
| take 5000
"""
AZFW_SOURCE = "union isfuzzy=true AZFWApplicationRule, AZFWNetworkRule"


def kql(hours: int, table: str | None) -> str:
    if table:
        if not table.replace("_", "").isalnum():
            raise ToolError(f"invalid firewall.table {table!r}")
        return KQL.format(source=table, category="Category", hours=hours)
    return KQL.format(source=AZFW_SOURCE, category="Type", hours=hours)


def _read_log_analytics(ctx: ToolContext, hours: int) -> list[dict[str, Any]]:
    env = ctx.platform.environment(ctx.settings.environment)
    firewall = env.get("firewall") or {}
    workspace = (
        os.environ.get("AKS_AGENT_FIREWALL_WORKSPACE")
        or firewall.get("workspaceId")
        or env.get("logAnalyticsWorkspaceId")
    )
    if not workspace:
        raise ToolError("no firewall workspace: set firewall.workspaceId or AKS_AGENT_FIREWALL_WORKSPACE")
    out = execute(
        ctx,
        [
            "az",
            "monitor",
            "log-analytics",
            "query",
            "-w",
            workspace,
            "--analytics-query",
            kql(hours, firewall.get("table")),
            "-o",
            "json",
        ],
        timeout=180,
    )
    return json.loads(out or "[]")


@tool(
    "firewall_denied_flows",
    "The connections the hub firewall denied from the environment's cells in the last hours, grouped by source "
    "address and destination (FQDN for application rules, IP otherwise) with counts, first and last seen, and the "
    "rule or default action that denied them. Optionally only sources in the given prefixes.",
    {
        "since_hours": {"type": "integer", "minimum": 1, "maximum": 168},
        "source_prefixes": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
    },
    required=["since_hours"],
)
def firewall_denied_flows(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    env = ctx.platform.environment(ctx.settings.environment)
    log = (env.get("firewall") or {}).get("log", "log_analytics")
    cells = (env.get("firewall") or {}).get("sourcePrefixes") or []
    asked = args.get("source_prefixes") or []
    networks = _networks(asked or cells)
    if asked and cells:
        allowed = _networks(cells)
        if not all(any(n.subnet_of(a) for a in allowed if n.version == a.version) for n in networks):
            raise ToolError(f"source_prefixes must be within the environment's cells {cells}")
    hours = int(args["since_hours"])
    since = dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours)
    if log == "file":
        path = os.environ.get("AKS_AGENT_FIREWALL_LOG") or (env.get("firewall") or {}).get("file")
        if not path:
            raise ToolError("no firewall log export: set AKS_AGENT_FIREWALL_LOG")
        records: Iterable[dict[str, Any]] = _read_file(path)
    elif log == "log_analytics":
        records = _read_log_analytics(ctx, hours)
    else:
        raise ToolError(f"unknown firewall.log {log!r}")
    result = summarize_denials(records, since, networks)
    cells = {c: _networks(prefixes) for c, prefixes in ((env.get("firewall") or {}).get("cells") or {}).items()}
    for flow in result["flows"]:
        flow["cluster"] = next((c for c, nets in cells.items() if _in(flow["sourceIp"], nets)), None)
    result["log"] = log
    result["spoke"] = (env.get("firewall") or {}).get("spoke")
    return result


def _owner(pod: dict[str, Any]) -> dict[str, str] | None:
    refs = pod.get("metadata", {}).get("ownerReferences") or []
    ref = next((r for r in refs if r.get("controller")), refs[0] if refs else None)
    if not ref:
        return None
    kind, name = ref.get("kind", ""), ref.get("name", "")
    # A Deployment's pods belong to a ReplicaSet <deployment>-<pod-template-hash>
    template_hash = pod.get("metadata", {}).get("labels", {}).get("pod-template-hash")
    if kind == "ReplicaSet" and template_hash and name.endswith("-" + template_hash):
        return {"kind": "Deployment", "name": name[: -len(template_hash) - 1]}
    return {"kind": kind, "name": name}


def map_addresses(
    ips: list[str], pods: list[dict[str, Any]], nodes: list[dict[str, Any]], namespaces: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_ns = {ns["metadata"]["name"]: ns["metadata"] for ns in namespaces}
    found: list[dict[str, Any]] = []
    for ip in ips:
        match: dict[str, Any] = {"ip": ip, "kind": "unknown"}
        for pod in pods:
            status = pod.get("status") or {}
            if pod.get("spec", {}).get("hostNetwork"):
                continue
            addresses = {a.get("ip") for a in status.get("podIPs") or []} | {status.get("podIP")}
            if ip in addresses:
                meta = pod["metadata"]
                ns = by_ns.get(meta.get("namespace"), {})
                labels = meta.get("labels") or {}
                match = {
                    "ip": ip,
                    "kind": "pod",
                    "namespace": meta.get("namespace"),
                    "pod": meta.get("name"),
                    "phase": status.get("phase"),
                    "workload": _owner(pod),
                    "app": labels.get("app.kubernetes.io/name") or labels.get("app"),
                    "node": pod.get("spec", {}).get("nodeName"),
                    "zone": (ns.get("labels") or {}).get("platform/zone"),
                    "team": (ns.get("labels") or {}).get("platform/team"),
                    "contact": (ns.get("annotations") or {}).get("platform/contact"),
                    "images": [c.get("image") for c in pod.get("spec", {}).get("containers") or []],
                }
                break
        else:
            for node in nodes:
                if ip in {a.get("address") for a in (node.get("status") or {}).get("addresses") or []}:
                    labels = node["metadata"].get("labels") or {}
                    match = {
                        "ip": ip,
                        "kind": "node",
                        "node": node["metadata"]["name"],
                        "zone": labels.get("platform/zone"),
                        "note": "the node's own address: a host-network pod, or a pod address translated on the "
                        "node - the firewall cannot tell the application",
                    }
                    break
        found.append(match)
    return found


def _items(ctx: ToolContext, cluster: str, kind: str) -> list[dict[str, Any]]:
    args = ["kubectl", "--context", cluster, "--request-timeout=60s", "get", kind, "-o", "json"]
    if kind == "pods":
        args.append("-A")
    return json.loads(execute(ctx, args, timeout=120)).get("items", [])


@tool(
    "pods_by_ip",
    "Which pod (namespace, workload, app, zone, owning team and contact) or node has each address in a cluster "
    "now - for the source addresses of denied flows. A pod that ended since the flow is not found.",
    {
        "cluster": {"type": "string"},
        "ips": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 100},
    },
)
def pods_by_ip(ctx: ToolContext, args: dict[str, Any]) -> list[dict[str, Any]]:
    cluster = _cluster(ctx, args["cluster"])
    for ip in args["ips"]:
        try:
            ipaddress.ip_address(ip)
        except ValueError as err:
            raise ToolError(f"not an IP address: {ip}") from err
    return map_addresses(
        list(args["ips"]),
        _items(ctx, cluster, "pods"),
        _items(ctx, cluster, "nodes"),
        _items(ctx, cluster, "namespaces"),
    )


@tool(
    "application_source",
    "Where a namespace's applications come from: its Flux Kustomizations (path, the Git repository URL and branch, "
    "applied revision) and which granted repository key that is, so you can repo_list / repo_read the right files "
    "(start with <path>/kustomization.yaml).",
    {"cluster": {"type": "string"}, "namespace": {"type": "string"}},
)
def application_source(ctx: ToolContext, args: dict[str, Any]) -> list[dict[str, Any]]:
    cluster = _cluster(ctx, args["cluster"])
    namespace = args["namespace"]
    if not namespace.replace("-", "").isalnum():
        raise ToolError(f"not a namespace name: {namespace}")

    def get(kind: str) -> list[dict[str, Any]]:
        out = execute(
            ctx, ["kubectl", "--context", cluster, "--request-timeout=60s", "get", kind, "-n", namespace, "-o", "json"]
        )
        return json.loads(out).get("items", [])

    repos = {r["metadata"]["name"]: r.get("spec", {}) for r in get("gitrepositories.source.toolkit.fluxcd.io")}
    keys = {str(v).lower(): k for k, v in ((ctx.platform.get("git.repositories") or {}).items()) if isinstance(v, str)}
    sources = []
    for k in get("kustomizations.kustomize.toolkit.fluxcd.io"):
        spec = k.get("spec", {})
        repo = repos.get((spec.get("sourceRef") or {}).get("name"), {})
        url = repo.get("url", "")
        slug = url.lower().removeprefix("https://github.com/").removesuffix(".git").strip("/")
        path = (spec.get("path") or ".").removeprefix("./").strip("/") or "."
        sources.append(
            {
                "kustomization": k["metadata"]["name"],
                "path": path,
                "repositoryUrl": url,
                "branch": (repo.get("ref") or {}).get("branch"),
                "appliedRevision": (k.get("status") or {}).get("lastAppliedRevision"),
                "repositoryKey": keys.get(slug),
                "start": f"repo_read repo={keys.get(slug)} path={path}/kustomization.yaml" if keys.get(slug) else None,
            }
        )
    return sources

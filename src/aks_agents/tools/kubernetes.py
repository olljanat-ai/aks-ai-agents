"""Kubernetes tools for the clusters of the run's environment.

The job prepares a kubeconfig with one context per cluster (``az aks get-credentials`` + ``kubelogin`` with workload
identity, run before the agent starts). In guarded mode only read verbs run, on top of the ``Azure Kubernetes Service
RBAC Reader`` role; ``Secret`` objects are never read, and logs only from platform namespaces, in any mode."""

from __future__ import annotations

import json
from typing import Any

from ..settings import PROTOTYPE
from .base import ToolContext, ToolError, tool
from .shell import execute, flag_value

READ_VERBS = {"get", "describe", "top", "events", "api-resources", "api-versions", "version", "explain", "logs", "auth"}
FORBIDDEN_FLAGS = (
    "--kubeconfig",
    "--context",
    "--cluster",
    "--user",
    "--token",
    "--as",
    "--as-group",
    "--server",
    "-s",
    "--raw",
)
SECRET_NAMES = {"secret", "secrets", "sealedsecret", "sealedsecrets"}


def _cluster(ctx: ToolContext, name: str) -> str:
    clusters = ctx.platform.environment(ctx.settings.environment).get("clusters") or []
    if name not in clusters:
        raise ToolError(f"cluster must be one of {clusters} in {ctx.settings.environment}")
    return name


def check_kubectl(args: list[str], mode: str, platform_namespaces: list[str]) -> None:
    if not args:
        raise ToolError("give kubectl arguments, e.g. ['get', 'pods', '-n', 'flux-system']")
    verb = args[0]
    if any(a.split("=")[0] in FORBIDDEN_FLAGS for a in args):
        raise ToolError(f"flags {FORBIDDEN_FLAGS} are set by the tool and cannot be given")
    # Any positional argument naming a Secret type (also "pods,secrets" and "secret/name"); a false positive on an
    # object that happens to be called "secrets" is acceptable.
    resources = {
        part.split("/")[0].split(".")[0].lower() for a in args[1:] if not a.startswith("-") for part in a.split(",")
    }
    if resources & SECRET_NAMES or (verb == "auth" and len(args) > 1 and args[1] not in ("can-i", "whoami")):
        raise ToolError("Secrets are never read by agents")
    if verb == "logs":
        namespace = flag_value(args, "-n", "--namespace")
        if namespace not in platform_namespaces:
            raise ToolError(
                f"logs only from platform namespaces {platform_namespaces}; application logs may hold personal data"
            )
    if mode == PROTOTYPE:
        return
    if verb not in READ_VERBS:
        raise ToolError(f"kubectl {verb} is not read-only; change clusters through the fleet repository")


@tool(
    "kubectl",
    "Run kubectl against one cluster of the environment, read-only, e.g. cluster 'aks-prd-sl-az1', args "
    "['get', 'nodes', '-o', 'wide'] or ['get', 'events', '-A', '--field-selector', 'type=Warning'].",
    {"cluster": {"type": "string"}, "args": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
)
def kubectl(ctx: ToolContext, args: dict[str, Any]) -> str:
    cluster = _cluster(ctx, args["cluster"])
    argv = list(args["args"])
    if argv and argv[0] == "kubectl":
        argv = argv[1:]
    check_kubectl(argv, ctx.settings.mode, ctx.platform.get("platformNamespaces") or [])
    if argv[0] == "logs" and not any(a.startswith("--tail") or a.startswith("--since") for a in argv):
        argv.append("--tail=500")
    return execute(ctx, ["kubectl", "--context", cluster, "--request-timeout=60s", *argv], timeout=120)


FLUX_KINDS = ",".join(
    [
        "kustomizations.kustomize.toolkit.fluxcd.io",
        "helmreleases.helm.toolkit.fluxcd.io",
        "ocirepositories.source.toolkit.fluxcd.io",
    ]
)


@tool(
    "flux_status",
    "Readiness of every Flux Kustomization, HelmRelease and OCIRepository in a cluster, with the artifact revision "
    "and the message of anything not Ready.",
    {"cluster": {"type": "string"}},
)
def flux_status(ctx: ToolContext, args: dict[str, Any]) -> list[dict[str, Any]]:
    cluster = _cluster(ctx, args["cluster"])
    out = execute(
        ctx, ["kubectl", "--context", cluster, "--request-timeout=60s", "get", FLUX_KINDS, "-A", "-o", "json"]
    )
    items = json.loads(out).get("items", [])
    status = []
    for item in items:
        ready = next((c for c in (item.get("status") or {}).get("conditions", []) if c.get("type") == "Ready"), {})
        status.append(
            {
                "kind": item["kind"],
                "namespace": item["metadata"].get("namespace"),
                "name": item["metadata"]["name"],
                "ready": ready.get("status"),
                "reason": ready.get("reason"),
                "message": (ready.get("message") or "")[:300] if ready.get("status") != "True" else "",
                "revision": (item.get("status") or {}).get("lastAppliedRevision")
                or ((item.get("status") or {}).get("artifact") or {}).get("revision"),
                "suspended": bool((item.get("spec") or {}).get("suspend")),
            }
        )
    return status

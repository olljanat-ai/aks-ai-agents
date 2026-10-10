"""Evaluation of a :class:`~aks_gate.model.PlanSet` against ``tiers.yaml`` and ``limits.yaml``.

Every rule produces :class:`Finding` objects. A finding either raises the tier of the pull request or – when it is a
*violation* – rejects the pull request outright, whatever approvals it has. Declarative rules from ``tiers.yaml`` are
applied first; the semantic rules below then explain changes the declarative rules cannot (versions, policy effects,
image promotion, limits). A change no rule explains gets the default tier of its kind.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .config import GateConfig, Rule, path_matches
from .manifests import container_images, without_images
from .model import Action, ManifestChange, PlanSet, ResourceChange, Tier

ENV_ORDER = ["dev", "acc", "prd"]

# Well-known built-in role definition IDs, so that role assignments by ID can be checked by name.
BUILTIN_ROLES = {
    "8e3af657-a8ff-443c-a75c-2fe8c4bcb635": "Owner",
    "b24988ac-6180-42a0-ab88-20f7382dd24c": "Contributor",
    "acdd72a7-3385-48ef-bd42-f606fba81ae7": "Reader",
    "18d7d88d-d35e-4fb5-a5c3-7773c20a72d9": "User Access Administrator",
    "f58310d2-a9f6-439a-9e8d-f62e7b41a168": "Role Based Access Control Administrator",
    "00482a5a-887f-4fb3-b363-3b7fe8e74483": "Key Vault Administrator",
    "4633458b-17de-408a-b874-0445c86b69e6": "Key Vault Secrets User",
    "b86a8fe4-44ce-4948-aee5-eccb2c155cd7": "Key Vault Secrets Officer",
    "7f951dda-4ed3-4680-a7ca-43fe172d538d": "AcrPull",
    "8311e382-0749-4cb8-b61a-304f252e45ec": "AcrPush",
    "b1ff04bb-8a4e-4dc4-8eb5-8693973ce19b": "Azure Kubernetes Service RBAC Cluster Admin",
    "3498e952-d568-435e-9b2c-8d77e338d7f7": "Azure Kubernetes Service RBAC Admin",
    "a7ffa36f-339b-4b5c-8bdf-e2c188b2c0eb": "Azure Kubernetes Service RBAC Writer",
    "4abbcc35-e782-43d8-92c5-2d3f1bd2253f": "Azure Kubernetes Service Cluster User Role",
    "db79e9a7-68ee-4b58-9aeb-b90e7c24fcba": "Key Vault Certificate User",
    "e147488a-f6f5-4113-8e2d-b22465e65bf6": "Key Vault Crypto Service Encryption User",
    "12338af0-0e69-4776-bea7-57ae8d297424": "Key Vault Crypto User",
    "21090545-7ca7-4776-b22c-e363652d74d2": "Key Vault Reader",
    "2a2b9908-6ea1-4ae2-8e65-a410df84e7d1": "Storage Blob Data Reader",
    "ba92f5b4-2d11-453d-a403-e96b0029c9fe": "Storage Blob Data Contributor",
    "19e7f393-937e-4f77-808e-94535e297925": "Storage Queue Data Reader",
    "974c5e8b-45b9-4653-ba55-5f855dd0fb88": "Storage Queue Data Contributor",
    "69a216fc-b8fb-44d8-bc22-1f3c2cd27a39": "Azure Service Bus Data Sender",
    "4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0": "Azure Service Bus Data Receiver",
    "2b629674-e913-4c01-ae53-ef4638d8f975": "Azure Event Hubs Data Sender",
    "a638d3c7-ab3a-418d-83e6-5f17a39d4fde": "Azure Event Hubs Data Receiver",
    "3913510d-42f4-4e42-8a64-420c390055eb": "Monitoring Metrics Publisher",
    "43d0d8ad-25c7-4714-9337-8ba259a9fe05": "Monitoring Reader",
    "4d97b98b-1d4f-4787-a291-c67834d212e7": "Network Contributor",
    "f1a07417-d97a-45cb-824c-7a7467783830": "Managed Identity Operator",
}

VERSION_KEY_RE = re.compile(r"(^|\.)(kubernetes_version|orchestrator_version|kubernetesVersion|orchestratorVersion)$")
ANY_ADDRESS = {"*", "0.0.0.0/0", "::/0", "internet", "any"}


@dataclass
class Finding:
    rule: str
    message: str
    subject: str = ""
    tier: Tier | None = None
    violation: bool = False

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"rule": self.rule, "message": self.message, "subject": self.subject}
        if self.tier is not None:
            out["tier"] = str(self.tier)
        if self.violation:
            out["violation"] = True
        return out


@dataclass
class Context:
    plan: PlanSet
    config: GateConfig
    agent: str | None  # agent name for agent pull requests, None for humans

    @property
    def is_agent(self) -> bool:
        return self.agent is not None

    def restricted(self, rule: str, message: str, subject: str) -> Finding:
        """Something agents may never do: rejected for agents, tier high for humans."""
        if self.is_agent:
            return Finding(rule, message, subject, violation=True)
        return Finding(rule, message, subject, tier=Tier.HIGH)


# --------------------------------------------------------------------------------------------- helpers


def changed_paths(before: Any, after: Any, prefix: str = "") -> list[str]:
    """Dotted paths of the leaves that differ. Unknown values (``None`` after) are ignored."""
    if after is None:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        paths = []
        for key in sorted(set(before) | set(after)):
            paths += changed_paths(before.get(key), after.get(key), f"{prefix}{key}.")
        return paths
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        paths = []
        for i, (b, a) in enumerate(zip(before, after, strict=True)):
            paths += changed_paths(b, a, f"{prefix}{i}.")
        return paths
    return [] if before == after else [prefix.rstrip(".")]


def minor(version: str | None) -> str | None:
    if not version:
        return None
    parts = str(version).lstrip("v").split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else None


def walk_dicts(node: Any, _is_properties: bool = False) -> Iterable[dict[str, Any]]:
    """Every dict in ``node``; a Bicep ``properties`` dict is not yielded on its own, because
    :func:`norm_keys` already flattens it into its parent."""
    if isinstance(node, dict):
        if not _is_properties:
            yield node
        for key, value in node.items():
            yield from walk_dicts(value, key == "properties" and isinstance(value, dict))
    elif isinstance(node, list):
        for item in node:
            yield from walk_dicts(item)


def norm_keys(obj: dict[str, Any]) -> dict[str, Any]:
    """Lower-case keys without underscores, flattening a Bicep ``properties`` level."""
    flat = dict(obj)
    if isinstance(obj.get("properties"), dict):
        flat.update(obj["properties"])
    return {k.replace("_", "").lower(): v for k, v in flat.items()}


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def cell_of(rc: ResourceChange, pattern: str) -> str | None:
    regex = re.compile(pattern)
    for candidate in (rc.name, str(rc.attr("kubernetes_cluster_id") or ""), rc.address):
        match = regex.search(candidate or "")
        if match:
            return match.group(0)
    return None


def _rule_matches_resource(rule: Rule, rc: ResourceChange) -> bool:
    return (not rule.types or rc.type.lower() in rule.types) and (not rule.actions or rc.action in rule.actions)


def _rule_matches_manifest(rule: Rule, mc: ManifestChange) -> bool:
    return (
        (not rule.kinds or mc.kind in rule.kinds)
        and (not rule.actions or mc.action in rule.actions)
        and (not rule.namespaces or mc.namespace in rule.namespaces)
    )


# --------------------------------------------------------------------------------------------- resources


def _versions(rc: ResourceChange, side: str) -> list[str]:
    obj = (rc.after if side == "after" else rc.before) or {}
    found = []
    for d in walk_dicts(obj):
        for key, value in norm_keys(d).items():
            if key in ("kubernetesversion", "orchestratorversion") and isinstance(value, str) and value:
                found.append(value)
    return found


def check_cluster_versions(ctx: Context, rc: ResourceChange) -> tuple[list[Finding], bool]:
    """Kubernetes version changes of clusters and node pools. Returns findings and whether the change is
    fully explained (nothing but versions changed)."""
    if rc.type not in (
        "Microsoft.ContainerService/managedClusters",
        "Microsoft.ContainerService/managedClusters/agentPools",
    ) or rc.action not in (Action.UPDATE, Action.CREATE):
        return [], False
    before, after = set(_versions(rc, "before")), set(_versions(rc, "after"))
    if rc.action == Action.UPDATE and before == after:
        return [], False
    limits = ctx.config.limits
    stateful_re = re.compile(limits.get("kubernetes.statefulClusterPattern", r"-sf$"))
    cluster_name = rc.name if rc.type.endswith("managedClusters") else str(rc.attr("kubernetes_cluster_id") or "")
    stateful = bool(stateful_re.search(cluster_name.rsplit("/", 1)[-1])) or bool(re.search(r"-sf($|/)", rc.address))
    cluster_type = "stateful" if stateful else "stateless"
    allowed = limits.get(f"kubernetes.allowedMinors.{cluster_type}", []) or []
    findings: list[Finding] = []
    for version in sorted(after):
        if allowed and minor(version) not in allowed:
            findings.append(
                Finding(
                    "kubernetes-allowed-minors",
                    f"{version} is not an allowed {cluster_type} minor {allowed}",
                    rc.address,
                    violation=True,
                )
            )
    if rc.action == Action.CREATE:
        return findings, False
    minor_change = {minor(v) for v in before} != {minor(v) for v in after}
    if stateful:
        tier, what = Tier.HIGH, "stateful cluster version change"
    elif minor_change:
        tier, what = Tier.MEDIUM, "Kubernetes minor upgrade of a stateless cell"
    else:
        tier, what = Tier.LOW, "Kubernetes patch upgrade of a stateless cell"
    findings.append(Finding("kubernetes-version", f"{what}: {sorted(before)} → {sorted(after)}", rc.address, tier))
    other = [p for p in changed_paths(rc.before, rc.after) if not VERSION_KEY_RE.search(p)]
    return findings, not other


def check_policy_assignment(ctx: Context, rc: ResourceChange) -> tuple[list[Finding], bool]:
    if rc.type != "Microsoft.Authorization/policyAssignments":
        return [], False

    def enforced(side: str) -> bool | None:
        obj = rc.after if side == "after" else rc.before
        if not obj:
            return None
        if "enforce" in obj:
            return bool(obj["enforce"])
        mode = rc.attr("properties.enforcementMode", "enforcementMode", side=side)
        return None if mode is None else str(mode).lower() == "default"

    before, after = enforced("before"), enforced("after")
    if rc.action == Action.DELETE or (before is True and after is False):
        return [ctx.restricted("policy-disable", "disables an Azure Policy assignment", rc.address)], True
    if before is False and after is True:
        return [Finding("policy-enforce", "Azure Policy assignment audit → deny", rc.address, Tier.MEDIUM)], True
    return [Finding("policy-assignment", "Azure Policy assignment change", rc.address, Tier.MEDIUM)], True


def role_name(rc: ResourceChange) -> str:
    name = rc.attr("role_definition_name")
    if name:
        return str(name)
    role_id = str(rc.attr("role_definition_id", "properties.roleDefinitionId", "roleDefinitionId") or "")
    guid = role_id.rstrip("/").rsplit("/", 1)[-1].lower()
    return BUILTIN_ROLES.get(guid, role_id)


def scope_level(scope: str) -> str:
    s = scope.rstrip("/").lower()
    if "/providers/microsoft.management/managementgroups/" in s and "/subscriptions/" not in s:
        return "managementGroup"
    if s in ("", "/"):
        return "tenant"
    if re.fullmatch(r"/subscriptions/[^/]+", s):
        return "subscription"
    if re.fullmatch(r"/subscriptions/[^/]+/resourcegroups/[^/]+", s):
        return "resourceGroup"
    return "resource"


SCOPE_ORDER = ["resource", "resourceGroup", "subscription", "managementGroup", "tenant"]


def check_role_assignment(ctx: Context, rc: ResourceChange) -> list[Finding]:
    if rc.type != "Microsoft.Authorization/roleAssignments" or rc.action == Action.DELETE:
        return []
    findings = []
    limits = ctx.config.limits
    role = role_name(rc)
    # Whitelist: only roles listed in limits.yaml (by name or role definition GUID). A role the plan does not show
    # (unknown until apply) or a custom role that is not listed is treated as not allowed.
    allowed = {str(r).lower() for r in limits.get("roleAssignments.allowedRoles") or []}
    role_id = str(rc.attr("role_definition_id", "properties.roleDefinitionId", "roleDefinitionId") or "")
    guid = role_id.rstrip("/").rsplit("/", 1)[-1].lower()
    if not role:
        findings.append(
            ctx.restricted("role-not-allowed", "role assignment whose role is not known from the plan", rc.address)
        )
    elif role.lower() not in allowed and (not guid or guid not in allowed):
        findings.append(ctx.restricted("role-not-allowed", f"{role} is not an allowed role", rc.address))
    scope = rc.attr("scope", "properties.scope")
    if scope is None and rc.source == "bicep":
        scope = rc.address.lower().split("/providers/microsoft.authorization/roleassignments/")[0]
    if scope is not None:
        level = scope_level(str(scope))
        max_scope = limits.get("roleAssignments.maxScope", "resourceGroup")
        if SCOPE_ORDER.index(level) > SCOPE_ORDER.index(max_scope):
            findings.append(
                Finding(
                    "role-assignment-scope",
                    f"role assignment of {role} at {level} scope is wider than a zone ({max_scope})",
                    rc.address,
                    violation=True,
                )
            )
    return findings


def check_agent_identity(ctx: Context, rc: ResourceChange) -> list[Finding]:
    pattern = ctx.config.limits.get("agentIdentityPattern")
    if not pattern or not rc.type.startswith("Microsoft.ManagedIdentity/"):
        return []
    parent = rc.address if rc.source == "bicep" else str(rc.attr("parent_id") or "")
    names = [rc.name, parent.rstrip("/").rsplit("/userAssignedIdentities/", 1)[-1].split("/")[0]]
    if any(re.search(pattern, n or "") for n in names):
        return [ctx.restricted("agent-identity", "changes an AI agent identity", rc.address)]
    return []


def check_forbidden(ctx: Context, rc: ResourceChange) -> list[Finding]:
    findings = []
    for entry in ctx.config.limits.get("forbidden") or []:
        types = [t.lower() for t in entry.get("types", [])]
        if rc.type.lower() not in types:
            continue
        action = entry.get("action")
        if action == "delete" and rc.is_destructive:
            verb = "replaces (deletes)" if rc.action == Action.REPLACE else "deletes"
            findings.append(ctx.restricted("forbidden-delete", f"{verb} a {rc.type}", rc.address))
    return findings


def check_address_space(ctx: Context, rc: ResourceChange) -> list[Finding]:
    if rc.type not in ("Microsoft.Network/virtualNetworks", "Microsoft.Network/virtualNetworks/subnets"):
        return []
    allowed = [ipaddress.ip_network(n) for n in ctx.config.limits.get("network.allowedAddressSpaces") or []]
    if not allowed or rc.action == Action.DELETE:
        return []
    prefixes: list[str] = []
    for d in walk_dicts(rc.after or {}):
        nk = norm_keys(d)
        for key in ("addressprefixes", "addressprefix", "addressspace"):
            value = nk.get(key)
            if isinstance(value, dict):
                value = value.get("addressPrefixes")
            prefixes += [p for p in as_list(value) if isinstance(p, str)]
    findings = []
    for prefix in sorted(set(prefixes)):
        try:
            net = ipaddress.ip_network(prefix, strict=False)
        except ValueError:
            continue
        if not any(net.version == a.version and net.subnet_of(a) for a in allowed):
            findings.append(
                Finding("address-plan", f"{prefix} is outside the IPAM allocation", rc.address, violation=True)
            )
    return findings


def check_open_rules(ctx: Context, rc: ResourceChange) -> list[Finding]:
    """Firewall rules to any destination, NSG rules that allow inbound traffic from anywhere."""
    if not rc.type.startswith(("Microsoft.Network/firewallPolicies", "Microsoft.Network/networkSecurityGroups")):
        return []
    findings = []
    for d in walk_dicts(rc.after or {}):
        nk = norm_keys(d)
        for key in ("destinationaddresses", "destinationfqdns", "targetfqdns", "destinationipgroups"):
            if any(str(v).lower() in ANY_ADDRESS for v in as_list(nk.get(key))):
                findings.append(
                    Finding(
                        "firewall-any",
                        f"Firewall rule {d.get('name', '')} to any destination",
                        rc.address,
                        violation=True,
                    )
                )
        if str(nk.get("access", "")).lower() == "allow" and str(nk.get("direction", "")).lower() == "inbound":
            sources = as_list(nk.get("sourceaddressprefix")) + as_list(nk.get("sourceaddressprefixes"))
            if any(str(v).lower() in ANY_ADDRESS for v in sources):
                findings.append(
                    Finding(
                        "nsg-any-inbound",
                        f"NSG rule {d.get('name', '')} allows inbound from anywhere",
                        rc.address,
                        violation=True,
                    )
                )
    return findings


def check_node_pools(ctx: Context, rc: ResourceChange) -> list[Finding]:
    if (
        rc.type
        not in (
            "Microsoft.ContainerService/managedClusters",
            "Microsoft.ContainerService/managedClusters/agentPools",
        )
        or rc.action == Action.DELETE
    ):
        return []
    limits = ctx.config.limits
    allowed_sizes = limits.env(rc.environment, "nodePools.allowedVmSizes") or []
    soft = limits.env(rc.environment, "nodePools.maxNodesPerPool.soft")
    hard = limits.env(rc.environment, "nodePools.maxNodesPerPool.hard")
    findings = []
    pools = []
    for d in walk_dicts(rc.after or {}):
        nk = norm_keys(d)
        if "vmsize" in nk:
            pools.append((d.get("name", rc.name), nk))
    for name, pool in pools:
        size = pool.get("vmsize")
        if allowed_sizes and size and size not in allowed_sizes:
            findings.append(
                Finding(
                    "vm-size",
                    f"node pool {name}: {size} is not an allowed VM size",
                    rc.address,
                    violation=True,
                )
            )
        counts = [pool.get(k) for k in ("nodecount", "count", "maxcount") if isinstance(pool.get(k), int)]
        if counts:
            top = max(counts)
            if hard is not None and top > hard:
                findings.append(
                    Finding(
                        "node-count",
                        f"node pool {name}: {top} nodes over the hard limit {hard}",
                        rc.address,
                        violation=True,
                    )
                )
            elif soft is not None and top > soft:
                findings.append(
                    Finding(
                        "node-count",
                        f"node pool {name}: {top} nodes over the soft limit {soft}",
                        rc.address,
                        Tier.MEDIUM,
                    )
                )
    return findings


# --------------------------------------------------------------------------------------------- manifests


def _cpu(value: Any) -> float | None:
    if value is None:
        return None
    s = str(value)
    try:
        return float(s[:-1]) / 1000 if s.endswith("m") else float(s)
    except ValueError:
        return None


def check_nap_node_pools(ctx: Context) -> list[Finding]:
    """NAP ``NodePool`` limits per zone (on-demand + spot), spot share and VM size requirements."""
    limits = ctx.config.limits
    findings: list[Finding] = []
    changed = {m.key for m in ctx.plan.effective_manifests if m.kind == "NodePool" and m.after}
    if not changed:
        return findings
    # Group every NodePool after the change by environment, cluster and zone label.
    zones: dict[tuple[str, str, str], dict[str, float]] = {}
    for m in ctx.plan.effective_manifests:
        if m.kind != "NodePool" or not m.after:
            continue
        spec = m.after.get("spec") or {}
        labels = ((spec.get("template") or {}).get("metadata") or {}).get("labels") or {}
        zone = labels.get("platform/zone") or m.name.removesuffix("spot")
        requirements = ((spec.get("template") or {}).get("spec") or {}).get("requirements") or []
        spot = any(
            r.get("key") == "karpenter.sh/capacity-type" and r.get("values") == ["spot"] for r in requirements
        ) or m.name.endswith("spot")
        cpu = _cpu((spec.get("limits") or {}).get("cpu"))
        if cpu is not None:
            bucket = zones.setdefault((m.environment, m.cluster, zone), {"spot": 0.0, "total": 0.0})
            bucket["total"] += cpu
            if spot:
                bucket["spot"] += cpu
        allowed = limits.env(m.environment, "nodePools.allowedVmSizes") or []
        for r in requirements:
            if r.get("key") in ("node.kubernetes.io/instance-type", "karpenter.azure.com/sku-name") and allowed:
                bad = [v for v in r.get("values") or [] if v not in allowed]
                if bad:
                    findings.append(
                        Finding("vm-size", f"NodePool {m.name}: VM sizes {bad} not allowed", m.key, violation=True)
                    )
    for (env, cluster, zone), bucket in sorted(zones.items()):
        subject = f"{env}/{cluster}/{zone}"
        soft = limits.env(env, "nodePools.maxCpuPerZone.soft")
        hard = limits.env(env, "nodePools.maxCpuPerZone.hard")
        if hard is not None and bucket["total"] > hard:
            findings.append(
                Finding("zone-cpu", f"{bucket['total']:g} CPU over the hard limit {hard}", subject, violation=True)
            )
        elif soft is not None and bucket["total"] > soft:
            findings.append(
                Finding("zone-cpu", f"{bucket['total']:g} CPU over the soft limit {soft}", subject, Tier.MEDIUM)
            )
        max_spot = limits.env(env, "nodePools.maxSpotShare")
        if max_spot is not None and bucket["total"] and bucket["spot"] / bucket["total"] > max_spot:
            findings.append(
                Finding(
                    "spot-share",
                    f"spot share {bucket['spot'] / bucket['total']:.0%} over {max_spot:.0%}",
                    subject,
                    violation=True,
                )
            )
    return findings


def _previous_env(env: str) -> str | None:
    if env in ENV_ORDER and ENV_ORDER.index(env) > 0:
        return ENV_ORDER[ENV_ORDER.index(env) - 1]
    return None


def check_image_promotion(ctx: Context, mc: ManifestChange) -> tuple[list[Finding], bool]:
    """An update that changes nothing but container images: low when every new image is a digest that already
    runs in the previous environment (a promotion), medium otherwise."""
    if mc.action != Action.UPDATE or without_images(mc.before) != without_images(mc.after):
        return [], False
    new_images = sorted(set(container_images(mc.after)) - set(container_images(mc.before)))
    if not new_images:
        return [], False
    undigested = [i for i in new_images if "@sha256:" not in i]
    if undigested:
        return [Finding("image-tag", f"images not pinned by digest: {undigested}", mc.key, Tier.MEDIUM)], True
    prev = _previous_env(mc.environment)
    if prev is None:
        return [Finding("image-update", f"image update in {mc.environment}", mc.key, Tier.LOW)], True
    digests_prev = {
        i.split("@", 1)[1] for o in ctx.plan.reference_manifests.get(prev, []) for i in container_images(o) if "@" in i
    }
    missing = [i for i in new_images if i.split("@", 1)[1] not in digests_prev]
    if missing:
        return [Finding("image-promotion", f"digests not running in {prev}: {missing}", mc.key, Tier.MEDIUM)], True
    return [Finding("image-promotion", f"promotion of digests from {prev}", mc.key, Tier.LOW)], True


# --------------------------------------------------------------------------------------------- whole PR


def check_cells(ctx: Context) -> list[Finding]:
    """One prd cell per pull request: cluster versions in the plan and cell entry points in the fleet repo."""
    limits = ctx.config.limits
    pattern = limits.get("kubernetes.cellPattern", r"(sl-az[0-9]+|sf)")
    findings = []
    for env in ENV_ORDER:
        max_cells = limits.get(f"environments.{env}.maxCellsPerPullRequest")
        if max_cells is None:
            continue
        cells = set()
        for rc in ctx.plan.effective_resources:
            if rc.environment == env and rc.type.startswith("Microsoft.ContainerService/managedClusters"):
                cell = cell_of(rc, pattern)
                if cell:
                    cells.add(cell)
        for path in ctx.plan.changed_files:
            match = re.match(rf"^(?:fleet/)?clusters/{env}/([^/]+)/", path)
            if match:
                cells.add(match.group(1))
        if len(cells) > max_cells:
            findings.append(
                Finding(
                    "cells-per-pull-request",
                    f"changes {len(cells)} {env} cells {sorted(cells)}; at most {max_cells} per pull request",
                    env,
                    violation=True,
                )
            )
    return findings


def check_blast_radius(ctx: Context) -> list[Finding]:
    count = len(ctx.plan.effective_resources)
    soft = ctx.config.limits.get("blastRadius.maxResources.soft")
    hard = ctx.config.limits.get("blastRadius.maxResources.hard")
    if hard is not None and count > hard:
        return [Finding("blast-radius", f"{count} resources change; hard limit {hard}", "", violation=True)]
    if soft is not None and count > soft:
        return [Finding("blast-radius", f"{count} resources change; soft limit {soft}", "", Tier.MEDIUM)]
    return []


def evaluate(plan: PlanSet, config: GateConfig, agent: str | None) -> list[Finding]:
    ctx = Context(plan=plan, config=config, agent=agent)
    tiers = config.tiers
    findings: list[Finding] = []

    repo_name = plan.repository.rsplit("/", 1)[-1]
    if repo_name in tiers.not_allowed_repositories:
        findings.append(ctx.restricted("repository", f"changes in {repo_name} are humans only", plan.repository))

    for rc in plan.effective_resources:
        explained = False
        for rule in tiers.resources:
            if _rule_matches_resource(rule, rc):
                findings.append(Finding(rule.id, f"{rc.action} {rc.type}", rc.address, rule.tier))
                explained = True
        for check in (check_cluster_versions, check_policy_assignment):
            found, fully = check(ctx, rc)
            findings += found
            explained = explained or fully
        for check in (
            check_role_assignment,
            check_agent_identity,
            check_forbidden,
            check_address_space,
            check_open_rules,
            check_node_pools,
        ):
            findings += check(ctx, rc)
        if not explained:
            findings.append(Finding("default-resource", f"{rc.action} {rc.type}", rc.address, tiers.default_resource))

    for mc in plan.effective_manifests:
        explained = False
        for rule in tiers.manifests:
            if _rule_matches_manifest(rule, mc):
                findings.append(Finding(rule.id, f"{mc.action} {mc.kind}", mc.key, rule.tier))
                explained = True
                break
        if not explained:
            found, explained = check_image_promotion(ctx, mc)
            findings += found
        if not explained:
            findings.append(Finding("default-manifest", f"{mc.action} {mc.kind}", mc.key, tiers.default_manifest))
    findings += check_nap_node_pools(ctx)

    has_effect = bool(plan.effective_resources or plan.effective_manifests)
    for path in plan.changed_files:
        matched = False
        for rule in tiers.files:
            if path_matches(path, rule.paths):
                findings.append(Finding(rule.id, "changed file", path, rule.tier))
                matched = True
                break
        if not matched and not has_effect:
            findings.append(Finding("default-file", "changed file without a plan effect", path, tiers.default_file))

    findings += check_cells(ctx)
    findings += check_blast_radius(ctx)
    # Nested Bicep ``properties`` objects are seen twice by the walkers; report each finding once.
    unique: dict[tuple, Finding] = {}
    for f in findings:
        unique.setdefault((f.rule, f.message, f.subject, f.tier, f.violation), f)
    return list(unique.values())

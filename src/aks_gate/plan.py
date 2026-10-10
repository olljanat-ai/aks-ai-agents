"""Parsers that turn tool-specific plans into :class:`~aks_gate.model.ResourceChange` objects.

The gate evaluates the *plan*, never the diff: what Terraform or Bicep will actually do is what decides the tier.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .model import Action, ResourceChange

# azurerm / azuread Terraform types → Azure resource types. Unmapped types keep a ``terraform:`` prefix so
# that they still match the default tier and can be added to the rules by type.
TERRAFORM_TYPES: dict[str, str] = {
    "azurerm_kubernetes_cluster": "Microsoft.ContainerService/managedClusters",
    "azurerm_kubernetes_cluster_node_pool": "Microsoft.ContainerService/managedClusters/agentPools",
    "azurerm_kubernetes_cluster_extension": "Microsoft.KubernetesConfiguration/extensions",
    "azurerm_kubernetes_flux_configuration": "Microsoft.KubernetesConfiguration/fluxConfigurations",
    "azurerm_role_assignment": "Microsoft.Authorization/roleAssignments",
    "azurerm_role_definition": "Microsoft.Authorization/roleDefinitions",
    "azurerm_user_assigned_identity": "Microsoft.ManagedIdentity/userAssignedIdentities",
    "azurerm_federated_identity_credential": (
        "Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials"
    ),
    "azurerm_key_vault": "Microsoft.KeyVault/vaults",
    "azurerm_key_vault_key": "Microsoft.KeyVault/vaults/keys",
    "azurerm_key_vault_secret": "Microsoft.KeyVault/vaults/secrets",
    "azurerm_key_vault_access_policy": "Microsoft.KeyVault/vaults/accessPolicies",
    "azurerm_container_registry": "Microsoft.ContainerRegistry/registries",
    "azurerm_storage_account": "Microsoft.Storage/storageAccounts",
    "azurerm_mssql_server": "Microsoft.Sql/servers",
    "azurerm_mssql_database": "Microsoft.Sql/servers/databases",
    "azurerm_postgresql_flexible_server": "Microsoft.DBforPostgreSQL/flexibleServers",
    "azurerm_cosmosdb_account": "Microsoft.DocumentDB/databaseAccounts",
    "azurerm_redis_cache": "Microsoft.Cache/redis",
    "azurerm_servicebus_namespace": "Microsoft.ServiceBus/namespaces",
    "azurerm_eventhub_namespace": "Microsoft.EventHub/namespaces",
    "azurerm_disk_encryption_set": "Microsoft.Compute/diskEncryptionSets",
    "azurerm_virtual_network": "Microsoft.Network/virtualNetworks",
    "azurerm_subnet": "Microsoft.Network/virtualNetworks/subnets",
    "azurerm_virtual_network_peering": "Microsoft.Network/virtualNetworks/virtualNetworkPeerings",
    "azurerm_network_security_group": "Microsoft.Network/networkSecurityGroups",
    "azurerm_network_security_rule": "Microsoft.Network/networkSecurityGroups/securityRules",
    "azurerm_subnet_network_security_group_association": "Microsoft.Network/virtualNetworks/subnets",
    "azurerm_route_table": "Microsoft.Network/routeTables",
    "azurerm_route": "Microsoft.Network/routeTables/routes",
    "azurerm_subnet_route_table_association": "Microsoft.Network/virtualNetworks/subnets",
    "azurerm_firewall": "Microsoft.Network/azureFirewalls",
    "azurerm_firewall_policy": "Microsoft.Network/firewallPolicies",
    "azurerm_firewall_policy_rule_collection_group": "Microsoft.Network/firewallPolicies/ruleCollectionGroups",
    "azurerm_private_endpoint": "Microsoft.Network/privateEndpoints",
    "azurerm_private_dns_zone": "Microsoft.Network/privateDnsZones",
    "azurerm_public_ip": "Microsoft.Network/publicIPAddresses",
    "azurerm_policy_definition": "Microsoft.Authorization/policyDefinitions",
    "azurerm_policy_set_definition": "Microsoft.Authorization/policySetDefinitions",
    "azurerm_management_group_policy_assignment": "Microsoft.Authorization/policyAssignments",
    "azurerm_subscription_policy_assignment": "Microsoft.Authorization/policyAssignments",
    "azurerm_resource_group_policy_assignment": "Microsoft.Authorization/policyAssignments",
    "azurerm_resource_policy_assignment": "Microsoft.Authorization/policyAssignments",
    "azurerm_management_group_policy_exemption": "Microsoft.Authorization/policyExemptions",
    "azurerm_subscription_policy_exemption": "Microsoft.Authorization/policyExemptions",
    "azurerm_resource_group_policy_exemption": "Microsoft.Authorization/policyExemptions",
    "azurerm_resource_policy_exemption": "Microsoft.Authorization/policyExemptions",
    "azurerm_monitor_metric_alert": "Microsoft.Insights/metricAlerts",
    "azurerm_monitor_scheduled_query_rules_alert_v2": "Microsoft.Insights/scheduledQueryRules",
    "azurerm_monitor_action_group": "Microsoft.Insights/actionGroups",
    "azurerm_monitor_diagnostic_setting": "Microsoft.Insights/diagnosticSettings",
    "azurerm_portal_dashboard": "Microsoft.Portal/dashboards",
    "azurerm_dashboard_grafana": "Microsoft.Dashboard/grafana",
    "azurerm_management_lock": "Microsoft.Authorization/locks",
    "azurerm_resource_group": "Microsoft.Resources/resourceGroups",
    "azurerm_cdn_frontdoor_profile": "Microsoft.Cdn/profiles",
    "azurerm_cdn_frontdoor_origin": "Microsoft.Cdn/profiles/originGroups/origins",
    "azurerm_cdn_frontdoor_firewall_policy": "Microsoft.Network/frontDoorWebApplicationFirewallPolicies",
}


def _tf_action(actions: list[str]) -> Action:
    actions = sorted(actions)
    if actions in (["no-op"], ["read"]):
        return Action.NOOP
    if actions == ["create"]:
        return Action.CREATE
    if actions == ["update"]:
        return Action.UPDATE
    if actions == ["delete"]:
        return Action.DELETE
    if actions == ["create", "delete"]:
        return Action.REPLACE
    raise ValueError(f"unknown Terraform actions {actions}")


def _tf_type(rc: dict[str, Any]) -> str:
    tf_type = rc["type"]
    if tf_type in ("azapi_resource", "azapi_update_resource"):
        value = (rc.get("change", {}).get("after") or rc.get("change", {}).get("before") or {}).get("type")
        if value:
            return value.split("@", 1)[0]
    return TERRAFORM_TYPES.get(tf_type, f"terraform:{tf_type}")


def parse_terraform(plan: dict[str, Any], environment: str) -> list[ResourceChange]:
    """Parse ``terraform show -json <planfile>`` output."""
    changes = []
    for rc in plan.get("resource_changes", []):
        if rc.get("mode", "managed") != "managed":
            continue
        change = rc.get("change", {})
        before, after = change.get("before"), change.get("after")
        changes.append(
            ResourceChange(
                address=rc["address"],
                type=_tf_type(rc),
                name=(after or before or {}).get("name") or rc.get("name", ""),
                action=_tf_action(change.get("actions", ["no-op"])),
                environment=environment,
                before=before,
                after=after,
                source="terraform",
            )
        )
    return changes


_WHATIF_ACTIONS = {
    "Create": Action.CREATE,
    "Delete": Action.DELETE,
    "Modify": Action.UPDATE,
    "Deploy": Action.UPDATE,
    "NoChange": Action.NOOP,
    "Ignore": Action.NOOP,
    "Unsupported": Action.UPDATE,
}


def resource_type_from_id(resource_id: str) -> tuple[str, str]:
    """``(type, name)`` of an Azure resource ID, nested types included (``.../vaults/kv/keys/k``).

    The last ``/providers/`` segment wins, so extension resources (a role assignment on a scope) resolve to
    their own type.
    """
    idx = resource_id.lower().rfind("/providers/")
    if idx < 0:
        if re.search(r"/resourceGroups/[^/]+$", resource_id, re.IGNORECASE):
            return "Microsoft.Resources/resourceGroups", resource_id.rsplit("/", 1)[-1]
        return "unknown", resource_id.rsplit("/", 1)[-1]
    parts = resource_id[idx + len("/providers/") :].split("/")
    namespace, rest = parts[0], parts[1:]
    types, names = rest[0::2], rest[1::2]
    return f"{namespace}/{'/'.join(types)}", (names[-1] if names else "")


def parse_whatif(whatif: dict[str, Any], environment: str) -> list[ResourceChange]:
    """Parse ``az deployment ... what-if --no-pretty-print`` (or ``--output json``) output."""
    changes = []
    for item in whatif.get("changes", []):
        resource_id = item["resourceId"]
        rtype, name = resource_type_from_id(resource_id)
        before, after = item.get("before"), item.get("after")
        action = _WHATIF_ACTIONS.get(item.get("changeType", "Modify"), Action.UPDATE)
        # what-if reports a replacement as a Modify whose delta contains a property marked as requiring one;
        # treat a changed ``name`` / ``location`` / ``zones`` as replace to stay on the safe side.
        if action == Action.UPDATE:
            for delta in item.get("delta") or []:
                if delta.get("path") in ("location", "zones", "name", "kind") and delta.get("propertyChangeType") in (
                    "Modify",
                    "Delete",
                ):
                    action = Action.REPLACE
        changes.append(
            ResourceChange(
                address=resource_id,
                type=rtype,
                name=name,
                action=action,
                environment=environment,
                before=before,
                after=after,
                source="bicep",
            )
        )
    return changes


def load_plan(path: str | Path, environment: str) -> list[ResourceChange]:
    """Detect the plan format from its content and parse it."""
    data = json.loads(Path(path).read_text())
    if "resource_changes" in data or "format_version" in data:
        return parse_terraform(data, environment)
    if "changes" in data:
        return parse_whatif(data, environment)
    if isinstance(data.get("properties"), dict) and "changes" in data["properties"]:
        return parse_whatif(data["properties"], environment)
    raise ValueError(f"{path}: neither a Terraform JSON plan nor a what-if result")

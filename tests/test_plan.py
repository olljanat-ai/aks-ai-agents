import json

from aks_gate.manifests import diff_trees
from aks_gate.model import Action
from aks_gate.plan import load_plan, parse_terraform, parse_whatif, resource_type_from_id


def test_terraform_actions_and_types():
    plan = {
        "format_version": "1.2",
        "resource_changes": [
            {
                "address": "azurerm_key_vault.kv",
                "type": "azurerm_key_vault",
                "name": "kv",
                "mode": "managed",
                "change": {"actions": ["delete"], "before": {"name": "kv-dev"}, "after": None},
            },
            {
                "address": "azurerm_subnet.s",
                "type": "azurerm_subnet",
                "name": "s",
                "mode": "managed",
                "change": {"actions": ["delete", "create"], "before": {}, "after": {"name": "snet"}},
            },
            {
                "address": "azapi_resource.x",
                "type": "azapi_resource",
                "name": "x",
                "mode": "managed",
                "change": {
                    "actions": ["create"],
                    "before": None,
                    "after": {"name": "np", "type": "Microsoft.ContainerService/managedClusters/agentPools@2025-01-01"},
                },
            },
            {
                "address": "data.azurerm_client_config.c",
                "type": "azurerm_client_config",
                "name": "c",
                "mode": "data",
                "change": {"actions": ["read"]},
            },
            {
                "address": "null_resource.n",
                "type": "null_resource",
                "name": "n",
                "mode": "managed",
                "change": {"actions": ["no-op"], "before": {}, "after": {}},
            },
        ],
    }
    changes = parse_terraform(plan, "dev")
    assert [(c.type, c.action) for c in changes] == [
        ("Microsoft.KeyVault/vaults", Action.DELETE),
        ("Microsoft.Network/virtualNetworks/subnets", Action.REPLACE),
        ("Microsoft.ContainerService/managedClusters/agentPools", Action.CREATE),
        ("terraform:null_resource", Action.NOOP),
    ]
    assert changes[0].name == "kv-dev"


def test_resource_type_from_id():
    rid = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.KeyVault/vaults/kv/keys/k1"
    assert resource_type_from_id(rid) == ("Microsoft.KeyVault/vaults/keys", "k1")
    ra = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Authorization/roleAssignments/guid"
    assert resource_type_from_id(ra) == ("Microsoft.Authorization/roleAssignments", "guid")
    assert resource_type_from_id("/subscriptions/s/resourceGroups/rg")[0] == "Microsoft.Resources/resourceGroups"


def test_whatif_and_replace_detection(tmp_path):
    whatif = {
        "changes": [
            {
                "resourceId": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/st",
                "changeType": "Modify",
                "before": {},
                "after": {},
                "delta": [{"path": "location", "propertyChangeType": "Modify"}],
            },
            {
                "resourceId": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Insights/metricAlerts/a",
                "changeType": "Create",
                "after": {},
            },
            {
                "resourceId": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Insights/metricAlerts/b",
                "changeType": "NoChange",
            },
        ]
    }
    changes = parse_whatif(whatif, "prd")
    assert [c.action for c in changes] == [Action.REPLACE, Action.CREATE, Action.NOOP]
    path = tmp_path / "whatif.json"
    path.write_text(json.dumps({"status": "Succeeded", "properties": whatif}))
    assert len(load_plan(path, "prd")) == 3


def test_manifest_diff(tmp_path):
    base, head = tmp_path / "base" / "prd" / "sl-az1", tmp_path / "head" / "prd" / "sl-az1"
    base.mkdir(parents=True)
    head.mkdir(parents=True)
    dep = "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: api, namespace: extfi-shop}\nspec: {replicas: %d}\n"
    (base / "a.yaml").write_text(dep % 2 + "---\napiVersion: v1\nkind: Namespace\nmetadata: {name: old}\n")
    (head / "a.yaml").write_text(dep % 3 + "---\napiVersion: v1\nkind: Namespace\nmetadata: {name: new}\n")
    changes = {(c.kind, c.name): c.action for c in diff_trees(tmp_path / "base", tmp_path / "head")}
    assert changes == {
        ("Deployment", "api"): Action.UPDATE,
        ("Namespace", "old"): Action.DELETE,
        ("Namespace", "new"): Action.CREATE,
    }

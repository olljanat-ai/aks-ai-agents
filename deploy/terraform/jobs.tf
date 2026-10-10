# One Container Apps job per agent and environment. Scheduled agents also get a cron job; event-driven runs (alerts,
# Flux notifications, version pipeline, GitHub events) start the manual job with AKS_AGENT_TASK and
# AKS_AGENT_TRIGGER_JSON overrides (az containerapp job start --env-vars ...).

locals {
  job_env = { for key, p in local.pairs : key => merge(
    {
      AKS_AGENT_NAME  = p.agent
      AKS_AGENT_ENV   = p.env
      AKS_AGENT_MODE  = var.mode
      AZURE_CLIENT_ID = azurerm_user_assigned_identity.agent[key].client_id
    },
    var.git_platform == "github" ? {
      AKS_AGENT_GITHUB_APP_ID          = try(var.github_apps[p.agent].app_id, "")
      AKS_AGENT_GITHUB_INSTALLATION_ID = try(var.github_apps[p.agent].installation_id, "")
      AKS_AGENT_GITHUB_KEY_VAULT       = var.management.key_vault_name
      AKS_AGENT_GITHUB_KEY_SECRET      = try(var.github_apps[p.agent].key_secret_name, "")
    } : {},
  ) }

  scheduled = { for key, p in local.pairs : key => merge(p, var.schedules[p.agent]) if contains(keys(var.schedules), p.agent) }
}

resource "azurerm_container_app_job" "manual" {
  for_each                     = local.pairs
  name                         = substr("job-${each.key}", 0, 32)
  resource_group_name          = var.management.resource_group_name
  location                     = var.management.location
  container_app_environment_id = var.management.container_app_environment_id
  replica_timeout_in_seconds   = 3600
  replica_retry_limit          = 0

  manual_trigger_config {
    parallelism              = 1
    replica_completion_count = 1
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.agent[each.key].id]
  }

  template {
    container {
      name   = "agent"
      image  = var.image
      cpu    = 1
      memory = "2Gi"
      dynamic "env" {
        for_each = merge(local.job_env[each.key], { AKS_AGENT_TASK = "Describe your role and finish." })
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  tags = { agent = each.value.agent, environment = each.value.env }
}

resource "azurerm_container_app_job" "scheduled" {
  for_each                     = local.scheduled
  name                         = substr("cron-${each.key}", 0, 32)
  resource_group_name          = var.management.resource_group_name
  location                     = var.management.location
  container_app_environment_id = var.management.container_app_environment_id
  replica_timeout_in_seconds   = 3600
  replica_retry_limit          = 0

  schedule_trigger_config {
    cron_expression          = each.value.cron
    parallelism              = 1
    replica_completion_count = 1
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.agent[each.key].id]
  }

  template {
    container {
      name   = "agent"
      image  = var.image
      cpu    = 1
      memory = "2Gi"
      dynamic "env" {
        for_each = merge(local.job_env[each.key], { AKS_AGENT_TASK = each.value.task })
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  tags = { agent = each.value.agent, environment = each.value.env }
}

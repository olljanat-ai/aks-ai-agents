output "identities" {
  description = "Client ID of every agent identity, per agent and environment."
  value       = { for key, id in azurerm_user_assigned_identity.agent : key => id.client_id }
}

output "manual_jobs" {
  description = "Container Apps job to start for event-driven runs, per agent and environment."
  value       = { for key, job in azurerm_container_app_job.manual : key => job.id }
}

output "azure_devops_users" {
  description = "Azure DevOps: managed identities to add to the organisation as users (principal ID per agent and environment)."
  value       = var.git_platform == "azure-devops" ? { for key, id in azurerm_user_assigned_identity.agent : id.name => id.principal_id } : {}
}

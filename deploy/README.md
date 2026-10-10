# Deploying the agents

The agents run as **Container Apps jobs in the management VNet** (design section 15), one job per agent and
environment, each with its own user-assigned managed identity and its own GitHub App. Their only egress is the model
endpoint, GitHub and the documentation allow-list, through the hub Firewall.

| Piece | Where | What |
|---|---|---|
| `Dockerfile` | this repository | Image with the agents, the merge gate, `az`, `kubectl`, `kubelogin` |
| `deploy/entrypoint.sh` | image | Managed identity login, GitHub App installation token, design repository, kubeconfig, then `aks-agent run` |
| `deploy/terraform/` | guardrails repository | Identities, role assignments (design table), audit and GitHub App key access, the jobs |
| `guardrails/.github/workflows/merge-gate.yml` | guardrails repository | Required workflow: the merge gate |
| `guardrails/.github/workflows/reviewer-agent.yml` | guardrails repository | Required workflow: starts the reviewer agent for every pull request |

## Roles (guarded mode)

| Agent | Azure | Kubernetes |
|---|---|---|
| All | `Reader` + `Monitoring Reader` on the environment subscription; Log Analytics read on the platform tables only (custom role at table scope) | – |
| Operations, platform, upgrade, release | as all | `Azure Kubernetes Service RBAC Reader` (no Secrets) |
| Capacity and cost | + `Cost Management Reader` | – |
| Security and compliance | + `Security Reader`, `Key Vault Reader` on the zone and platform vaults (metadata only) | `Azure Kubernetes Service RBAC Reader` for the stateful cluster drift report |
| Onboarding, reviewer | as all | – |

Every identity can also read **its own** GitHub App key (secret-scoped `Key Vault Secrets User` on the agents' vault in
the management subscription – never the zone or platform vaults) and append runs to the immutable audit container.
No identity gets a write role, a data-plane role on PaaS or ACR, or `listClusterAdminCredential`.

With `mode = "prototype"` (build tenant, phases 1–2 of section 17 only) every identity also gets `Owner` on the
environment subscription and `Azure Kubernetes Service RBAC Cluster Admin`, and the tools allow writes. Every action
is logged; `aks-agent permissions` turns the logged runs into the least-privilege roles before phase 3.

## Triggers

| Trigger | Starts | How |
|---|---|---|
| Schedule | operations (daily health report), security (daily), capacity (weekly) | `cron-*` jobs |
| Azure Monitor alert, Flux notification, failed pipeline | operations agent | action group / notification → `az containerapp job start` of `job-operations-agent-<env>` with `AKS_AGENT_TRIGGER_JSON` |
| Version pipeline output | upgrade agent | the version pipeline starts `job-upgrade-agent-<env>` with its output as trigger |
| Successful release in an environment | release agent | the release pipeline starts `job-release-agent-<next env>` |
| Pull request | reviewer agent | required workflow `reviewer-agent.yml` |
| Onboarding request issue, task issue | onboarding / platform agent | issue workflow in the IaC repository starts the job |

Starting a job needs only a dispatcher identity with permission to start the agent jobs; the dispatcher cannot change
what a job runs except the task and trigger payload, which the agent treats as data.

## Kill switch

- suspend an agent's GitHub App installation – its pull requests, issues and reviews stop;
- `agents.enabled: false` in `guardrails/limits.yaml` – the merge gate rejects every agent pull request;
- remove the identity's role assignments (or the job) – its Azure reads stop.

The platform keeps running; it is just changed by humans again.

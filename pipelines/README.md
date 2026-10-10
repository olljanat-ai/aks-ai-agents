# Pipeline examples

Examples of the CI pipelines around the agents, for **GitHub Actions** ([`github/`](github)) and **Azure DevOps**
([`azure-devops/`](azure-devops)). They are copied into the IaC, fleet and design repositories; the merge gate and
reviewer agent pipelines themselves belong to the guardrails repository ([`../guardrails`](../guardrails)).

| Purpose | GitHub Actions | Azure DevOps |
|---|---|---|
| Merge gate | [`guardrails/.github/workflows/merge-gate.yml`](../guardrails/.github/workflows/merge-gate.yml) (required workflow) | [`guardrails/azure-devops/pull-request.yml`](../guardrails/azure-devops/pull-request.yml) (required template) + [`merge-gate-sweep.yml`](../guardrails/azure-devops/merge-gate-sweep.yml) |
| Reviewer agent | [`guardrails/.github/workflows/reviewer-agent.yml`](../guardrails/.github/workflows/reviewer-agent.yml) | stage of [`pull-request.yml`](../guardrails/azure-devops/pull-request.yml) |
| IaC plan for the gate | [`github/iac-pull-request.yml`](github/iac-pull-request.yml) | [`azure-devops/iac-pull-request.yml`](azure-devops/iac-pull-request.yml) |
| Fleet rendered manifests for the gate | [`github/fleet-pull-request.yml`](github/fleet-pull-request.yml) | [`azure-devops/fleet-pull-request.yml`](azure-devops/fleet-pull-request.yml) |
| Design repository | gate with `REPOSITORY_HAS_PLAN=false` | [`azure-devops/design-pull-request.yml`](azure-devops/design-pull-request.yml) |
| Version pipeline → upgrade agent | [`github/version-pipeline.yml`](github/version-pipeline.yml) | [`azure-devops/version-pipeline.yml`](azure-devops/version-pipeline.yml) |
| Shared scripts | [`scripts/`](scripts) | [`scripts/`](scripts) |

The contract between CI and the gate is the same on both platforms: artifacts `plan-<env>/plan.json` and
`rendered-manifests/{base,head}/<env>/<cluster>/*.yaml` for the head commit; `aks-gate evaluate --artifacts <dir>`
finds them. See [azure-devops/README.md](azure-devops/README.md) for how the GitHub mechanisms map to Azure DevOps.

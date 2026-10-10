# aks-ai-agents

Implementation of the AI agents that run day 2 operations of the AKS platform described in
[aks-design](https://github.com/olljanat-ai/aks-design) –
[section 15, AI-driven day 2 operations](https://github.com/olljanat-ai/aks-design/blob/main/docs/15-ai-driven-day-2-operations.md),
plus the agent parts of
[section 17, implementation project](https://github.com/olljanat-ai/aks-design/blob/main/docs/17-implementation-project-and-acceptance-testing.md)
and the version pipeline of
[section 12](https://github.com/olljanat-ai/aks-design/blob/main/docs/12-zero-downtime-cluster-upgrades.md#update-policy).

> Status: **work in progress**, built for the build tenant (phase 2 of section 17). Not yet run against a real
> environment.

Agents may change every environment, prd included, but only the way a human platform engineer would: **with a pull
request**. They observe with read-only identities; a deterministic merge gate owned by humans decides what may be
merged and by whom.

```text
 alert / schedule / pipeline / PR event
            │
            ▼
 ┌────────────────────┐  read-only tools   ┌─────────────────────────────┐
 │ agent (Claude)     │ ─────────────────▶ │ Azure, clusters, Log        │
 │ src/aks_agents     │                    │ Analytics, design docs      │
 └─────────┬──────────┘                    └─────────────────────────────┘
           │ pull request (agent/<name>/*, signed commit, Agent / Agent-Run trailers)
           │ issue · comment · review · safe runbook dispatch
           ▼
 ┌────────────────────┐   ┌─────────────────────┐   ┌──────────────────────────────────┐
 │ CI attaches plan   │──▶│ reviewer agent      │──▶│ merge gate  src/aks_gate         │
 │ (terraform / what- │   │ (required check)    │   │ plan → tier → approvals → merge  │
 │ if / manifests)    │   └─────────────────────┘   └──────────────────────────────────┘
 └────────────────────┘                                          │ main
                                                                 ▼
                                   existing delivery: IaC pipelines, Flux, stateful pipeline
```

## What is here

| Path | Content |
|---|---|
| [`src/aks_agents/`](src/aks_agents) | Agent runtime: the Claude tool-use loop, tools with per-agent grants, audit trail, CLI |
| [`src/aks_agents/tools/`](src/aks_agents/tools) | Design and guardrails reading; repositories and pull requests; issues, reviews, runbooks; read-only `az`, Resource Graph, Log Analytics, `kubectl`, Flux; documentation sites |
| [`src/aks_agents/versions.py`](src/aks_agents/versions.py) | Version pipeline: due upgrade steps per the update policy (deterministic, no LLM) |
| [`src/aks_agents/permissions.py`](src/aks_agents/permissions.py) | Least-privilege roles derived from logged prototyping runs |
| [`src/aks_gate/`](src/aks_gate) | **Merge gate**: Terraform plan / what-if / rendered manifests → risk tier, violations, approvals, windows – no LLM |
| [`agents/`](agents) | The eight agent definitions and their instructions |
| [`guardrails/`](guardrails) | Content of the guardrails repository: `limits.yaml`, `tiers.yaml`, merge gate and reviewer workflows |
| [`config/platform.yaml`](config/platform.yaml) | Repositories, environments, clusters, model endpoint, documentation allow-list |
| [`deploy/`](deploy) | Container image entrypoint and Terraform for identities, roles and Container Apps jobs |

## The agents

| Agent | Triggered by | Output |
|---|---|---|
| Operations (on-call) | alerts, Flux notifications, failed pipelines, daily schedule, merged pull requests | incident and task issues, verification comments, safe runbook dispatch |
| Platform change | task issues | pull requests to IaC and fleet `infrastructure/` |
| Upgrade | version pipeline output | version pull requests with a compatibility report; halt + incident on failed health |
| Release | successful release in the previous environment | promotion pull request dev → acc → prd with release notes and risk summary |
| Capacity and cost | weekly schedule, quota and budget alerts | pull requests within `limits.yaml`, issues to application teams |
| Security and compliance | daily schedule, Defender and Key Vault events | pull requests for platform findings, issues for application teams |
| Onboarding | onboarding request issue | pull requests creating namespace (tier low), identity (tier high), overlays |
| Reviewer | every pull request | review and the required `reviewer-agent` check – never an approval |

What keeps them inside their role, independently of the model:

- **Tool grants**: an agent only gets the tools its definition lists; repositories and writable paths are part of the
  grant; every tool input is validated against its schema; denied calls are logged.
- **Read-only by construction**: `az` and `kubectl` allow only read verbs in guarded mode (on top of read-only Azure
  roles); commands that reveal secrets or credentials, `Secret` objects and application logs are denied in every mode.
- **Git is the only write path**: one signed commit per pull request through the GitHub API on `agent/<name>/*`,
  with `Agent:` / `Agent-Run:` trailers and a body with intent, evidence, design section and rollback.
- **The merge gate** computes the tier from the plan, never from a label: data deletion, identity, network, policy
  and pipeline changes need humans; agents cannot touch the guardrails, runbooks or their own identities at all;
  prd is one cell per pull request, inside change windows and merge rate limits; `agents.enabled: false` stops all.
- **Audit**: every model response, tool call and result is written to `runs/<run-id>/` (and the immutable storage
  account); commits, pull requests and issues carry the run ID.

## Using it

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check . && .venv/bin/pytest

# Agents
aks-agent validate                                   # check the agent definitions against the role rules
aks-agent list                                       # what each agent may write
AKS_AGENT_ENV=dev aks-agent run operations-agent --dry-run --task "Write the daily health report."
aks-agent run upgrade-agent --env dev --task "Open the due version pull requests." --trigger steps.json
aks-agent permissions --runs runs/                   # least-privilege report from prototyping runs

# Version pipeline (section 12)
aks-agent versions --catalog aks-releases.yaml --clusters current-versions.yaml

# Merge gate
aks-gate collect --guardrails guardrails --repository org/aks-iac --number 42 --output pr.json --changed-files files.txt
aks-gate evaluate --guardrails guardrails --pr pr.json --changed-files files.txt \
  --plan dev:plan-dev.json --manifests-base rendered/base --manifests-head rendered/head --markdown
```

Runs need a Claude endpoint: the Claude API (`ANTHROPIC_API_KEY`, server-side refusal fallbacks) or Microsoft
Foundry (`model.provider: foundry` in `platform.yaml`, Entra ID token of the run's managed identity); the model
provider and region are still an open question of the design. `AKS_AGENT_MODE=prototype` (build tenant, phases 1–2
only) lets the tools write directly and logs it.

## Assumptions and open points for the design

- **IaC repository layout**: the agents' writable paths assume `environments/<env>/{versions,capacity,applications}.auto.tfvars`,
  `modules/`, `policy/`, `monitoring/` ([agents/README.md](agents/README.md)). Adjust the grants if the real layout differs.
- **Security agent and clusters**: the design's role table does not give the security agent `Azure Kubernetes
  Service RBAC Reader`, but its stateful-cluster drift report needs it; the Terraform grants it.
- **Reviewer model**: the design asks for a different model than the authors where possible; set `model.name` in
  `agents/reviewer-agent.yaml` once the provider question is answered.
- **Event dispatch**: alerts and pipelines start the agents' Container Apps jobs with `az containerapp job start`;
  the dispatcher (action group → Logic App / Function) is not part of this repository yet.

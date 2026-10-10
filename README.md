# aks-ai-agents

Implementation of the AI agents that run day 2 operations of the AKS platform described in
[aks-design](https://github.com/olljanat-ai/aks-design), mainly
[section 15 – AI-driven day 2 operations](https://github.com/olljanat-ai/aks-design/blob/main/docs/15-ai-driven-day-2-operations.md)
and the agent parts of
[section 17 – Implementation project](https://github.com/olljanat-ai/aks-design/blob/main/docs/17-implementation-project-and-acceptance-testing.md).

> Status: **work in progress**, built in the build tenant (phase 2 of section 17).

## What is here

| Path | Content |
|---|---|
| `src/aks_agents/` | Agent runtime: Claude tool-use loop, read-only tools, Git / pull request tools, audit trail |
| `src/aks_gate/` | Deterministic **merge gate**: plan → risk tier, `limits.yaml` checks, approvals – no LLM involved |
| `agents/` | Agent definitions: instructions, tool allow-lists, allowed repositories and paths |
| `guardrails/` | Content of the **guardrails repository**: `limits.yaml`, tier rules, the merge gate workflow |

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check . && .venv/bin/pytest
```

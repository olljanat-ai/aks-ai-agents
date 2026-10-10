"""The merge gate decision: tier, violations, required approvals, windows and rate limits.

The gate never trusts what the author says about a change: the tier comes from the plan, the agent from the branch
and the GitHub App that opened the pull request, and labels count only when a human set them.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from .config import GateConfig, per_agent
from .model import PlanSet, Tier
from .pr import PullRequest, branch_agent, check_structure
from .rules import ENV_ORDER, Finding, evaluate
from .windows import in_any_window, in_freeze, window_timezone

REJECT, WAIT, PASS = "reject", "wait", "pass"


@dataclass
class Decision:
    decision: str
    tier: Tier
    agent: str | None
    environments: list[str]
    findings: list[Finding] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)  # reasons to reject
    waiting: list[str] = field(default_factory=list)  # reasons to wait (approvals, windows)
    required: dict[str, Any] = field(default_factory=dict)

    @property
    def auto_merge(self) -> bool:
        return self.decision == PASS and self.tier == Tier.LOW

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "tier": str(self.tier),
            "autoMerge": self.auto_merge,
            "agent": self.agent,
            "environments": self.environments,
            "violations": self.violations,
            "waiting": self.waiting,
            "required": self.required,
            "findings": [f.to_dict() for f in self.findings],
        }

    def summary_markdown(self) -> str:
        icon = {PASS: "✅", WAIT: "⏳", REJECT: "⛔"}[self.decision]
        lines = [
            f"## {icon} Merge gate: {self.decision} – risk tier **{self.tier}**",
            "",
            f"- Author: {'agent `' + self.agent + '`' if self.agent else 'human'}",
            f"- Environments: {', '.join(self.environments) or 'none'}",
            f"- Required: reviewer agent{''.join(', 1 human from ' + t for t in self.required.get('teams', []))}",
            f"- Auto-merge: {'yes' if self.auto_merge else 'no'}",
        ]
        if self.violations:
            lines += ["", "### Rejected because", *[f"- {v}" for v in self.violations]]
        if self.waiting:
            lines += ["", "### Waiting for", *[f"- {w}" for w in self.waiting]]
        if self.findings:
            lines += ["", "### Findings", "", "| Rule | Tier | Subject | Finding |", "|---|---|---|---|"]
            for f in sorted(self.findings, key=lambda f: (not f.violation, -(f.tier or 0))):
                tier = "⛔ violation" if f.violation else str(f.tier)
                lines.append(f"| {f.rule} | {tier} | `{f.subject}` | {f.message} |")
        return "\n".join(lines) + "\n"


def environments_of(plan: PlanSet) -> list[str]:
    envs = {r.environment for r in plan.effective_resources} | {m.environment for m in plan.effective_manifests}
    for path in plan.changed_files:
        for env in ENV_ORDER:
            if re.search(rf"(^|/){env}(/|$|\.)", path):
                envs.add(env)
    return [e for e in ENV_ORDER if e in envs] + sorted(e for e in envs if e not in ENV_ORDER)


def identify_agent(pr: PullRequest, config: GateConfig) -> tuple[str | None, list[str]]:
    """The agent behind a pull request, from its branch and author; problems if they disagree."""
    from_branch = branch_agent(pr.head_ref)
    from_login = config.agent_for_login(pr.author)
    problems = []
    if from_login and from_branch != from_login:
        problems.append(f"{pr.author} must push to agent/{from_login}/*, not {pr.head_ref}")
    # A human pushing to an agent/* branch is held to that agent's rules.
    return from_login or from_branch, problems


def check_approvals(pr: PullRequest, teams: list[str]) -> list[str]:
    """Missing human approvals: one distinct human per required team, on the head commit, not the author."""
    latest: dict[str, Any] = {}
    for review in pr.reviews:
        if review.state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[review.user] = review
    approvers = [
        r
        for r in latest.values()
        if r.state == "APPROVED" and r.commit_id == pr.head_sha and not r.is_bot and r.user != pr.author
    ]
    missing, used = [], set()
    for team in teams:
        candidate = next((r for r in approvers if team in r.teams and r.user not in used), None)
        if candidate:
            used.add(candidate.user)
        else:
            missing.append(f"approval of the head commit from a member of team '{team}'")
    blocking = [r.user for r in latest.values() if r.state == "CHANGES_REQUESTED" and not r.is_bot]
    missing += [f"changes requested by {u}" for u in blocking]
    return missing


def decide(pr: PullRequest, plan: PlanSet, config: GateConfig, now: dt.datetime | None = None) -> Decision:
    now = now or dt.datetime.now(dt.UTC)
    limits, gate = config.limits, config.gate
    agent, problems = identify_agent(pr, config)
    findings = evaluate(plan, config, agent)
    envs = environments_of(plan)

    violations = problems + [f"{f.rule}: {f.message} ({f.subject})" for f in findings if f.violation]
    tier = max([f.tier for f in findings if f.tier is not None], default=Tier.LOW)

    if agent:
        if not limits.agents_enabled:
            violations.append("agents are disabled (agents.enabled: false in limits.yaml)")
        pr_rules = gate.get("pullRequest") or {}
        violations += check_structure(
            pr, agent, pr_rules.get("requiredSections", []), pr_rules.get("requiredTrailers", [])
        )
        if tier == Tier.NOT_ALLOWED:
            violations.append("the change is not allowed for agents")
        for env in envs:
            minimum = per_agent(limits.get("agents.minimumTier"), agent, {}).get(env)
            if minimum and Tier.parse(minimum) > tier:
                findings.append(
                    Finding("minimum-tier", f"{agent} in {env} starts at {minimum}", env, Tier.parse(minimum))
                )
                tier = Tier.parse(minimum)
        max_open = per_agent(limits.get("agents.openPullRequests"), agent)
        if max_open is not None and pr.open_pull_requests > max_open:
            violations.append(f"{agent} has {pr.open_pull_requests} open pull requests; limit {max_open}")
    elif tier == Tier.NOT_ALLOWED:
        tier = Tier.HIGH

    if pr.reviewer_agent_flagged and tier < Tier.MEDIUM:
        findings.append(Finding("reviewer-flag", "the reviewer agent flagged this change", "", Tier.MEDIUM))
        tier = Tier.MEDIUM

    required = dict((gate.get("approvals") or {}).get(str(tier), {"reviewerAgent": True, "teams": ["platform"]}))
    waiting: list[str] = []
    if required.get("reviewerAgent", True) and pr.reviewer_agent_conclusion != "success":
        waiting.append(f"reviewer agent check (now: {pr.reviewer_agent_conclusion or 'not run'})")
    waiting += check_approvals(pr, required.get("teams", []))

    for env in envs:
        windows = limits.get(f"environments.{env}.changeWindows") or []
        freeze = in_freeze(limits.get(f"environments.{env}.freezes") or [], now, window_timezone(windows))
        if pr.incident_fix_by_human:
            continue
        if freeze:
            waiting.append(f"{env} is frozen ({freeze})")
        elif not in_any_window(windows, now):
            waiting.append(f"{env} change window ({'; '.join(windows)})")
        if agent:
            max_merges = per_agent(limits.get(f"environments.{env}.maxMergesPerDay"), agent)
            done = pr.merges_today.get(env, 0)
            if max_merges is not None and done >= max_merges:
                waiting.append(f"{agent} already merged {done} changes to {env} today (limit {max_merges})")

    decision = REJECT if violations else WAIT if waiting else PASS
    return Decision(
        decision=decision,
        tier=tier,
        agent=agent,
        environments=envs,
        findings=findings,
        violations=violations,
        waiting=waiting,
        required=required,
    )

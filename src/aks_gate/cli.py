"""``aks-gate``: evaluate a pull request and print the decision.

Exit codes: 0 pass, 1 reject, 2 wait (approvals, change window, rate limit), 3 usage or input error.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from .config import GateConfig
from .gate import PASS, REJECT, decide
from .manifests import diff_trees, load_tree
from .model import PlanSet
from .plan import load_plan
from .pr import PullRequest

EXIT = {PASS: 0, REJECT: 1, "wait": 2}


def build_plan(args: argparse.Namespace, repository: str) -> PlanSet:
    plan = PlanSet(repository=repository)
    for spec in args.plan or []:
        env, _, path = spec.partition(":")
        if not path:
            raise SystemExit(f"--plan expects <env>:<path>, got {spec!r}")
        plan.resources += load_plan(path, env)
    if args.manifests_head:
        plan.manifests = diff_trees(args.manifests_base or "/nonexistent", args.manifests_head)
        for (env, _cluster), objects in load_tree(args.manifests_head).items():
            plan.reference_manifests.setdefault(env, []).extend(objects)
    if args.changed_files:
        plan.changed_files = [
            line.strip() for line in Path(args.changed_files).read_text().splitlines() if line.strip()
        ]
    return plan


def cmd_evaluate(args: argparse.Namespace) -> int:
    config = GateConfig.load(args.guardrails)
    pr = PullRequest.from_dict(json.loads(Path(args.pr).read_text()))
    if not (args.plan or args.manifests_head or args.no_plan):
        print("no --plan or --manifests-head given; pass --no-plan for a repository without a plan", file=sys.stderr)
        return 3
    plan = build_plan(args, pr.repository)
    now = dt.datetime.fromisoformat(args.now) if args.now else None
    decision = decide(pr, plan, config, now)
    result = json.dumps(decision.to_dict(), indent=2)
    if args.output:
        Path(args.output).write_text(result + "\n")
    if args.summary:
        Path(args.summary).write_text(decision.summary_markdown())
    print(decision.summary_markdown() if args.markdown else result)
    return EXIT[decision.decision]


def cmd_collect(args: argparse.Namespace) -> int:
    from .github import GitHub, changed_files, collect

    config = GateConfig.load(args.guardrails)
    gh = GitHub()
    data = collect(gh, args.repository, args.number, config)
    Path(args.output).write_text(json.dumps(data, indent=2) + "\n")
    if args.changed_files:
        Path(args.changed_files).write_text("\n".join(changed_files(gh, args.repository, args.number)) + "\n")
    return 0


def cmd_collect_azdo(args: argparse.Namespace) -> int:
    from .azure_devops import AzureDevOps, changed_files, collect

    config = GateConfig.load(args.guardrails)
    ado = AzureDevOps(args.collection_url, args.project)
    data = collect(ado, args.repository, args.number, config)
    Path(args.output).write_text(json.dumps(data, indent=2) + "\n")
    if args.changed_files:
        Path(args.changed_files).write_text("\n".join(changed_files(ado, args.repository, args.number)) + "\n")
    return 0


def cmd_publish_azdo(args: argparse.Namespace) -> int:
    from .azure_devops import AzureDevOps, publish

    ado = AzureDevOps(args.collection_url, args.project)
    publish(ado, args.repository, args.number, json.loads(Path(args.decision).read_text()), args.target_url or "")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aks-gate", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    ev = sub.add_parser("evaluate", help="decide on a pull request")
    ev.add_argument("--guardrails", required=True, help="directory with limits.yaml and tiers.yaml")
    ev.add_argument("--pr", required=True, help="pull request facts (JSON, see 'collect')")
    ev.add_argument("--plan", action="append", help="<env>:<terraform plan JSON or what-if JSON>, repeatable")
    ev.add_argument("--manifests-base", help="rendered manifests of the base: <dir>/<env>/<cluster>/*.yaml")
    ev.add_argument("--manifests-head", help="rendered manifests of the head")
    ev.add_argument("--changed-files", help="file with one changed path per line")
    ev.add_argument("--no-plan", action="store_true", help="the repository has no plan (e.g. the design repository)")
    ev.add_argument("--now", help="evaluation time (ISO 8601), for tests and re-runs")
    ev.add_argument("--output", help="write the decision JSON here")
    ev.add_argument("--summary", help="write the Markdown summary here (e.g. $GITHUB_STEP_SUMMARY)")
    ev.add_argument("--markdown", action="store_true", help="print Markdown instead of JSON")
    ev.set_defaults(func=cmd_evaluate)

    co = sub.add_parser("collect", help="fetch pull request facts from GitHub (GITHUB_TOKEN)")
    co.add_argument("--guardrails", required=True)
    co.add_argument("--repository", required=True, help="owner/name")
    co.add_argument("--number", required=True, type=int)
    co.add_argument("--output", required=True)
    co.add_argument("--changed-files", help="also write the changed paths here")
    co.set_defaults(func=cmd_collect)

    for name, func, helptext in (
        ("collect-azdo", cmd_collect_azdo, "fetch pull request facts from Azure DevOps (SYSTEM_ACCESSTOKEN)"),
        ("publish-azdo", cmd_publish_azdo, "post the merge-gate status (and auto-complete) to Azure DevOps"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--collection-url", required=True, help="e.g. $(System.CollectionUri)")
        p.add_argument("--project", required=True)
        p.add_argument("--repository", required=True, help="repository name or ID")
        p.add_argument("--number", required=True, type=int, help="pull request ID")
        if name == "collect-azdo":
            p.add_argument("--guardrails", required=True)
            p.add_argument("--output", required=True)
            p.add_argument("--changed-files")
        else:
            p.add_argument("--decision", required=True, help="decision JSON written by 'evaluate --output'")
            p.add_argument("--target-url", help="link to the gate run")
        p.set_defaults(func=func)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError, KeyError) as err:
        print(f"aks-gate: {err}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())

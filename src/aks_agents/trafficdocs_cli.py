"""``aks-agent docs traffic``: the traffic flow pages from local clones, without a model (trafficdocs.py).

    aks-agent docs traffic --spoke dev-int --landing-zone ../azure-landing-zone --fleet ../aks-fluxcd-platform \\
        --iac ../aks-terraform --app-repo onek8s/aks-fluxcd-example=../aks-fluxcd-example [--check] [--github]

Writes into ``<landing-zone>/docs/traffic-flows`` (``--output`` for another directory) and prints the findings;
``--check`` writes nothing and fails when the pages there are stale (a CI check); ``--github`` also marks the
destinations the egress agent's open pull requests propose (needs AKS_AGENT_GITHUB_TOKEN and the platform config).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import trafficdocs
from .settings import PlatformConfig, Settings


def _output(name: str, value: object) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{name}={value}\n")


def cmd_traffic(args: argparse.Namespace, settings: Settings) -> int:
    app_repos: dict[str, Path] = {}
    for item in args.app_repo or []:
        slug, _, directory = item.partition("=")
        if not directory:
            print(f"--app-repo takes owner/repo=directory, not {item!r}", file=sys.stderr)
            return 2
        app_repos[trafficdocs.repo_slug(slug)] = Path(directory)
    sources = trafficdocs.Sources(
        landing_zone=Path(args.landing_zone),
        fleet=Path(args.fleet),
        iac=Path(args.iac),
        spoke=args.spoke,
        app_repos=app_repos,
    )
    if args.github:
        from .egress_cli import GitHub

        platform = PlatformConfig.load(settings.platform_file)
        for namespace, entries in GitHub(platform, args.spoke).open_pr_files().items():
            sources.proposed[namespace] = {entry: pr["url"] for entry, pr in entries.items()}
    result = trafficdocs.generate(sources)
    out_dir = Path(args.output) if args.output else Path(args.landing_zone) / trafficdocs.DOCS_DIR
    print(f"{len(result.applications)} applications of {args.spoke}; {len(result.files)} pages")
    print(trafficdocs.findings_text(result.findings))
    _output("findings", len(result.findings))
    if args.check:
        stale = trafficdocs.diff(result, out_dir, args.spoke)
        for rel in stale:
            print(f"stale: {out_dir / rel}")
        print("up to date" if not stale else f"{len(stale)} pages are stale: run aks-agent docs traffic")
        _output("changed", len(stale))
        return 1 if stale else 0
    changed, removed = trafficdocs.write(result, out_dir, args.spoke)
    for rel in changed:
        print(f"wrote {out_dir / rel}")
    for rel in removed:
        print(f"removed {out_dir / rel}")
    print(f"changed: {len(changed)}, removed: {len(removed)}, unchanged: {len(result.files) - len(changed)}")
    _output("changed", len(changed) + len(removed))
    return 0


def add_parser(sub) -> None:
    docs = sub.add_parser("docs", help="generated documentation (traffic flows per application and zone)")
    dsub = docs.add_subparsers(dest="docs_command", required=True)
    tr = dsub.add_parser("traffic", help="traffic flow pages with Mermaid pictures, from local clones, no model")
    tr.add_argument("--spoke", required=True, help="<env>-<exposure>, e.g. dev-int")
    tr.add_argument("--landing-zone", required=True, help="clone of the landing zone (the firewall rule files)")
    tr.add_argument("--fleet", required=True, help="clone of the fleet repository (zone allow-lists)")
    tr.add_argument("--iac", required=True, help="clone of aks-terraform (cells, managed namespaces)")
    tr.add_argument(
        "--app-repo",
        action="append",
        metavar="OWNER/REPO=DIR",
        help="clone of an application repository the managed namespaces name; repeatable",
    )
    tr.add_argument("--output", help="directory for the pages (default <landing-zone>/docs/traffic-flows)")
    tr.add_argument("--check", action="store_true", help="write nothing; fail when the pages there are stale")
    tr.add_argument("--github", action="store_true", help="mark destinations in the egress agent's open pull requests")
    tr.add_argument("--env", help="environment (default AKS_AGENT_ENV)")
    tr.set_defaults(func=cmd_traffic)

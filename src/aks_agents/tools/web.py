"""Reading documentation (release notes, Kubernetes deprecations) from an allow-list of sites – the same list the
hub Firewall allows for the agents."""

from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request
from typing import Any

from .base import ToolContext, ToolError, tool

MAX_BYTES = 2_000_000


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|nav|header|footer)\b.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr)>", "\n", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


@tool(
    "fetch_documentation",
    "Fetch a documentation page or release notes from an allowed site (e.g. learn.microsoft.com, github.com/Azure/AKS "
    "releases, kubernetes.io). Returns text.",
    {"url": {"type": "string", "pattern": "^https://"}},
)
def fetch_documentation(ctx: ToolContext, args: dict[str, Any]) -> str:
    host = (urllib.parse.urlparse(args["url"]).hostname or "").lower()
    sites = ctx.platform.get("documentationSites") or []
    if not any(host == s or host.endswith("." + s) for s in sites):
        raise ToolError(f"{host} is not an allowed documentation site: {sites}")
    request = urllib.request.Request(args["url"], headers={"User-Agent": "aks-ai-agents"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(MAX_BYTES).decode(response.headers.get_content_charset() or "utf-8", errors="replace")
            content_type = response.headers.get_content_type()
    except OSError as err:
        raise ToolError(f"fetch failed: {err}") from err
    return html_to_text(raw) if "html" in content_type else raw

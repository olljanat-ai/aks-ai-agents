"""Model client for the agents: Claude through the Claude API (first party) or Microsoft Foundry, or an OpenAI-format
model on Foundry (``azure-openai``, openai_compat.py) where no Claude deployment is available.

The model provider and region are an open question of the design (data residency of diagnostics sent to the
model); both providers expose the same Messages API, so only the client construction differs.
"""

from __future__ import annotations

import contextlib
import subprocess
from typing import Any

import anthropic

from .settings import PlatformConfig

FALLBACK_BETA = "server-side-fallback-2026-07-01"
COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"


def azure_token_provider():
    """Entra ID token for Foundry from the run's managed identity (workload identity), no API key."""
    try:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider

        return get_bearer_token_provider(DefaultAzureCredential(), COGNITIVE_SCOPE)
    except ImportError:

        def from_cli() -> str:
            out = subprocess.run(
                [
                    "az",
                    "account",
                    "get-access-token",
                    "--scope",
                    COGNITIVE_SCOPE,
                    "--query",
                    "accessToken",
                    "-o",
                    "tsv",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            return out.stdout.strip()

        return from_cli


class ModelClient:
    """Thin wrapper that builds the request the same way for every provider."""

    def __init__(self, platform: PlatformConfig, client: Any | None = None):
        self.provider = platform.get("model.provider", "anthropic")
        self.model = platform.get("model.name", "claude-opus-5-5")
        self.fallbacks = platform.get("model.fallbacks", "default")
        # One state per run, so that the conversation stays pinned to the model that accepted it.
        self._fallback_state = anthropic.BetaFallbackState()
        if client is not None:
            self.client = client
        elif self.provider == "azure-openai":
            from .openai_compat import AzureOpenAIClient

            self.client = AzureOpenAIClient(platform.get("model.foundryResource"), azure_token_provider())
        elif self.provider == "foundry":
            middleware = []
            if isinstance(self.fallbacks, list) and self.fallbacks:
                middleware.append(anthropic.BetaRefusalFallbackMiddleware([{"model": m} for m in self.fallbacks]))
            self.client = anthropic.AnthropicFoundry(
                resource=platform.get("model.foundryResource"),
                azure_ad_token_provider=azure_token_provider(),
                middleware=middleware,
            )
        else:
            self.client = anthropic.Anthropic()

    def create(
        self,
        *,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        effort: str,
        max_tokens: int,
        model: str | None = None,
    ) -> Any:
        if self.provider == "azure-openai":
            # OpenAI-format models: no thinking, effort or caching parameters (openai_compat.py)
            return self.client.create(
                model=model or self.model, system=system, messages=messages, tools=tools, max_tokens=max_tokens
            )
        params: dict[str, Any] = {
            "model": model or self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "tools": tools,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort},
            "cache_control": {"type": "ephemeral"},
        }
        state: contextlib.AbstractContextManager[Any] = contextlib.nullcontext()
        if self.provider == "anthropic" and self.fallbacks == "default":
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        elif self.provider == "foundry" and isinstance(self.fallbacks, list) and self.fallbacks:
            state = self._fallback_state
        with state:
            return self.client.beta.messages.create(**params)

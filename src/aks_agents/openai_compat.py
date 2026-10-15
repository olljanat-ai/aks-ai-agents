"""OpenAI-format models (Azure OpenAI deployments on Microsoft Foundry) behind the agents' Messages-API shaped loop.

The runtime speaks Anthropic's Messages API: content blocks, ``tool_use`` / ``tool_result``, ``stop_reason``. This
client translates one request to the Chat Completions API with function tools and the answer back into the same
shape, so the loop, the tool grants and the audit trail stay as they are. Authentication: an Entra ID token of the
run's identity (no API key), like the Foundry Claude client. Standard library only.

Not translated: thinking, effort and prompt caching (the request has no equivalent; they are left out).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

API_VERSION = "2024-10-21"
RETRIES = 4


def _field(block: Any, name: str, default: Any = None) -> Any:
    return block.get(name, default) if isinstance(block, dict) else getattr(block, name, default)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if _field(block, "type") == "text":
            parts.append(_field(block, "text", ""))
    return "\n".join(parts)


def to_chat_messages(system: list[dict[str, Any]], messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Messages API history -> Chat Completions messages."""
    chat: list[dict[str, Any]] = [{"role": "system", "content": "\n\n".join(_text([b]) for b in system)}]
    for message in messages:
        role, content = message["role"], message["content"]
        if role == "assistant":
            out: dict[str, Any] = {"role": "assistant", "content": _text(content) or None}
            calls = [
                {
                    "id": _field(b, "id"),
                    "type": "function",
                    "function": {"name": _field(b, "name"), "arguments": json.dumps(_field(b, "input") or {})},
                }
                for b in (content if isinstance(content, list) else [])
                if _field(b, "type") == "tool_use"
            ]
            if calls:
                out["tool_calls"] = calls
            chat.append(out)
            continue
        if isinstance(content, str):
            chat.append({"role": "user", "content": content})
            continue
        texts = []
        for block in content:
            if _field(block, "type") == "tool_result":
                result = _field(block, "content", "")
                chat.append(
                    {
                        "role": "tool",
                        "tool_call_id": _field(block, "tool_use_id"),
                        "content": result if isinstance(result, str) else _text(result),
                    }
                )
            elif _field(block, "type") == "text":
                texts.append(_field(block, "text", ""))
        if texts:
            chat.append({"role": "user", "content": "\n".join(texts)})
    return chat


def to_chat_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t["input_schema"]},
        }
        for t in tools
    ]


def from_chat_response(data: dict[str, Any], model: str) -> SimpleNamespace:
    """Chat Completions answer -> a Messages API shaped response (content blocks, stop_reason, usage)."""
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content: list[SimpleNamespace] = []
    if message.get("content"):
        content.append(SimpleNamespace(type="text", text=message["content"]))
    for call in message.get("tool_calls") or []:
        try:
            arguments = json.loads(call["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {"_unparsed_arguments": call["function"].get("arguments")}
        content.append(SimpleNamespace(type="tool_use", id=call["id"], name=call["function"]["name"], input=arguments))
    finish = choice.get("finish_reason")
    if message.get("refusal"):
        stop_reason = "refusal"
    elif any(b.type == "tool_use" for b in content):
        stop_reason = "tool_use"
    elif finish == "length":
        stop_reason = "max_tokens"
    else:
        stop_reason = "end_turn"
    usage = data.get("usage") or {}
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        stop_details=message.get("refusal"),
        model=data.get("model", model),
        usage=SimpleNamespace(
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            cache_read_input_tokens=((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)),
            cache_creation_input_tokens=0,
        ),
    )


class AzureOpenAIClient:
    """Chat Completions on an Azure OpenAI deployment of a Foundry resource."""

    def __init__(
        self,
        resource: str,
        token_provider: Callable[[], str],
        post: Callable[[str, dict[str, str], bytes], dict[str, Any]] | None = None,
    ):
        if not resource:
            raise ValueError("model.foundryResource is required for model.provider azure-openai")
        self.endpoint = f"https://{resource}.openai.azure.com/openai/deployments"
        self.token_provider = token_provider
        self.post = post or _post

    def create(self, *, model: str, system, messages, tools, max_tokens: int) -> SimpleNamespace:
        body = {
            "messages": to_chat_messages(system, messages),
            "tools": to_chat_tools(tools),
            "tool_choice": "auto",
            "max_tokens": max_tokens,
            "temperature": 0,
        }
        url = f"{self.endpoint}/{model}/chat/completions?api-version={API_VERSION}"
        headers = {"Authorization": f"Bearer {self.token_provider()}", "Content-Type": "application/json"}
        return from_chat_response(self.post(url, headers, json.dumps(body).encode()), model)


def _post(url: str, headers: dict[str, str], body: bytes) -> dict[str, Any]:
    delay = 2.0
    for attempt in range(RETRIES + 1):
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as err:
            # Throttling and transient server errors are retried; anything else is the caller's to see.
            if err.code in (429, 500, 502, 503, 504) and attempt < RETRIES:
                retry_after = err.headers.get("retry-after") if err.headers else None
                time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else delay)
                delay *= 2
                continue
            raise RuntimeError(f"Azure OpenAI {err.code}: {err.read().decode(errors='replace')[:2000]}") from err
    raise RuntimeError("Azure OpenAI: retries exhausted")

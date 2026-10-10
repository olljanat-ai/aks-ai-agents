"""A scripted stand-in for the Claude client."""

from types import SimpleNamespace


def text(t):
    return SimpleNamespace(type="text", text=t)


def use(name, input, id=None):
    return SimpleNamespace(type="tool_use", name=name, input=input, id=id or f"toolu_{name}")


def response(*blocks, stop_reason=None):
    stop = stop_reason or ("tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn")
    usage = SimpleNamespace(
        input_tokens=100, output_tokens=10, cache_read_input_tokens=0, cache_creation_input_tokens=0
    )
    return SimpleNamespace(content=list(blocks), stop_reason=stop, usage=usage, model="fake", stop_details=None)


class FakeModel:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **params):
        self.requests.append({**params, "messages": list(params["messages"])})
        return self.responses.pop(0)

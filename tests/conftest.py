from pathlib import Path

import pytest

from aks_gate.config import GateConfig
from aks_gate.model import Action, ManifestChange, ResourceChange

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def config() -> GateConfig:
    return GateConfig.load(ROOT / "guardrails")


def resource(
    type_, action=Action.UPDATE, before=None, after=None, name="r", env="dev", address=None, source="terraform"
):
    return ResourceChange(
        address=address or f"{type_}.{name}",
        type=type_,
        name=name,
        action=action,
        environment=env,
        before=before,
        after=after,
        source=source,
    )


def manifest(
    kind, action=Action.UPDATE, before=None, after=None, name="x", namespace="ns", env="dev", cluster="sl-az1"
):
    return ManifestChange(
        environment=env,
        cluster=cluster,
        api_version="v1",
        kind=kind,
        namespace=namespace,
        name=name,
        action=action,
        before=before,
        after=after,
    )

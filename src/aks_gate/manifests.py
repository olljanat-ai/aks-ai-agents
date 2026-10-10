"""Diff of rendered fleet manifests per environment and cluster.

CI renders the fleet repository (``kustomize build`` of every ``clusters/<env>/<cell>`` entry point and every
``apps/*/stateful/<env>`` overlay) for the base and the head of a pull request into::

    <root>/<env>/<cluster>/*.yaml

The gate compares the two trees object by object.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .model import Action, ManifestChange

ObjectKey = tuple[str, str, str, str]  # apiVersion group, kind, namespace, name


def _key(obj: dict[str, Any]) -> ObjectKey:
    meta = obj.get("metadata") or {}
    group = str(obj.get("apiVersion", "")).split("/")[0] if "/" in str(obj.get("apiVersion", "")) else ""
    return group, obj.get("kind", ""), meta.get("namespace", "") or "", meta.get("name", "")


def load_objects(directory: Path) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    for path in sorted(directory.rglob("*")):
        if path.suffix not in (".yaml", ".yml") or not path.is_file():
            continue
        for doc in yaml.safe_load_all(path.read_text()):
            if not isinstance(doc, dict):
                continue
            if doc.get("kind") == "List":
                objects.extend(i for i in doc.get("items", []) if isinstance(i, dict))
            else:
                objects.append(doc)
    return objects


def load_tree(root: str | Path) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """``{(env, cluster): [objects]}`` of a rendered tree; a missing root is an empty tree."""
    root = Path(root)
    tree: dict[tuple[str, str], list[dict[str, Any]]] = {}
    if not root.is_dir():
        return tree
    for env_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for cluster_dir in sorted(p for p in env_dir.iterdir() if p.is_dir()):
            tree[(env_dir.name, cluster_dir.name)] = load_objects(cluster_dir)
    return tree


def _strip(obj: dict[str, Any]) -> dict[str, Any]:
    """Drop fields that change without meaning (status, generated metadata)."""
    obj = dict(obj)
    obj.pop("status", None)
    meta = dict(obj.get("metadata") or {})
    for field in ("creationTimestamp", "generation", "resourceVersion", "uid", "managedFields"):
        meta.pop(field, None)
    obj["metadata"] = meta
    return obj


def diff_trees(base_root: str | Path, head_root: str | Path) -> list[ManifestChange]:
    base, head = load_tree(base_root), load_tree(head_root)
    changes: list[ManifestChange] = []
    for env, cluster in sorted(set(base) | set(head)):
        before = {_key(o): _strip(o) for o in base.get((env, cluster), [])}
        after = {_key(o): _strip(o) for o in head.get((env, cluster), [])}
        for key in sorted(set(before) | set(after)):
            b, a = before.get(key), after.get(key)
            if b is None:
                action = Action.CREATE
            elif a is None:
                action = Action.DELETE
            elif a != b:
                action = Action.UPDATE
            else:
                continue
            obj = a or b or {}
            changes.append(
                ManifestChange(
                    environment=env,
                    cluster=cluster,
                    api_version=obj.get("apiVersion", ""),
                    kind=key[1],
                    namespace=key[2],
                    name=key[3],
                    action=action,
                    before=b,
                    after=a,
                )
            )
    return changes


def container_images(obj: dict[str, Any] | None) -> list[str]:
    """All container images of a workload object (Deployment, StatefulSet, CronJob, Pod, ...)."""
    if not obj:
        return []
    images: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in ("containers", "initContainers", "ephemeralContainers"):
                for container in node.get(key) or []:
                    if isinstance(container, dict) and container.get("image"):
                        images.append(container["image"])
            for value in node.values():
                if isinstance(value, dict | list):
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(obj.get("spec") or {})
    return images


def without_images(obj: dict[str, Any] | None) -> Any:
    """Copy of an object with every container image removed – to check that only images changed."""
    if obj is None:
        return None

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out = {}
            for key, value in node.items():
                if key in ("containers", "initContainers", "ephemeralContainers") and isinstance(value, list):
                    out[key] = [
                        {k: v for k, v in c.items() if k != "image"} if isinstance(c, dict) else c for c in value
                    ]
                else:
                    out[key] = walk(value)
            return out
        if isinstance(node, list):
            return [walk(i) for i in node]
        return node

    return walk(obj)

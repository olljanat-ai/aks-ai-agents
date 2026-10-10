"""Audit trail of an agent run: every model request, tool call and result, linked by the run ID.

Runs are written as JSON lines under ``runs/<run-id>/`` and, when configured, uploaded to the immutable storage
account in the management subscription. Git commits (``Agent-Run`` trailer), pull requests and Azure / Kubernetes
logs lead back to the run through the ID.
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
import subprocess
from pathlib import Path
from typing import Any

MAX_EVENT_TEXT = 20_000


def new_run_id(agent: str, now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.UTC)
    return f"{agent}-{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_EVENT_TEXT:
        return value[:MAX_EVENT_TEXT] + f"... [{len(value) - MAX_EVENT_TEXT} more characters]"
    if isinstance(value, dict):
        return {k: _clip(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v) for v in value]
    return value


class RunRecorder:
    def __init__(self, runs_dir: Path, run_id: str):
        self.run_id = run_id
        self.directory = Path(runs_dir) / run_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self._events = (self.directory / "events.jsonl").open("a", encoding="utf-8")

    def event(self, kind: str, **data: Any) -> None:
        record = {"time": dt.datetime.now(dt.UTC).isoformat(), "run": self.run_id, "event": kind, **_clip(data)}
        self._events.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
        self._events.flush()

    def write_json(self, name: str, data: Any) -> Path:
        path = self.directory / name
        path.write_text(json.dumps(data, default=str, indent=2, ensure_ascii=False))
        return path

    def close(self) -> None:
        self._events.close()

    def events(self) -> list[dict[str, Any]]:
        lines = (self.directory / "events.jsonl").read_text().splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def upload(self, storage_account: str, container: str) -> None:
        """Copy the run to blob storage with the run's managed identity (``--auth-mode login``)."""
        subprocess.run(
            [
                "az",
                "storage",
                "blob",
                "upload-batch",
                "--account-name",
                storage_account,
                "--destination",
                container,
                "--destination-path",
                self.run_id,
                "--source",
                str(self.directory),
                "--auth-mode",
                "login",
                "--only-show-errors",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )

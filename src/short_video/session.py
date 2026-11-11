from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .domain import BrandRules, MaterialStore, Templates, VideoRequest
from .miniagent import read_json, write_json


@dataclass
class Session:
    request: VideoRequest
    materials: MaterialStore
    rules: BrandRules
    templates: Templates
    state_dir: Path
    searches: list[dict[str, Any]] = field(default_factory=list)
    scripts: dict[str, dict[str, Any]] = field(default_factory=dict)
    estimates: list[dict[str, Any]] = field(default_factory=list)
    rule_checks: list[dict[str, Any]] = field(default_factory=list)
    save_attempts: list[dict[str, Any]] = field(default_factory=list)
    revision_errors: list[dict[str, Any]] = field(default_factory=list)
    finish_payload: dict[str, Any] | None = None

    def saved_script(self) -> dict[str, Any] | None:
        for attempt in reversed(self.save_attempts):
            if attempt.get("ok"):
                return attempt.get("data")
        return None

    def active_script(self) -> dict[str, Any] | None:
        # Policy records the latest draft/revise result here so report/tools agree.
        return getattr(self, "_active_script", None)

    def set_active_script(self, script: dict[str, Any]) -> None:
        self._active_script = script


class ScriptStore:
    def __init__(self, state_dir: Path):
        self.path = state_dir / "scripts.json"
        self.lock = threading.Lock()
        self.data: dict[str, Any] = {"schema_version": "1.0", "scripts": {}}
        if self.path.exists():
            loaded = read_json(self.path)
            if isinstance(loaded, dict):
                self.data.update(loaded)

    def put(self, script: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        with self.lock:
            script_id = script["script_id"]
            if script_id in self.data["scripts"]:
                return "existing", self.data["scripts"][script_id]
            record = {
                **script,
                "status": "saved",
                "editable": True,
                "published": False,
            }
            self.data["scripts"][script_id] = record
            write_json(self.path, self.data)
            return "saved", record

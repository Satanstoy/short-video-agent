from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  request_json TEXT NOT NULL,
  status TEXT NOT NULL,
  current_script_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS turns (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  user_text TEXT NOT NULL,
  assistant_text TEXT,
  intent TEXT,
  status TEXT NOT NULL,
  engine TEXT NOT NULL,
  model TEXT,
  created_at TEXT NOT NULL,
  completed_at TEXT,
  UNIQUE (task_id, seq)
);

CREATE TABLE IF NOT EXISTS tool_calls (
  id TEXT PRIMARY KEY,
  turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  tool TEXT NOT NULL,
  arguments_json TEXT NOT NULL,
  result_json TEXT,
  ok INTEGER NOT NULL,
  error_code TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (turn_id, seq)
);

CREATE TABLE IF NOT EXISTS script_versions (
  script_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  parent_script_id TEXT REFERENCES script_versions(script_id),
  version_no INTEGER NOT NULL,
  template TEXT NOT NULL,
  content_json TEXT NOT NULL,
  total_duration_sec INTEGER NOT NULL,
  status TEXT NOT NULL,
  change_intent TEXT,
  change_summary TEXT,
  created_by_turn_id TEXT REFERENCES turns(id),
  created_at TEXT NOT NULL,
  UNIQUE (task_id, version_no)
);

CREATE TABLE IF NOT EXISTS estimates (
  estimate_id TEXT PRIMARY KEY,
  script_id TEXT NOT NULL REFERENCES script_versions(script_id) ON DELETE CASCADE,
  total_duration_sec INTEGER NOT NULL,
  target_duration_sec INTEGER NOT NULL,
  within_target INTEGER NOT NULL,
  scene_durations_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_checks (
  check_id TEXT PRIMARY KEY,
  script_id TEXT NOT NULL REFERENCES script_versions(script_id) ON DELETE CASCADE,
  passed INTEGER NOT NULL,
  violations_json TEXT NOT NULL,
  warnings_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  script_id TEXT NOT NULL REFERENCES script_versions(script_id) ON DELETE CASCADE,
  decision TEXT NOT NULL,
  reason TEXT,
  turn_id TEXT REFERENCES turns(id),
  created_at TEXT NOT NULL,
  UNIQUE (task_id, script_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_turns_task_seq ON turns(task_id, seq);
CREATE INDEX IF NOT EXISTS idx_versions_task_no ON script_versions(task_id, version_no);
CREATE INDEX IF NOT EXISTS idx_estimates_script ON estimates(script_id);
CREATE INDEX IF NOT EXISTS idx_checks_script ON rule_checks(script_id);
CREATE INDEX IF NOT EXISTS idx_approvals_task ON approvals(task_id);
"""


class Storage:
    """SQLite 任务、轮次、版本、估算、检查和审核仓库。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            self.path,
            timeout=5,
            isolation_level=None,
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.lock = threading.RLock()
        self._migrate()
        self.interrupt_stale_turns()

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (1, ?)",
                (utc_now(),),
            )

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                yield
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    # tasks

    def create_task(self, request: dict[str, Any], task_id: str | None = None) -> dict[str, Any]:
        task_id = task_id or f"SV-T-{uuid4().hex[:8].upper()}"
        now = utc_now()
        with self.transaction():
            self.conn.execute(
                "INSERT INTO tasks(id, request_json, status, current_script_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, _json(request), "new", None, now, now),
            )
        task = self.get_task(task_id)
        if task is None:
            raise RuntimeError("任务创建失败")
        return task

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._task_row(row) if row else None

    def list_tasks(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY updated_at DESC", (status,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM tasks ORDER BY updated_at DESC").fetchall()
        return [self._task_row(row) for row in rows]

    def update_task(self, task_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {"status", "current_script_id"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return self.get_task(task_id)
        updates["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values()) + [task_id]
        with self.transaction():
            self.conn.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", values)
        return self.get_task(task_id)

    def restore_candidate(self, task_id: str, script_id: str) -> dict[str, Any]:
        version = self.get_version(script_id)
        if version is None or version["task_id"] != task_id:
            raise ValueError("版本不属于当前任务")
        if version["status"] not in {"validated", "approved"}:
            raise ValueError(f"v{version['version_no']} 尚未通过校验或已被驳回")
        status = "approved" if version["status"] == "approved" else "active"
        self.update_task(task_id, current_script_id=script_id, status=status)
        return version

    def _task_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "request": json.loads(row["request_json"]),
            "status": row["status"],
            "current_script_id": row["current_script_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # turns

    def start_turn(
        self,
        task_id: str,
        user_text: str,
        engine: str,
        model: str | None = None,
        intent: str | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS seq FROM turns WHERE task_id = ?", (task_id,)
            ).fetchone()
            seq = int(row["seq"]) + 1
            turn_id = f"TURN-{uuid4().hex[:10].upper()}"
            now = utc_now()
            self.conn.execute(
                "INSERT INTO turns(id, task_id, seq, user_text, assistant_text, intent, status, engine, model, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (turn_id, task_id, seq, user_text, None, intent, "running", engine, model, now),
            )
        return self.get_turn(turn_id)

    def get_turn(self, turn_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
        return dict(row) if row else None

    def finish_turn(
        self,
        turn_id: str,
        status: str,
        assistant_text: str,
        intent: str | None = None,
    ) -> None:
        with self.transaction():
            self.conn.execute(
                "UPDATE turns SET status = ?, assistant_text = ?, intent = COALESCE(?, intent), "
                "completed_at = ? WHERE id = ?",
                (status, assistant_text, intent, utc_now(), turn_id),
            )

    def list_turns(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM turns WHERE task_id = ? ORDER BY seq", (task_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def interrupt_stale_turns(self, minutes: int = 10) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
        with self.transaction():
            cursor = self.conn.execute(
                "UPDATE turns SET status = 'interrupted', completed_at = ? "
                "WHERE status = 'running' AND created_at < ?",
                (utc_now(), cutoff),
            )
        return cursor.rowcount

    def add_tool_call(
        self,
        turn_id: str,
        seq: int,
        tool: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        error_code = None
        if isinstance(result.get("error"), dict):
            error_code = result["error"].get("code")
        with self.transaction():
            self.conn.execute(
                "INSERT OR IGNORE INTO tool_calls"
                "(id, turn_id, seq, tool, arguments_json, result_json, ok, error_code, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"TC-{uuid4().hex[:10].upper()}",
                    turn_id,
                    seq,
                    tool,
                    _json(arguments),
                    _json(result),
                    1 if result.get("ok") else 0,
                    error_code,
                    utc_now(),
                ),
            )

    def list_tool_calls(self, turn_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM tool_calls WHERE turn_id = ? ORDER BY seq", (turn_id,)
        ).fetchall()
        return [
            {
                "id": row["id"],
                "turn_id": row["turn_id"],
                "seq": row["seq"],
                "tool": row["tool"],
                "arguments": json.loads(row["arguments_json"]),
                "result": json.loads(row["result_json"]) if row["result_json"] else None,
                "ok": bool(row["ok"]),
                "error_code": row["error_code"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # versions

    def next_version_no(self, task_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) AS version_no FROM script_versions WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return int(row["version_no"]) + 1

    def add_version(
        self,
        task_id: str,
        script: dict[str, Any],
        *,
        parent_script_id: str | None,
        version_no: int,
        status: str,
        change_intent: str | None = None,
        change_summary: str | None = None,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        script_id = str(script["script_id"])
        existing = self.get_version(script_id)
        if existing:
            if existing["task_id"] != task_id:
                raise ValueError(f"脚本 {script_id} 已属于其他任务")
            return existing
        with self.transaction():
            self.conn.execute(
                "INSERT INTO script_versions"
                "(script_id, task_id, parent_script_id, version_no, template, content_json, "
                " total_duration_sec, status, change_intent, change_summary, created_by_turn_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    script_id,
                    task_id,
                    parent_script_id,
                    version_no,
                    str(script.get("template") or "base"),
                    _json(script),
                    int(script.get("total_duration_sec") or 0),
                    status,
                    change_intent,
                    change_summary,
                    turn_id,
                    utc_now(),
                ),
            )
        version = self.get_version(script_id)
        if version is None:
            raise RuntimeError("版本写入失败")
        return version

    def get_version(self, script_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM script_versions WHERE script_id = ?", (script_id,)
        ).fetchone()
        return self._version_row(row) if row else None

    def list_versions(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM script_versions WHERE task_id = ? ORDER BY version_no", (task_id,)
        ).fetchall()
        return [self._version_row(row) for row in rows]

    def update_version_status(self, script_id: str, status: str) -> None:
        with self.transaction():
            self.conn.execute(
                "UPDATE script_versions SET status = ? WHERE script_id = ?", (status, script_id)
            )

    def _version_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "script_id": row["script_id"],
            "task_id": row["task_id"],
            "parent_script_id": row["parent_script_id"],
            "version_no": row["version_no"],
            "template": row["template"],
            "content": json.loads(row["content_json"]),
            "total_duration_sec": row["total_duration_sec"],
            "status": row["status"],
            "change_intent": row["change_intent"],
            "change_summary": row["change_summary"],
            "created_by_turn_id": row["created_by_turn_id"],
            "created_at": row["created_at"],
        }

    # estimates

    def add_estimate(self, script_id: str, estimate: dict[str, Any]) -> None:
        with self.transaction():
            self.conn.execute(
                "INSERT OR IGNORE INTO estimates"
                "(estimate_id, script_id, total_duration_sec, target_duration_sec, within_target, "
                " scene_durations_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    estimate["estimate_id"],
                    script_id,
                    int(estimate["total_duration_sec"]),
                    int(estimate["target_duration_sec"]),
                    1 if estimate.get("within_target") else 0,
                    _json(estimate.get("scene_durations") or []),
                    utc_now(),
                ),
            )

    def latest_estimate(self, script_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM estimates WHERE script_id = ? ORDER BY rowid DESC LIMIT 1",
            (script_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "estimate_id": row["estimate_id"],
            "script_id": row["script_id"],
            "total_duration_sec": row["total_duration_sec"],
            "target_duration_sec": row["target_duration_sec"],
            "within_target": bool(row["within_target"]),
            "scene_durations": json.loads(row["scene_durations_json"]),
            "created_at": row["created_at"],
        }

    # rule checks

    def add_rule_check(self, script_id: str, check: dict[str, Any]) -> None:
        with self.transaction():
            self.conn.execute(
                "INSERT OR IGNORE INTO rule_checks"
                "(check_id, script_id, passed, violations_json, warnings_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    check["check_id"],
                    script_id,
                    1 if check.get("passed") else 0,
                    _json(check.get("violations") or []),
                    _json(check.get("warnings") or []),
                    utc_now(),
                ),
            )

    def latest_rule_check(self, script_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM rule_checks WHERE script_id = ? ORDER BY rowid DESC LIMIT 1",
            (script_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "check_id": row["check_id"],
            "script_id": row["script_id"],
            "passed": bool(row["passed"]),
            "violations": json.loads(row["violations_json"]),
            "warnings": json.loads(row["warnings_json"]),
            "created_at": row["created_at"],
        }

    # approvals

    def add_approval(
        self,
        task_id: str,
        script_id: str,
        decision: str,
        reason: str | None = None,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM approvals WHERE task_id = ? AND script_id = ?", (task_id, script_id)
        ).fetchone()
        if row:
            return self._approval_row(row)
        approval_id = f"APR-{uuid4().hex[:10].upper()}"
        with self.transaction():
            self.conn.execute(
                "INSERT INTO approvals(id, task_id, script_id, decision, reason, turn_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (approval_id, task_id, script_id, decision, reason, turn_id, utc_now()),
            )
        return self.get_approval(task_id, script_id)  # type: ignore[return-value]

    def get_approval(self, task_id: str, script_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM approvals WHERE task_id = ? AND script_id = ?", (task_id, script_id)
        ).fetchone()
        return self._approval_row(row) if row else None

    def list_approvals(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM approvals WHERE task_id = ? ORDER BY created_at", (task_id,)
        ).fetchall()
        return [self._approval_row(row) for row in rows]

    def _approval_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "task_id": row["task_id"],
            "script_id": row["script_id"],
            "decision": row["decision"],
            "reason": row["reason"],
            "turn_id": row["turn_id"],
            "created_at": row["created_at"],
        }

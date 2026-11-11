from __future__ import annotations

from pathlib import Path
from typing import Any

from . import tools as tool_impl
from .domain import BrandRules, MaterialStore, Templates, VideoRequest
from .miniagent import Tool, ToolError, ToolResult, ToolRegistry
from .session import Session
from .storage import Storage
from .task_plan import TurnPlan


def _scene_map(content: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {int(scene["scene"]): scene for scene in content.get("scenes", [])}


def diff_scripts(from_version: dict[str, Any], to_version: dict[str, Any]) -> dict[str, Any]:
    before = _scene_map(from_version["content"])
    after = _scene_map(to_version["content"])
    scene_changes: list[dict[str, Any]] = []
    for scene_no in sorted(set(before) | set(after)):
        old = before.get(scene_no)
        new = after.get(scene_no)
        if old is None:
            scene_changes.append({"scene": scene_no, "change": "added", "to": new})
            continue
        if new is None:
            scene_changes.append({"scene": scene_no, "change": "removed", "from": old})
            continue
        fields: dict[str, Any] = {}
        for field in ("duration_sec", "narration", "visual", "claim", "source_ids"):
            if old.get(field) != new.get(field):
                fields[field] = {"from": old.get(field), "to": new.get(field)}
        if fields:
            scene_changes.append({"scene": scene_no, "change": "modified", "fields": fields})
    return {
        "from_script_id": from_version["script_id"],
        "to_script_id": to_version["script_id"],
        "from_version": from_version["version_no"],
        "to_version": to_version["version_no"],
        "duration_delta_sec": to_version["total_duration_sec"] - from_version["total_duration_sec"],
        "target_delta_sec": (
            int(to_version["content"].get("target_duration_sec") or 0)
            - int(from_version["content"].get("target_duration_sec") or 0)
        ),
        "scenes": scene_changes,
    }


class TaskRuntime:
    def __init__(
        self,
        store: Storage,
        task: dict[str, Any],
        turn_id: str,
        plan: TurnPlan,
        data_dir: str | Path = "data",
    ):
        self.store = store
        self.task = task
        self.task_id = task["id"]
        self.turn_id = turn_id
        self.plan = plan
        self.data_dir = Path(data_dir)
        self.session = self._build_session()
        self.saved_script_id: str | None = None
        self.result: dict[str, Any] | None = None
        self.change_summary = plan.summary
        self._load_current()

    def _build_session(self) -> Session:
        request = VideoRequest.from_dict(self.task["request"])
        return Session(
            request=request,
            materials=MaterialStore.load(self.data_dir),
            rules=BrandRules.load(self.data_dir),
            templates=Templates.load(self.data_dir),
            state_dir=Path(self.store.path).parent,
        )

    def _load_current(self) -> None:
        script_id = self.task.get("current_script_id")
        if script_id:
            self._load_version(script_id)

    def _load_version(self, script_id: str) -> dict[str, Any] | None:
        version = self.store.get_version(script_id)
        if version is None or version["task_id"] != self.task_id:
            return None
        self.session.scripts[script_id] = version["content"]
        self.session.set_active_script(version["content"])
        self._load_evidence(script_id)
        return version

    def _load_evidence(self, script_id: str) -> None:
        if not any(item["script_id"] == script_id for item in self.session.estimates):
            estimate = self.store.latest_estimate(script_id)
            if estimate:
                self.session.estimates.append(estimate)
        if not any(item["script_id"] == script_id for item in self.session.rule_checks):
            check = self.store.latest_rule_check(script_id)
            if check:
                self.session.rule_checks.append(check)

    def _persist_version(
        self,
        script: dict[str, Any],
        *,
        status: str = "draft",
        change_intent: str | None = None,
        change_summary: str | None = None,
    ) -> dict[str, Any]:
        return self.store.add_version(
            self.task_id,
            script,
            parent_script_id=script.get("revised_from"),
            version_no=self.store.next_version_no(self.task_id),
            status=status,
            change_intent=change_intent or self.plan.intent,
            change_summary=change_summary or self.change_summary,
            turn_id=self.turn_id,
        )

    # tools

    def get_task(self) -> dict[str, Any]:
        current = self.current_version()
        return {
            "task_id": self.task_id,
            "status": self.task["status"],
            "request": self.task["request"],
            "current_version": self._version_summary(current) if current else None,
        }

    def list_versions(self) -> list[dict[str, Any]]:
        return [self._version_summary(item) for item in self.store.list_versions(self.task_id)]

    def diff_versions(self, from_version: int, to_version: int) -> dict[str, Any]:
        versions = {item["version_no"]: item for item in self.store.list_versions(self.task_id)}
        if from_version not in versions or to_version not in versions:
            raise ToolError("UNKNOWN_VERSION", f"版本 {from_version} 或 {to_version} 不存在")
        return diff_scripts(versions[from_version], versions[to_version])

    def search_materials(self, query: str) -> dict[str, Any]:
        return tool_impl._search(self.session, query)

    def draft_script(
        self,
        topic: str,
        platform: str,
        target_duration_sec: int,
        material_ids: list[str],
        audience: str = "通用受众",
        style: str = "自然口语",
    ) -> dict[str, Any]:
        script = tool_impl._draft(
            self.session,
            topic,
            audience,
            platform,
            target_duration_sec,
            style,
            material_ids,
        )
        version = self._persist_version(
            script,
            status="draft",
            change_intent="create",
            change_summary="生成第一版",
        )
        return self._version_summary(version)

    def revise_script(
        self,
        script_id: str,
        target_duration_sec: int | None = None,
        strategy: str = "next_template",
        direction: str = "target",
        scene_patches: list[dict[str, Any]] | None = None,
        remove_source_ids: list[str] | None = None,
        focus_keywords: list[str] | None = None,
        instructions: str = "",
    ) -> ToolResult:
        if script_id not in self.session.scripts:
            version = self.store.get_version(script_id)
            if version is not None and version["task_id"] != self.task_id:
                raise ToolError("TASK_MISMATCH", "版本不属于当前任务")
            if self._load_version(script_id) is None:
                raise ToolError("UNKNOWN_SCRIPT", f"未知脚本 {script_id}")
        self._load_evidence(script_id)
        if strategy == "drop_facts":
            base = self.session.scripts[script_id]
            if not set(remove_source_ids or []) & set(base.get("material_ids", [])):
                raise ToolError("NO_CHANGE", "当前版本没有这些素材，不需要删除")
        result = tool_impl._revise(
            self.session,
            script_id,
            target_duration_sec,
            strategy=strategy,
            direction=direction,
            scene_patches=scene_patches,
            remove_source_ids=remove_source_ids,
            focus_keywords=focus_keywords,
            instructions=instructions,
        )
        if result.ok and result.data.get("script_id"):
            self.change_summary = self.plan.summary or "修改脚本"
            self._persist_version(
                result.data,
                status="draft",
                change_intent=self.plan.intent,
                change_summary=self.change_summary,
            )
        return result

    def estimate_duration(self, script_id: str) -> dict[str, Any]:
        result = tool_impl._estimate(self.session, script_id)
        self.store.add_estimate(script_id, result)
        return result

    def check_rules(self, script_id: str) -> dict[str, Any]:
        result = tool_impl._check(self.session, script_id)
        self.store.add_rule_check(script_id, result)
        return result

    def save_script(self, script_id: str) -> dict[str, Any]:
        script = self.session.scripts.get(script_id)
        if script is None:
            raise ToolError("UNKNOWN_SCRIPT", f"未知脚本 {script_id}")
        estimate = self.store.latest_estimate(script_id)
        check = self.store.latest_rule_check(script_id)
        if estimate is None or not estimate.get("within_target"):
            raise ToolError("DURATION_NOT_VERIFIED", "保存前必须先估算且满足目标时长")
        if check is None or not check.get("passed"):
            raise ToolError("RULE_CHECK_REQUIRED", "保存前必须通过品牌规则检查")
        version = self.store.get_version(script_id)
        if version is None:
            raise ToolError("UNKNOWN_SCRIPT", f"未知脚本 {script_id}")
        if version["status"] == "rejected":
            raise ToolError("VERSION_REJECTED", "已驳回的版本不能重新保存")
        if version["status"] in {"validated", "approved"}:
            status = "existing"
        else:
            self.store.update_version_status(script_id, "validated")
            status = "saved"
        task_status = "approved" if version["status"] == "approved" else "active"
        self.store.update_task(self.task_id, current_script_id=script_id, status=task_status)
        self.task = self.store.get_task(self.task_id) or self.task
        data = {
            "script_id": script_id,
            "save_status": status,
            "status": "validated",
            "version_no": version["version_no"],
            "total_duration_sec": version["total_duration_sec"],
            "template": version["template"],
        }
        self.saved_script_id = script_id
        self.session.save_attempts.append({"ok": True, "data": data})
        return data

    def rollback_version(self, script_id: str) -> dict[str, Any]:
        version = self.store.get_version(script_id)
        if version is None or version["task_id"] != self.task_id:
            raise ToolError("TASK_MISMATCH", "版本不属于当前任务")
        try:
            version = self.store.restore_candidate(self.task_id, script_id)
        except ValueError as exc:
            raise ToolError("INVALID_ROLLBACK_VERSION", str(exc)) from exc
        self.task = self.store.get_task(self.task_id) or self.task
        self.change_summary = f"回退到 v{version['version_no']}"
        return self._version_summary(version)

    def finish(
        self,
        status: str,
        summary: str,
        risks: list[Any] | None = None,
        gap: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        allowed = {
            "completed",
            "answered",
            "needs_clarification",
            "needs_input",
            "no_material",
            "cannot_meet_duration",
            "rule_failed",
            "tool_error",
            "save_failed",
        }
        if status not in allowed:
            raise ToolError("INVALID_STATUS", f"status 必须是 {sorted(allowed)} 之一")
        if status == "completed":
            script_id = self.saved_script_id
            if not script_id:
                raise ToolError("EVIDENCE_MISMATCH", "本轮没有保存候选，不能标记 completed")
            estimate = self.store.latest_estimate(script_id)
            check = self.store.latest_rule_check(script_id)
            if estimate is None or not estimate.get("within_target"):
                raise ToolError("EVIDENCE_MISMATCH", "缺少满足目标时长的估算")
            if check is None or not check.get("passed"):
                raise ToolError("EVIDENCE_MISMATCH", "缺少通过的规则检查")
        if status == "cannot_meet_duration":
            if not self.session.revision_errors:
                raise ToolError("EVIDENCE_MISMATCH", "缺少压缩失败的证据")
        if status == "rule_failed":
            script_id = self.session.active_script()["script_id"] if self.session.active_script() else None
            check = self.store.latest_rule_check(script_id) if script_id else None
            if check is None or check.get("passed"):
                raise ToolError("EVIDENCE_MISMATCH", "缺少规则检查失败的证据")
        if status == "no_material":
            if self.session.active_script():
                raise ToolError("EVIDENCE_MISMATCH", "已有脚本，不能标记 no_material")
            if not self.session.searches or self.session.searches[-1].get("hits"):
                raise ToolError("EVIDENCE_MISMATCH", "缺少无素材的检索证据")
        payload = {"status": status, "summary": summary, "risks": risks or [], "gap": gap}
        self.result = payload
        return payload

    # views

    def current_version(self) -> dict[str, Any] | None:
        task = self.store.get_task(self.task_id) or self.task
        script_id = task.get("current_script_id")
        return self.store.get_version(script_id) if script_id else None

    def version(self, script_id: str) -> dict[str, Any] | None:
        return self.store.get_version(script_id)

    def _version_summary(self, version: dict[str, Any] | None) -> dict[str, Any] | None:
        if version is None:
            return None
        return {
            "script_id": version["script_id"],
            "version_no": version["version_no"],
            "parent_script_id": version["parent_script_id"],
            "template": version["template"],
            "total_duration_sec": version["total_duration_sec"],
            "status": version["status"],
            "change_intent": version["change_intent"],
            "change_summary": version["change_summary"],
        }

    def render_response(self) -> str:
        if self.result is None:
            return "本轮没有产生最终状态，请查看历史记录。"
        status = self.result["status"]
        summary = self.result.get("summary") or ""
        if status in {"answered", "needs_clarification"}:
            return summary
        if status == "completed":
            script_id = self.saved_script_id or (self.current_version() or {}).get("script_id")
            version = self.store.get_version(script_id) if script_id else None
            if version is None:
                return summary
            estimate = self.store.latest_estimate(script_id)
            check = self.store.latest_rule_check(script_id)
            lines = [
                f"v{version['version_no']} 已存候选：{version['total_duration_sec']} 秒，"
                f"模板 {version['template']}，规则检查通过。"
            ]
            if self.change_summary:
                lines.append(f"改动：{self.change_summary}。")
            if estimate and check:
                warnings = check.get("warnings") or []
                if warnings:
                    lines.append(f"提示：{'；'.join(warnings)}。")
            return "\n".join(lines)
        if status == "cannot_meet_duration":
            gap = self.result.get("gap") or {}
            return (
                f"{summary} 目标 {gap.get('target_duration_sec')} 秒，"
                f"最短 {gap.get('min_duration_sec')} 秒，差 {gap.get('over_by_sec')} 秒。"
            )
        if status == "rule_failed":
            risks = self.result.get("risks") or []
            return f"{summary} 违规项：{risks}"
        return summary

    def build_registry(self, faults: dict[str, int] | None = None) -> ToolRegistry:
        tools = [
            Tool(
                name="get_task",
                description="读取当前任务、请求字段和当前版本摘要。",
                parameters={"type": "object", "properties": {}, "required": []},
                handler=lambda: self.get_task(),
            ),
            Tool(
                name="list_versions",
                description="列出当前任务的所有版本摘要。",
                parameters={"type": "object", "properties": {}, "required": []},
                handler=lambda: {"versions": self.list_versions()},
            ),
            Tool(
                name="diff_versions",
                description="比较两个版本的时长、分镜和声明变化。",
                parameters={
                    "type": "object",
                    "properties": {
                        "from_version": {"type": "integer"},
                        "to_version": {"type": "integer"},
                    },
                    "required": ["from_version", "to_version"],
                },
                handler=lambda from_version, to_version: self.diff_versions(
                    int(from_version), int(to_version)
                ),
            ),
            Tool(
                name="search_materials",
                description="检索产品素材事实，返回素材 ID 与可引用事实。",
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                handler=lambda query: ToolResult.success(
                    "素材检索完成", self.search_materials(query)
                ),
            ),
            Tool(
                name="draft_script",
                description="根据主题、平台、目标时长和素材生成第一版分镜脚本。",
                parameters={
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string"},
                        "audience": {"type": "string"},
                        "platform": {"type": "string"},
                        "target_duration_sec": {"type": "integer"},
                        "style": {"type": "string"},
                        "material_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["topic", "platform", "target_duration_sec", "material_ids"],
                },
                handler=lambda topic, platform, target_duration_sec, material_ids, audience="通用受众", style="自然口语": self.draft_script(
                    topic,
                    platform,
                    int(target_duration_sec),
                    list(material_ids),
                    audience,
                    style,
                ),
            ),
            Tool(
                name="revise_script",
                description="基于当前脚本做增量修改：换模板、改分镜、删事实或重新生成。",
                parameters={
                    "type": "object",
                    "properties": {
                        "script_id": {"type": "string"},
                        "target_duration_sec": {"type": "integer"},
                        "strategy": {
                            "type": "string",
                            "enum": ["next_template", "patch_scenes", "drop_facts", "redraft"],
                        },
                        "direction": {
                            "type": "string",
                            "enum": ["target", "shorter", "longer"],
                        },
                        "scene_patches": {"type": "array", "items": {"type": "object"}},
                        "remove_source_ids": {"type": "array", "items": {"type": "string"}},
                        "focus_keywords": {"type": "array", "items": {"type": "string"}},
                        "instructions": {"type": "string"},
                    },
                    "required": ["script_id"],
                },
                handler=lambda script_id, target_duration_sec=None, strategy="next_template", direction="target", scene_patches=None, remove_source_ids=None, focus_keywords=None, instructions="": self.revise_script(
                    script_id,
                    int(target_duration_sec) if target_duration_sec is not None else None,
                    strategy,
                    direction,
                    scene_patches,
                    remove_source_ids,
                    focus_keywords,
                    instructions,
                ),
            ),
            Tool(
                name="estimate_duration",
                description="按分镜逐项计算预计总时长，并判断是否超过目标时长。",
                parameters={
                    "type": "object",
                    "properties": {"script_id": {"type": "string"}},
                    "required": ["script_id"],
                },
                handler=lambda script_id: ToolResult.success(
                    "时长估算完成", self.estimate_duration(script_id)
                ),
            ),
            Tool(
                name="check_rules",
                description="检查品牌禁词、事实来源、素材 ID、旁白断言和时长上下限。",
                parameters={
                    "type": "object",
                    "properties": {"script_id": {"type": "string"}},
                    "required": ["script_id"],
                },
                handler=lambda script_id: ToolResult.success(
                    "规则检查完成", self.check_rules(script_id)
                ),
            ),
            Tool(
                name="save_script",
                description="把已通过估算和规则检查的版本存为候选。",
                parameters={
                    "type": "object",
                    "properties": {"script_id": {"type": "string"}},
                    "required": ["script_id"],
                },
                handler=lambda script_id: ToolResult.success(
                    "候选版本已保存", self.save_script(script_id)
                ),
            ),
            Tool(
                name="rollback_version",
                description="把当前版本指针回退到指定历史版本，不删除历史。",
                parameters={
                    "type": "object",
                    "properties": {"script_id": {"type": "string"}},
                    "required": ["script_id"],
                },
                handler=lambda script_id: ToolResult.success(
                    "已回退版本", self.rollback_version(script_id)
                ),
            ),
            Tool(
                name="finish",
                description="结束本轮。completed 必须有候选、达标估算和通过的规则检查。",
                parameters={
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": [
                                "completed",
                                "answered",
                                "needs_clarification",
                                "needs_input",
                                "no_material",
                                "cannot_meet_duration",
                                "rule_failed",
                                "tool_error",
                                "save_failed",
                            ],
                        },
                        "summary": {"type": "string"},
                        "risks": {"type": "array"},
                        "gap": {"type": "object"},
                    },
                    "required": ["status", "summary"],
                },
                handler=lambda status, summary, risks=None, gap=None: ToolResult.success(
                    "任务结束", self.finish(status, summary, risks, gap)
                ),
            ),
        ]
        return ToolRegistry(tools, faults=faults)

from __future__ import annotations

from typing import Any

from .miniagent import OFFLINE_NOTE, EngineFailure, TextOnly, ToolCall, ToolCalls
from .session import Session


def _results(state: Any, name: str) -> list[Any]:
    return [exchange for exchange in state.transcript if exchange.call.name == name]


def _script_events(state: Any) -> list[Any]:
    return [
        exchange
        for exchange in state.transcript
        if exchange.call.name in {"draft_script", "revise_script"}
    ]


def _active_script(state: Any) -> dict[str, Any] | None:
    successful = [
        exchange.result.data
        for exchange in _script_events(state)
        if exchange.result.ok and exchange.result.data.get("script_id")
    ]
    return successful[-1] if successful else None


class ScriptProgression:
    """从已有脚本开始，推进估算、修正、规则检查、保存和收尾。"""

    def __init__(self, session: Session):
        self.session = session

    def _call(self, name: str, arguments: dict[str, Any], reason: str) -> ToolCalls:
        return ToolCalls([ToolCall(name=name, arguments=arguments, reason=reason)])

    def next_action(self, state: Any) -> ToolCalls | TextOnly | EngineFailure:
        request = state.request
        active = _active_script(state)
        if active is None:
            return self._call(
                "finish",
                {
                    "status": "tool_error",
                    "summary": "没有可用的脚本对象。",
                    "gap": {"code": "NO_SCRIPT"},
                },
                "状态不一致，停止。",
            )
        script_id = active["script_id"]

        estimates = [
            item
            for item in _results(state, "estimate_duration")
            if item.call.arguments.get("script_id") == script_id
        ]
        if not estimates:
            return self._call(
                "estimate_duration",
                {"script_id": script_id},
                "按分镜估算总时长。",
            )
        if not estimates[-1].result.ok:
            if len(estimates) == 1:
                return self._call(
                    "estimate_duration",
                    {"script_id": script_id},
                    "时长估算失败，重试一次。",
                )
            return self._call(
                "finish",
                {
                    "status": "tool_error",
                    "summary": "时长估算连续失败，脚本初稿保留，但无法确认是否满足时长。",
                    "gap": {"code": "ESTIMATE_FAILED", "script_id": script_id},
                },
                "无法核验时长，不能声称完成。",
            )
        estimate = estimates[-1].result.data
        if not estimate.get("within_target"):
            revision_errors = [
                item for item in self.session.revision_errors if item.get("script_id") == script_id
            ]
            if revision_errors:
                total = int(estimate["total_duration_sec"])
                target = int(estimate["target_duration_sec"])
                return self._call(
                    "finish",
                    {
                        "status": "cannot_meet_duration",
                        "summary": (
                            f"脚本最短可压缩到 {revision_errors[-1]['min_duration_sec']} 秒，"
                            f"目标 {target} 秒，无法满足。"
                        ),
                        "risks": ["压缩后仍超出目标时长，未保存脚本"],
                        "gap": {
                            "code": "DURATION",
                            "target_duration_sec": target,
                            "min_duration_sec": revision_errors[-1]["min_duration_sec"],
                            "over_by_sec": max(0, int(revision_errors[-1]["min_duration_sec"]) - target),
                            "current_total_sec": total,
                        },
                    },
                    "压缩到最短仍超时长，明确失败。",
                )
            revisions = [
                item
                for item in _results(state, "revise_script")
                if item.call.arguments.get("script_id") == script_id
            ]
            if not revisions:
                return self._call(
                    "revise_script",
                    {"script_id": script_id, "target_duration_sec": request.target_duration_sec},
                    f"初稿 {estimate['total_duration_sec']} 秒超过目标，压缩脚本。",
                )
            if not revisions[-1].result.ok:
                if len(revisions) == 1:
                    return self._call(
                        "revise_script",
                        {"script_id": script_id, "target_duration_sec": request.target_duration_sec},
                        "压缩失败，重试一次。",
                    )
                return self._call(
                    "finish",
                    {
                        "status": "tool_error",
                        "summary": "脚本压缩连续失败，初稿保留但未达标。",
                        "gap": {"code": "REVISE_FAILED"},
                    },
                    "压缩工具失败，停止。",
                )
            # A successful revision creates a new script id; the next loop
            # estimates that new version.
            return self._call(
                "estimate_duration",
                {"script_id": _active_script(state)["script_id"]},
                "压缩后重新估算时长。",
            )

        checks = [
            item
            for item in _results(state, "check_rules")
            if item.call.arguments.get("script_id") == script_id
        ]
        if not checks:
            return self._call(
                "check_rules",
                {"script_id": script_id},
                "时长达标，检查品牌规则和事实来源。",
            )
        if not checks[-1].result.ok:
            if len(checks) == 1:
                return self._call(
                    "check_rules",
                    {"script_id": script_id},
                    "规则检查失败，重试一次。",
                )
            return self._call(
                "finish",
                {
                    "status": "tool_error",
                    "summary": "规则检查连续失败，脚本未保存。",
                    "gap": {"code": "CHECK_FAILED"},
                },
                "无法核验规则，不能声称完成。",
            )
        check = checks[-1].result.data
        if not check.get("passed"):
            return self._call(
                "finish",
                {
                    "status": "rule_failed",
                    "summary": "脚本未通过品牌规则检查，未保存。",
                    "risks": check.get("violations", []),
                    "gap": {"code": "RULE_FAILED"},
                },
                "规则不通过，明确失败并列出违规项。",
            )

        saves = [
            item
            for item in _results(state, "save_script")
            if item.call.arguments.get("script_id") == script_id
        ]
        if not saves:
            return self._call(
                "save_script",
                {"script_id": script_id},
                "时长和规则都通过，保存可编辑脚本。",
            )
        if not saves[-1].result.ok:
            if len(saves) == 1:
                return self._call(
                    "save_script",
                    {"script_id": script_id},
                    "保存失败，重试一次。",
                )
            self.session.save_attempts.append({"ok": False, "error": saves[-1].result.code})
            return self._call(
                "finish",
                {
                    "status": "save_failed",
                    "summary": "脚本保存连续失败，生成和检查结果保留，但没有可交付的脚本记录。",
                    "gap": {"code": "SAVE_FAILED", "script_id": script_id},
                },
                "保存失败，不能虚报完成。",
            )
        return self._call(
            "finish",
            {
                "status": "completed",
                "summary": (
                    f"脚本 {script_id} 已保存，总时长 {estimate['total_duration_sec']} 秒，"
                    f"目标 {request.target_duration_sec} 秒，规则检查通过。"
                ),
                "risks": check.get("warnings", []),
            },
            "脚本已保存且检查通过，汇报结果。",
        )


class OfflineEngine:
    name = "offline-reference"
    model = "reference-policy"
    mode_note = OFFLINE_NOTE
    prompt_hash = "offline-reference-policy"

    def __init__(self, session: Session):
        self.session = session
        self.progression = ScriptProgression(session)

    def _call(self, name: str, arguments: dict[str, Any], reason: str) -> ToolCalls:
        return ToolCalls([ToolCall(name=name, arguments=arguments, reason=reason)])

    def next_action(self, state: Any) -> ToolCalls | TextOnly | EngineFailure:
        request = state.request
        if request.needs_input:
            return self._call(
                "finish",
                {
                    "status": "needs_input",
                    "summary": f"缺少关键信息：{', '.join(request.missing_fields)}；未生成脚本。",
                    "gap": {"code": "MISSING_INPUT", "fields": list(request.missing_fields)},
                },
                "主题、平台或目标时长不完整，先返回缺口。",
            )

        searches = _results(state, "search_materials")
        if not searches:
            return self._call(
                "search_materials",
                {"query": request.topic},
                "先检索产品素材事实。",
            )
        if not searches[-1].result.ok:
            if len(searches) == 1:
                return self._call(
                    "search_materials",
                    {"query": f"{request.topic} 视频"},
                    "检索失败，改写查询重试一次。",
                )
            return self._call(
                "finish",
                {
                    "status": "tool_error",
                    "summary": "素材检索连续失败，未生成脚本。",
                    "gap": {"code": "SEARCH_FAILED"},
                },
                "连续检索失败，停止而不是编造素材。",
            )

        hits = searches[-1].result.data.get("hits") or []
        if not hits:
            if len(searches) == 1:
                return self._call(
                    "search_materials",
                    {"query": f"{request.topic} 视频"},
                    "没有检索结果，改写查询重试。",
                )
            return self._call(
                "finish",
                {
                    "status": "no_material",
                    "summary": f"素材库没有与“{request.topic}”相关的事实，未生成脚本。",
                    "gap": {"code": "NO_MATERIAL", "topic": request.topic},
                },
                "没有素材依据，不能编造产品事实。",
            )

        material_ids = [hit["id"] for hit in hits]
        drafts = _results(state, "draft_script")
        if not drafts:
            return self._call(
                "draft_script",
                {
                    "topic": request.topic,
                    "audience": request.audience,
                    "platform": request.platform,
                    "target_duration_sec": request.target_duration_sec,
                    "style": request.style,
                    "material_ids": material_ids,
                },
                "用检索到的素材事实生成脚本初稿。",
            )
        if not drafts[-1].result.ok:
            if len(drafts) == 1:
                return self._call(
                    "draft_script",
                    {
                        "topic": request.topic,
                        "audience": request.audience,
                        "platform": request.platform,
                        "target_duration_sec": request.target_duration_sec,
                        "style": request.style,
                        "material_ids": material_ids,
                    },
                    "脚本初稿生成失败，重试一次。",
                )
            return self._call(
                "finish",
                {
                    "status": "tool_error",
                    "summary": "脚本生成连续失败，未保存。",
                    "gap": {"code": "DRAFT_FAILED"},
                },
                "生成工具连续失败，停止。",
            )

        return self.progression.next_action(state)

    def record_tool_results(self, exchanges: list[Any]) -> None:
        return None

    def record_nudge(self, message: str) -> None:
        return None

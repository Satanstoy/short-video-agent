from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .domain import BrandRules, MaterialStore, Templates, VideoRequest
from .miniagent import (
    Limits,
    OFFLINE_NOTE,
    OpenAIEngine,
    RunStatus,
    TextOnly,
    ToolCall,
    ToolCalls,
    ToolExchange,
    TraceRecorder,
    run_loop,
)
from .policy import OfflineEngine, ScriptProgression, _results
from .session import Session
from .storage import Storage
from .task_plan import TurnPlan, parse_intent
from .task_runtime import TaskRuntime


@dataclass
class TaskState:
    request: VideoRequest
    steps: int = 0
    tool_calls: int = 0
    transcript: list[ToolExchange] = field(default_factory=list)
    finished: bool = False


class PersistingRegistry:
    """把每次工具调用写进 SQLite，同时保持 ToolRegistry 的接口。"""

    def __init__(self, inner: Any, store: Storage, turn_id: str):
        self.inner = inner
        self.store = store
        self.turn_id = turn_id
        self.seq = 0

    def schemas(self) -> list[dict[str, Any]]:
        return self.inner.schemas()

    def names(self) -> list[str]:
        return self.inner.names()

    def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        result = self.inner.execute(name, arguments)
        self.seq += 1
        self.store.add_tool_call(
            self.turn_id,
            self.seq,
            name,
            arguments,
            result.to_dict() if hasattr(result, "to_dict") else {"ok": bool(result)},
        )
        return result


def _parse_session(task: dict[str, Any], data_dir: str | Path) -> Session:
    return Session(
        request=VideoRequest.from_dict(task["request"]),
        materials=MaterialStore.load(data_dir),
        rules=BrandRules.load(data_dir),
        templates=Templates.load(data_dir),
        state_dir=Path(data_dir),
    )


def _answer_question(runtime: TaskRuntime, question: str) -> str:
    version = runtime.current_version()
    if version is None:
        return "当前还没有脚本，先生成一版再问。"
    content = version["content"]
    estimate = runtime.store.latest_estimate(version["script_id"])
    check = runtime.store.latest_rule_check(version["script_id"])
    total = version["total_duration_sec"]
    target = int(content.get("target_duration_sec") or 0)
    if "为什么" in question and ("28" in question or "时长" in question or "短" in question):
        return (
            f"当前 v{version['version_no']} 是 {total} 秒，目标 {target} 秒。"
            "28 秒是压缩模板的下限，再压会丢掉产品事实或行动引导。"
        )
    if "风险" in question or "违规" in question:
        warnings = (check or {}).get("warnings") or []
        violations = (check or {}).get("violations") or []
        if violations:
            return f"当前 v{version['version_no']} 有 {len(violations)} 条违规：{violations}"
        return f"当前 v{version['version_no']} 规则通过；提示：{warnings or '无'}。"
    return (
        f"当前 v{version['version_no']}：{total} 秒，目标 {target} 秒，"
        f"模板 {version['template']}，状态 {version['status']}。"
    )


class TaskOfflineEngine:
    name = "offline-task"
    model = "reference-policy"
    mode_note = OFFLINE_NOTE
    prompt_hash = "offline-task-policy"

    def __init__(self, runtime: TaskRuntime, plan: TurnPlan):
        self.runtime = runtime
        self.plan = plan
        self.progression = ScriptProgression(runtime.session)
        self.create_engine = OfflineEngine(runtime.session)

    def _call(self, name: str, arguments: dict[str, Any], reason: str = "") -> ToolCalls:
        return ToolCalls([ToolCall(name=name, arguments=arguments, reason=reason)])

    def next_action(self, state: Any) -> ToolCalls | TextOnly:
        plan = self.plan
        if plan.intent in {"question", "approve_suggestion"}:
            summary = plan.summary
            if plan.intent == "question":
                summary = _answer_question(self.runtime, plan.summary)
            return self._call("finish", {"status": "answered", "summary": summary})

        if plan.intent == "unknown":
            return self._call(
                "finish",
                {
                    "status": "needs_clarification",
                    "summary": plan.clarification or "你想改哪一部分？",
                },
            )

        if plan.intent == "rollback":
            rollbacks = _results(state, "rollback_version")
            if not rollbacks:
                if not plan.target_script_id:
                    return self._call(
                        "finish",
                        {"status": "needs_clarification", "summary": "要回到哪一版？"},
                    )
                return self._call(
                    "rollback_version",
                    {"script_id": plan.target_script_id},
                    "把当前版本指针回退到旧版本。",
                )
            if not rollbacks[-1].result.ok:
                return self._call(
                    "finish",
                    {
                        "status": "tool_error",
                        "summary": f"回退失败：{rollbacks[-1].result.summary}",
                        "gap": {"code": rollbacks[-1].result.code},
                    },
                )
            return self._call(
                "finish",
                {"status": "answered", "summary": self.runtime.change_summary or "已回退版本。"},
            )

        if plan.intent == "create":
            return self.create_engine.next_action(state)

        if plan.intent != "revise":
            return self._call(
                "finish",
                {"status": "needs_clarification", "summary": "这一轮想做什么？"},
            )

        if plan.strategy == "next_template" and plan.direction in {"shorter", "longer"}:
            version = self.runtime.current_version()
            if version:
                current_total = int(version["total_duration_sec"])
                totals = [
                    sum(int(item["duration_sec"]) for item in template)
                    for template in (
                        self.runtime.session.templates.base,
                        self.runtime.session.templates.compact,
                        self.runtime.session.templates.compressed,
                    )
                    if template
                ]
                if plan.direction == "shorter" and totals and current_total <= min(totals):
                    return self._call(
                        "finish",
                        {
                            "status": "answered",
                            "summary": (
                                f"当前 v{version['version_no']} 是 {current_total} 秒，"
                                "已经是最短模板；再压会丢掉产品事实或行动引导。"
                            ),
                        },
                    )
                if plan.direction == "longer" and totals and current_total >= max(totals):
                    return self._call(
                        "finish",
                        {
                            "status": "answered",
                            "summary": (
                                f"当前 v{version['version_no']} 是 {current_total} 秒，"
                                "已经是最长模板，无法再加长。"
                            ),
                        },
                    )

        revisions = _results(state, "revise_script")
        if not revisions:
            script_id = (self.runtime.current_version() or {}).get("script_id")
            if not script_id:
                return self._call(
                    "finish",
                    {"status": "tool_error", "summary": "当前没有可修改的脚本。"},
                )
            arguments: dict[str, Any] = {
                "script_id": script_id,
                "strategy": plan.strategy or "next_template",
                "direction": plan.direction or "target",
                "instructions": plan.instructions,
            }
            if plan.target_duration_sec is not None:
                arguments["target_duration_sec"] = plan.target_duration_sec
            if plan.scene_patches:
                arguments["scene_patches"] = plan.scene_patches
            if plan.remove_source_ids:
                arguments["remove_source_ids"] = plan.remove_source_ids
            if plan.focus_keywords:
                arguments["focus_keywords"] = plan.focus_keywords
            return self._call("revise_script", arguments, plan.summary)

        last = revisions[-1]
        if not last.result.ok:
            code = last.result.code
            data = last.result.data or {}
            if code == "CANNOT_COMPRESS":
                target = int(data.get("target_duration_sec") or 0)
                minimum = int(data.get("min_duration_sec") or 0)
                return self._call(
                    "finish",
                    {
                        "status": "cannot_meet_duration",
                        "summary": f"最短可到 {minimum} 秒，无法满足 {target} 秒。",
                        "risks": ["压缩到最短仍超目标，未保存新版本"],
                        "gap": {
                            "code": "DURATION",
                            "target_duration_sec": target,
                            "min_duration_sec": minimum,
                            "over_by_sec": max(0, minimum - target),
                        },
                    },
                )
            if code == "CANNOT_EXTEND":
                return self._call(
                    "finish",
                    {
                        "status": "answered",
                        "summary": f"当前已经是最长模板，无法再加长：{last.result.summary}",
                    },
                )
            if code in {"NO_FACT", "NO_CHANGE"}:
                return self._call(
                    "finish",
                    {
                        "status": "answered",
                        "summary": last.result.summary,
                    },
                )
            return self._call(
                "finish",
                {
                    "status": "tool_error",
                    "summary": f"修改失败：{last.result.summary}",
                    "gap": {"code": code},
                },
            )

        return self.progression.next_action(state)

    def record_tool_results(self, exchanges: list[Any]) -> None:
        return None

    def record_nudge(self, message: str) -> None:
        return None


def _live_prompt(runtime: TaskRuntime, user_text: str) -> tuple[str, str]:
    current = runtime.current_version()
    versions = runtime.store.list_versions(runtime.task_id)[-5:]
    recent_turns = runtime.store.list_turns(runtime.task_id)[-4:-1]
    system = (
        "你是短视频脚本 Agent，服务内容运营。"
        "你只能通过工具读写状态，不能编造素材事实，不能自己批准版本。"
        "每轮先理解用户要改什么，再选择工具；失败要如实说明。"
        "如果用户意图不明确，调用 finish(needs_clarification) 并在 summary 中只问一个问题。"
        "保存前要重新估算并检查规则，只有成功保存后才能调用 finish(completed)。"
    )
    lines = [
        f"任务：{runtime.task['request']}",
        f"当前版本：{current['version_no'] if current else '无'}",
        f"历史版本：{[(item['version_no'], item['total_duration_sec'], item['status']) for item in versions]}",
    ]
    if current:
        lines.append(f"当前分镜：{current['content'].get('scenes', [])}")
    for turn in recent_turns:
        lines.append(f"此前用户：{turn['user_text'][:600]}\n此前回复：{(turn['assistant_text'] or '')[:600]}")
    lines.append(f"用户消息：{user_text}")
    return system, "\n".join(lines)


def run_task_turn(
    store: Storage,
    task_id: str,
    user_text: str,
    *,
    engine_name: str = "offline",
    data_dir: str | Path = "data",
    force_intent: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    task = store.get_task(task_id)
    if task is None:
        raise ValueError(f"任务 {task_id} 不存在")
    versions = store.list_versions(task_id)
    current = store.get_version(task["current_script_id"]) if task.get("current_script_id") else None
    if force_intent == "create":
        plan = TurnPlan(intent="create", summary="生成第一版")
    else:
        plan = parse_intent(
            user_text,
            task=task,
            current=current,
            versions=versions,
            session=_parse_session(task, data_dir),
        )
    if engine_name == "live":
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:
            pass
        model = model or os.getenv("OPENAI_MODEL")
    elif engine_name != "offline":
        raise ValueError(f"未知 engine：{engine_name}")
    turn = store.start_turn(task_id, user_text, engine_name, model, plan.intent)
    runtime = TaskRuntime(store, task, turn["id"], plan, data_dir)
    registry = PersistingRegistry(runtime.build_registry(), store, turn["id"])
    state = TaskState(request=runtime.session.request)
    trace = TraceRecorder(
        run_id=turn["id"],
        engine=engine_name,
        model=model or "reference-policy",
        request=runtime.session.request.to_dict(),
        limits=Limits().to_dict(),
        mode_note=OFFLINE_NOTE if engine_name == "offline" else "",
    )

    if engine_name == "offline":
        engine: Any = TaskOfflineEngine(runtime, plan)
    else:
        try:
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key or not model:
                raise RuntimeError("live 模式需要 OPENAI_API_KEY 和 OPENAI_MODEL")
            system_prompt, user_prompt = _live_prompt(runtime, user_text)
            engine = OpenAIEngine(
                api_key=api_key,
                base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                registry=registry,
            )
        except Exception:
            store.finish_turn(turn["id"], "failed", "live 模式初始化失败。", plan.intent)
            raise

    started = time.perf_counter()
    run_status: RunStatus = run_loop(
        engine=engine,
        registry=registry,
        state=state,
        trace=trace,
        limits=Limits(),
    )
    result_status = (runtime.result or {}).get("status")
    if result_status in {"needs_clarification", "needs_input"}:
        turn_status = "needs_clarification"
    elif run_status.status == "completed":
        turn_status = "completed"
    elif run_status.status == "failed":
        turn_status = "failed"
    else:
        turn_status = "incomplete"
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    assistant_text = runtime.render_response()
    if not runtime.result:
        assistant_text = f"本轮未完成（{run_status.stop_reason}）。"
    store.finish_turn(turn["id"], turn_status, assistant_text, plan.intent)
    return {
        "task_id": task_id,
        "turn_id": turn["id"],
        "intent": plan.intent,
        "turn_status": turn_status,
        "run_status": run_status,
        "elapsed_ms": elapsed_ms,
        "result": runtime.result,
        "assistant_text": assistant_text,
        "runtime": runtime,
    }

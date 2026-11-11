"""A deliberately small tool-calling loop shared by the three demo projects.

No framework. The model can only pick registered tools; the loop records every
decision and result; the report is built from recorded tool evidence.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Protocol
from uuid import uuid4

OFFLINE_NOTE = "离线参考策略仅验证流程、状态和护栏，不代表真实模型决策效果。"


class ToolError(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    id: str = ""


@dataclass
class ToolResult:
    ok: bool
    code: str = "OK"
    summary: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None

    @classmethod
    def success(cls, summary: str, data: dict[str, Any] | None = None, code: str = "OK") -> "ToolResult":
        return cls(ok=True, code=code, summary=summary, data=data or {})

    @classmethod
    def failure(
        cls,
        code: str,
        message: str,
        retryable: bool = False,
        data: dict[str, Any] | None = None,
    ) -> "ToolResult":
        return cls(
            ok=False,
            code=code,
            summary=message,
            data=data or {},
            error={"code": code, "message": message, "retryable": retryable},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "summary": self.summary,
            "data": self.data,
            "error": self.error,
        }


@dataclass
class ToolExchange:
    call: ToolCall
    result: ToolResult
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.call.name,
            "arguments": self.call.arguments,
            "result": self.result.to_dict(),
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., ToolResult | dict[str, Any]]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self, tools: list[Tool], faults: dict[str, int] | None = None):
        self._tools = {tool.name: tool for tool in tools}
        self.faults = dict(faults or {})

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if name not in self._tools:
            return ToolResult.failure(
                "UNKNOWN_TOOL",
                f"未知工具 {name}；可用工具：{', '.join(self.names())}",
            )
        if not isinstance(arguments, dict):
            return ToolResult.failure("INVALID_ARGUMENTS", "工具参数必须是 JSON 对象")
        if self.faults.get(name, 0) > 0:
            self.faults[name] -= 1
            return ToolResult.failure(
                "INJECTED_FAILURE",
                f"注入的工具失败：{name}",
                retryable=True,
            )
        tool = self._tools[name]
        required = tool.parameters.get("required", [])
        missing = [key for key in required if key not in arguments]
        if missing:
            return ToolResult.failure(
                "INVALID_ARGUMENTS",
                f"{name} 缺少参数：{', '.join(missing)}",
            )
        try:
            result = tool.handler(**arguments)
        except ToolError as exc:
            return ToolResult.failure(exc.code, exc.message, exc.retryable)
        except TypeError as exc:
            return ToolResult.failure("INVALID_ARGUMENTS", f"{name} 参数错误：{exc}")
        except Exception as exc:  # tool boundary must not crash the loop
            return ToolResult.failure("TOOL_EXCEPTION", f"{name} 执行异常：{exc}")
        if isinstance(result, ToolResult):
            return result
        if isinstance(result, dict):
            return ToolResult.success(f"{name} 完成", result)
        return ToolResult.failure("INVALID_TOOL_RESULT", f"{name} 返回了不支持的结果类型")


class TraceRecorder:
    def __init__(
        self,
        *,
        run_id: str,
        engine: str,
        model: str,
        request: dict[str, Any],
        limits: dict[str, int],
        mode_note: str = "",
        prompt_hash: str = "",
    ):
        self.data: dict[str, Any] = {
            "schema_version": "1.0",
            "run_id": run_id,
            "engine": engine,
            "model": model,
            "mode_note": mode_note,
            "request": request,
            "limits": limits,
            "prompt_sha256": prompt_hash,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "steps": [],
        }

    def _append(self, event: dict[str, Any]) -> None:
        event["index"] = len(self.data["steps"]) + 1
        self.data["steps"].append(event)

    def model_decision(self, step: int, text: str, calls: list[ToolCall]) -> None:
        self._append(
            {
                "type": "model_decision",
                "model_step": step,
                "assistant_text": text,
                "tool_calls": [
                    {
                        "id": call.id,
                        "name": call.name,
                        "arguments": call.arguments,
                        "reason": call.reason,
                    }
                    for call in calls
                ],
            }
        )

    def tool_result(self, step: int, call: ToolCall, result: ToolResult, elapsed_ms: int) -> None:
        self._append(
            {
                "type": "tool_result",
                "model_step": step,
                "tool_call_id": call.id,
                "tool": call.name,
                "arguments": call.arguments,
                "ok": result.ok,
                "code": result.code,
                "summary": result.summary,
                "data": result.data,
                "error": result.error,
                "elapsed_ms": elapsed_ms,
            }
        )

    def text_only(self, step: int, text: str) -> None:
        self._append({"type": "assistant_text", "model_step": step, "text": text})

    def note(self, kind: str, **payload: Any) -> None:
        self._append({"type": kind, **payload})

    def error(self, message: str, stage: str = "model") -> None:
        self.note("error", stage=stage, message=message)

    def finalize(self, output: dict[str, Any]) -> None:
        self.data["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self.data["result"] = {
            "status": output.get("status"),
            "outcome": output.get("outcome"),
            "stop_reason": output.get("stop_reason"),
        }

    def to_dict(self) -> dict[str, Any]:
        return self.data


@dataclass(frozen=True)
class Limits:
    max_steps: int = 12
    max_tool_calls: int = 20

    def to_dict(self) -> dict[str, int]:
        return {"max_steps": self.max_steps, "max_tool_calls": self.max_tool_calls}


@dataclass
class RunStatus:
    status: str
    stop_reason: str
    steps: int
    tool_calls: int


@dataclass
class TextOnly:
    text: str


@dataclass
class EngineFailure:
    message: str


@dataclass
class ToolCalls:
    calls: list[ToolCall]
    assistant_text: str = ""


class Engine(Protocol):
    name: str
    model: str
    mode_note: str
    prompt_hash: str

    def next_action(self, state: Any) -> ToolCalls | TextOnly | EngineFailure: ...


class OpenAIEngine:
    """Optional OpenAI-compatible function-calling engine."""

    name = "live-openai"
    mode_note = "真实模型（OpenAI-compatible function calling）决策；结果仍需人工确认。"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
        registry: ToolRegistry,
        client: Any | None = None,
    ):
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("需要安装 openai：pip install -r requirements.txt") from exc
            client = OpenAI(api_key=api_key, base_url=base_url, timeout=90, max_retries=1)
        self.client = client
        self.model = model
        self.registry = registry
        self.prompt_hash = sha256((system_prompt + "\n" + user_prompt).encode("utf-8")).hexdigest()
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def next_action(self, state: Any) -> ToolCalls | TextOnly | EngineFailure:
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=self.messages,
                tools=self.registry.schemas(),
                tool_choice="auto",
                temperature=0,
                max_tokens=1600,
            )
        except Exception as exc:
            return EngineFailure(f"模型调用失败：{exc}")
        message = response.choices[0].message
        raw_calls = getattr(message, "tool_calls", None)
        if not raw_calls:
            return TextOnly(getattr(message, "content", "") or "")
        assistant = {
            "role": "assistant",
            "content": getattr(message, "content", None),
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments or "{}",
                    },
                }
                for call in raw_calls
            ],
        }
        self.messages.append(assistant)
        calls: list[ToolCall] = []
        for call in raw_calls:
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
            calls.append(ToolCall(name=call.function.name, arguments=arguments, id=call.id))
        return ToolCalls(calls, getattr(message, "content", "") or "")

    def record_tool_results(self, exchanges: list[ToolExchange]) -> None:
        for exchange in exchanges:
            self.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": exchange.call.id,
                    "content": json.dumps(exchange.result.to_dict(), ensure_ascii=False),
                }
            )

    def record_nudge(self, message: str) -> None:
        self.messages.append({"role": "user", "content": message})


def run_loop(
    *,
    engine: Engine,
    registry: ToolRegistry,
    state: Any,
    trace: TraceRecorder,
    limits: Limits,
    finish_tool: str = "finish",
) -> RunStatus:
    stop_reason: str | None = None
    consecutive_text = 0

    while state.steps < limits.max_steps:
        if state.tool_calls >= limits.max_tool_calls:
            stop_reason = "tool_limit"
            break
        try:
            action = engine.next_action(state)
        except Exception as exc:
            trace.error(f"模型调用失败：{exc}")
            stop_reason = "model_error"
            break

        if isinstance(action, EngineFailure):
            trace.error(action.message)
            stop_reason = "model_error"
            break

        if isinstance(action, TextOnly):
            state.steps += 1
            consecutive_text += 1
            trace.text_only(state.steps, action.text)
            if consecutive_text >= 2:
                stop_reason = "model_no_tool_call"
                break
            if hasattr(engine, "record_nudge"):
                engine.record_nudge("请调用一个已注册工具；不要只输出解释文字。")
            continue

        if not isinstance(action, ToolCalls) or not action.calls:
            stop_reason = "model_no_tool_call"
            break

        state.steps += 1
        consecutive_text = 0
        for offset, call in enumerate(action.calls):
            if not call.id:
                call.id = f"call_{state.tool_calls + offset + 1}"
        trace.model_decision(state.steps, action.assistant_text, action.calls)

        exchanges: list[ToolExchange] = []
        limit_hit = False
        for call in action.calls:
            if state.tool_calls >= limits.max_tool_calls:
                limit_hit = True
                break
            started = time.perf_counter()
            result = registry.execute(call.name, call.arguments)
            elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
            state.tool_calls += 1
            exchange = ToolExchange(call=call, result=result, elapsed_ms=elapsed_ms)
            state.transcript.append(exchange)
            exchanges.append(exchange)
            trace.tool_result(state.steps, call, result, elapsed_ms)
            if call.name == finish_tool and result.ok:
                state.finished = True
                break

        if hasattr(engine, "record_tool_results"):
            engine.record_tool_results(exchanges)
        if state.finished:
            stop_reason = "finished"
            break
        if limit_hit:
            stop_reason = "tool_limit"
            break
    else:
        stop_reason = "step_limit"

    if stop_reason is None:
        stop_reason = "step_limit"
    status = "completed" if state.finished else ("failed" if stop_reason == "model_error" else "incomplete")
    return RunStatus(status=status, stop_reason=stop_reason, steps=state.steps, tool_calls=state.tool_calls)


def new_run_id(prefix: str = "RUN") -> str:
    return f"{prefix}-{uuid4().hex[:10].upper()}"


def stable_id(prefix: str, payload: dict[str, Any], length: int = 10) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}-{sha256(raw.encode('utf-8')).hexdigest()[:length].upper()}"


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: str | Path, data: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".json-", suffix=".tmp", dir=target.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def parse_faults(values: list[str] | None) -> dict[str, int]:
    faults: dict[str, int] = {}
    for raw in values or []:
        name, _, count = raw.partition(":")
        name = name.strip()
        if not name:
            continue
        try:
            faults[name] = max(1, int(count)) if count else 1
        except ValueError:
            faults[name] = 1
    return faults


def env_engine_default() -> str:
    return "live" if os.getenv("OPENAI_API_KEY") and os.getenv("OPENAI_MODEL") else "offline"

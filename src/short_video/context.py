"""Deterministic context assembly for the tool loop.

The context is rebuilt from the durable run state on every model step. Tool
results are observations, not instructions. Old observations are compacted
before recent ones, and failed observations are always kept visible.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ContextStats:
    chars: int = 0
    observations_total: int = 0
    observations_detailed: int = 0
    observations_omitted: int = 0
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "chars": self.chars,
            "observations_total": self.observations_total,
            "observations_detailed": self.observations_detailed,
            "observations_omitted": self.observations_omitted,
            "truncated": self.truncated,
        }


class ContextBuilder:
    def __init__(
        self,
        *,
        system_prompt: str,
        tools: list[dict[str, Any]],
        request_renderer: Callable[[Any], Any],
        state_renderer: Callable[[Any], Any],
        output_contract: str,
        max_chars: int = 14000,
        recent_observations: int = 6,
        max_observation_chars: int = 1200,
    ):
        self.system_prompt = system_prompt
        self.tools = tools
        self.request_renderer = request_renderer
        self.state_renderer = state_renderer
        self.output_contract = output_contract
        self.max_chars = max_chars
        self.recent_observations = recent_observations
        self.max_observation_chars = max_observation_chars
        self.last_stats = ContextStats()

    def _tool_list(self) -> str:
        lines: list[str] = []
        for schema in self.tools:
            function = schema.get("function", {})
            name = function.get("name", "?")
            params = function.get("parameters", {})
            required = set(params.get("required", []))
            properties = params.get("properties", {})
            rendered_args: list[str] = []
            for key, spec in properties.items():
                marker = "*" if key in required else ""
                detail = ""
                if isinstance(spec, dict) and spec.get("enum"):
                    detail = "[" + "|".join(str(item) for item in spec["enum"]) + "]"
                rendered_args.append(f"{key}{marker}{detail}")
            args = ", ".join(rendered_args)
            lines.append(f"- {name}({args})：{function.get('description', '')}")
        return "\n".join(lines)

    def _observation(self, exchange: Any, detailed: bool, max_chars: int) -> str:
        result = exchange.result
        payload: dict[str, Any] = {
            "tool": exchange.call.name,
            "arguments": exchange.call.arguments,
            "ok": result.ok,
            "code": result.code,
            "summary": result.summary,
        }
        if detailed:
            payload["data"] = result.data
            if result.error:
                payload["error"] = result.error
        text = json.dumps(payload, ensure_ascii=False)
        if len(text) > max_chars:
            text = text[:max_chars] + "...[本条观察已截断]"
        return text

    def _render(
        self,
        state: Any,
        nudges: list[str],
        detailed_count: int,
        observation_chars: int,
        dropped_old: int,
    ) -> str:
        transcript = list(getattr(state, "transcript", []))
        total = len(transcript)
        old = transcript[: max(0, total - detailed_count)] if detailed_count < total else []
        recent = transcript[-detailed_count:] if detailed_count else []
        if dropped_old and old:
            old = old[dropped_old:]

        observations: list[str] = []
        for index, exchange in enumerate(old, start=1 + dropped_old):
            observations.append(f"观察 {index}: {self._observation(exchange, False, observation_chars)}")
        for index, exchange in enumerate(recent, start=len(old) + 1 + dropped_old):
            observations.append(f"观察 {index}: {self._observation(exchange, True, observation_chars)}")

        parts = [
            self.system_prompt,
            "## 可用工具\n" + self._tool_list(),
            "## 请求\n" + json.dumps(self.request_renderer(state), ensure_ascii=False, indent=2),
            "## 当前状态\n" + json.dumps(self.state_renderer(state), ensure_ascii=False, indent=2),
        ]
        if observations:
            parts.append("## 工具观察\n" + "\n".join(observations))
        failures = [exchange for exchange in transcript if not exchange.result.ok]
        if failures:
            recent_failures = failures[-3:]
            parts.append(
                "## 最近失败（必须处理）\n"
                + "\n".join(
                    f"- {exchange.call.name}: {exchange.result.code} / {exchange.result.summary}"
                    for exchange in recent_failures
                )
            )
        if nudges:
            parts.append("## 纠偏提示\n" + "\n".join(f"- {item}" for item in nudges))
        parts.append("## 下一步\n" + self.output_contract)
        return "\n\n".join(parts)

    def build(self, state: Any, nudges: list[str] | None = None) -> str:
        transcript = list(getattr(state, "transcript", []))
        total = len(transcript)
        detailed = min(self.recent_observations, total)
        observation_chars = self.max_observation_chars
        dropped_old = 0
        text = self._render(state, nudges or [], detailed, observation_chars, dropped_old)

        while len(text) > self.max_chars and detailed > 0:
            detailed -= 1
            text = self._render(state, nudges or [], detailed, observation_chars, dropped_old)

        while len(text) > self.max_chars and observation_chars > 240:
            observation_chars = max(240, observation_chars // 2)
            text = self._render(state, nudges or [], detailed, observation_chars, dropped_old)

        old_count = max(0, total - detailed)
        while len(text) > self.max_chars and dropped_old < old_count:
            dropped_old += 1
            text = self._render(state, nudges or [], detailed, observation_chars, dropped_old)

        if len(text) > self.max_chars:
            text = text[: self.max_chars] + "\n...[上下文超过预算，尾部已截断]"

        self.last_stats = ContextStats(
            chars=len(text),
            observations_total=total,
            observations_detailed=detailed,
            observations_omitted=dropped_old,
            truncated=len(text) >= self.max_chars,
        )
        return text

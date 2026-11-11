from __future__ import annotations

import json
import os
import shutil
import subprocess
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable

from .context import ContextBuilder
from .miniagent import EngineFailure, TextOnly, ToolCall, ToolCalls


def default_cli_path() -> str | None:
    configured = os.getenv("OPENCODE_CLI_PATH")
    if configured:
        return configured
    for name in ("opencode-cli", "opencode"):
        found = shutil.which(name)
        if found:
            return found
    base = Path.home() / "Library" / "Application Support" / "ai.opencode.desktop" / "cli"
    if base.exists():
        candidates = sorted(base.glob("*/opencode-cli"))
        if candidates:
            return str(candidates[-1])
    return None


def extract_json_object(text: str) -> dict[str, Any] | None:
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 2:
            stripped = "\n".join(lines[1:])
        if stripped.endswith("```"):
            stripped = stripped[:-3]
    start = stripped.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(stripped)):
        char = stripped[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(stripped[start : index + 1])
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


class OpenCodeLocalEngine:
    name = "live-opencode-local"
    mode_note = "真实模型（本机 OpenCode 实验接口）决策；结果仍需人工确认。"

    def __init__(
        self,
        *,
        context_builder: ContextBuilder,
        cli_path: str | None = None,
        provider: str = "opencode-go",
        model: str = "deepseek-v4.1-flash",
        generate_fn: Callable[[str], str] | None = None,
        timeout: int = 180,
    ):
        self.context_builder = context_builder
        self.cli_path = cli_path or default_cli_path()
        self.provider = provider
        self.model_id = model
        self.model = f"{provider}/{model}"
        self.generate_fn = generate_fn
        self.timeout = timeout
        self.nudges: list[str] = []
        self.prompt_hash = sha256(
            (context_builder.system_prompt + "\ncontext-builder-v1").encode("utf-8")
        ).hexdigest()

    def next_action(self, state: Any) -> ToolCalls | TextOnly | EngineFailure:
        prompt = self.context_builder.build(state, self.nudges)
        try:
            text = self._generate(prompt)
        except Exception as exc:
            return EngineFailure(f"本地模型调用失败：{exc}")
        action = extract_json_object(text)
        if action is None:
            return TextOnly(text)
        tool = action.get("tool")
        arguments = action.get("arguments")
        if not isinstance(tool, str) or not isinstance(arguments, dict):
            return TextOnly(text)
        reason = action.get("reason", "")
        if not isinstance(reason, str):
            reason = ""
        return ToolCalls([ToolCall(name=tool, arguments=arguments, reason=reason)], assistant_text=text)

    def _generate(self, prompt: str) -> str:
        if self.generate_fn is not None:
            return self.generate_fn(prompt)
        if not self.cli_path:
            raise RuntimeError("未配置 OPENCODE_CLI_PATH，找不到本机 OpenCode CLI")
        payload = {
            "prompt": prompt,
            "model": {"id": self.model_id, "providerID": self.provider},
        }
        command = [
            self.cli_path,
            "api",
            "post",
            "/api/experimental/generate/text",
            "-d",
            json.dumps(payload, ensure_ascii=False),
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )
        output = completed.stdout.strip()
        if completed.returncode != 0:
            message = output or completed.stderr.strip()
            raise RuntimeError(f"OpenCode 返回非零退出码：{message[:500]}")
        parsed = extract_json_object(output)
        if parsed is None:
            raise RuntimeError(f"OpenCode 输出不是 JSON：{output[:500]}")
        if isinstance(parsed.get("data"), dict) and isinstance(parsed["data"].get("text"), str):
            return parsed["data"]["text"]
        error = parsed.get("error") or parsed
        raise RuntimeError(f"OpenCode 请求失败：{json.dumps(error, ensure_ascii=False)[:500]}")

    def record_tool_results(self, exchanges: list[Any]) -> None:
        return None

    def record_nudge(self, message: str) -> None:
        self.nudges.append(message)

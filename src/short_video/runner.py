from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .context_adapter import build_context
from .domain import BrandRules, MaterialStore, Templates, VideoRequest, load_request
from .miniagent import (
    Limits,
    OpenAIEngine,
    ToolExchange,
    TraceRecorder,
    new_run_id,
    parse_faults,
    run_loop,
    write_json,
)
from .local_engine import OpenCodeLocalEngine
from .policy import OfflineEngine
from .report import build_report
from .session import ScriptStore, Session
from .tools import build_tools

SYSTEM_PROMPT = """你是短视频脚本 Agent，服务内容创作者。
目标：生成有事实依据、满足目标时长和品牌规则的可编辑脚本。
规则：
1. 先检索素材事实；无素材时说明缺口，不得编造产品事实或效果数据。
2. 脚本分镜必须给出时长、旁白/字幕和素材来源 ID。
3. 时长估算必须逐分镜计算；超目标时先 estimate_duration，再用 revise_script 的 strategy=next_template 压缩，并重新估算；无法满足时明确失败。
4. 规则检查不通过时修正或说明原因；不得跳过。
5. 只有 save_script 成功后才能结束为 completed。
6. 只能通过 finish 汇报，失败状态必须有工具证据。

输出协议：每次只输出一个 JSON 对象，格式为
{"tool":"工具名","arguments":{...},"reason":"一句话依据"}。
不要输出 Markdown、解释或思维过程。"""


def build_user_prompt(request: VideoRequest) -> str:
    return (
        "请处理以下短视频脚本请求：\n"
        + json.dumps(request.to_dict(), ensure_ascii=False, indent=2)
        + "\n可用工具：search_materials, draft_script, estimate_duration, revise_script, check_rules, save_script, finish。"
    )


@dataclass
class AgentState:
    request: VideoRequest
    transcript: list[ToolExchange] = field(default_factory=list)
    steps: int = 0
    tool_calls: int = 0
    finished: bool = False


def run_agent(
    *,
    input_path: str | Path,
    data_dir: str | Path,
    state_dir: str | Path,
    engine_name: str = "offline",
    now: str | None = None,
    faults: list[str] | None = None,
    max_steps: int = 14,
    max_tool_calls: int = 24,
    run_id: str | None = None,
    provider: str = "opencode-go",
    model: str = "deepseek-v4.1-flash",
    cli_path: str | None = None,
    max_context_chars: int = 14000,
) -> tuple[dict[str, Any], dict[str, Any]]:
    request = load_request(input_path)
    materials = MaterialStore.load(data_dir)
    rules = BrandRules.load(data_dir)
    templates = Templates.load(data_dir)
    state_path = Path(state_dir)
    state_path.mkdir(parents=True, exist_ok=True)
    session = Session(
        request=request,
        materials=materials,
        rules=rules,
        templates=templates,
        state_dir=state_path,
    )
    store = ScriptStore(state_path)
    registry = build_tools(session, store, faults=parse_faults(faults))
    limits = Limits(max_steps=max_steps, max_tool_calls=max_tool_calls)
    run_id = run_id or new_run_id("SV")

    if engine_name == "offline":
        engine: Any = OfflineEngine(session)
    elif engine_name == "live":
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:
            pass
        api_key = os.getenv("OPENAI_API_KEY")
        openai_model = os.getenv("OPENAI_MODEL")
        if not api_key or not openai_model:
            raise RuntimeError("live 模式需要 OPENAI_API_KEY 和 OPENAI_MODEL")
        engine = OpenAIEngine(
            api_key=api_key,
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            model=openai_model,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=build_user_prompt(request),
            registry=registry,
        )
    elif engine_name == "opencode-local":
        builder = build_context(session, SYSTEM_PROMPT, registry, max_chars=max_context_chars)
        engine = OpenCodeLocalEngine(
            context_builder=builder,
            cli_path=cli_path,
            provider=provider,
            model=model,
        )
    else:
        raise RuntimeError(f"未知 engine：{engine_name}")

    trace = TraceRecorder(
        run_id=run_id,
        engine=engine.name,
        model=engine.model,
        request=request.to_dict(),
        limits=limits.to_dict(),
        mode_note=engine.mode_note,
        prompt_hash=engine.prompt_hash,
    )
    state = AgentState(request=request)
    run_status = run_loop(
        engine=engine,
        registry=registry,
        state=state,
        trace=trace,
        limits=limits,
    )
    output = build_report(
        session=session,
        exchanges=state.transcript,
        run_status=run_status,
        run_id=run_id,
        engine=engine.name,
        model=engine.model,
        mode_note=engine.mode_note,
        limits=limits.to_dict(),
    )
    if engine_name == "opencode-local":
        trace.note("context_stats", **builder.last_stats.to_dict())
    trace.finalize(output)
    trace_data = trace.to_dict()
    write_json(state_path / "output.json", output)
    write_json(state_path / "trace.json", trace_data)
    return output, trace_data

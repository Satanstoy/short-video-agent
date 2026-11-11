from __future__ import annotations

from typing import Any

from .context import ContextBuilder
from .miniagent import ToolRegistry
from .session import Session


def _recent_failures(transcript: list[Any]) -> list[dict[str, Any]]:
    return [
        {"tool": exchange.call.name, "code": exchange.result.code}
        for exchange in transcript[-8:]
        if not exchange.result.ok
    ]


def render_state(session: Session, state: Any) -> dict[str, Any]:
    return {
        "search_runs": [
            {
                "query": item.get("query"),
                "hit_count": item.get("hit_count"),
                "material_ids": [hit["id"] for hit in item.get("hits", [])],
            }
            for item in session.searches
        ],
        "scripts": [
            {
                "script_id": script.get("script_id"),
                "version": script.get("version"),
                "template": script.get("template"),
                "total_duration_sec": script.get("total_duration_sec"),
                "target_duration_sec": script.get("target_duration_sec"),
                "revised_from": script.get("revised_from"),
            }
            for script in session.scripts.values()
        ],
        "estimates": [
            {
                "script_id": item.get("script_id"),
                "total_duration_sec": item.get("total_duration_sec"),
                "within_target": item.get("within_target"),
            }
            for item in session.estimates
        ],
        "rule_checks": [
            {
                "script_id": item.get("script_id"),
                "passed": item.get("passed"),
                "violations": [violation.get("code") for violation in item.get("violations", [])],
            }
            for item in session.rule_checks
        ],
        "save_attempts": [
            {
                "ok": bool(item.get("ok")),
                "script_id": (item.get("data") or {}).get("script_id"),
                "error": item.get("error"),
            }
            for item in session.save_attempts
        ],
        "active_script": (session.active_script() or {}).get("script_id"),
        "finish": session.finish_payload,
        "recent_failures": _recent_failures(state.transcript),
    }


def build_context(
    session: Session,
    system_prompt: str,
    registry: ToolRegistry,
    max_chars: int = 14000,
) -> ContextBuilder:
    return ContextBuilder(
        system_prompt=system_prompt,
        tools=registry.schemas(),
        request_renderer=lambda state: state.request.to_dict(),
        state_renderer=lambda state: render_state(session, state),
        output_contract=(
            '只输出一个 JSON 对象，格式：{"tool":"工具名","arguments":{...},"reason":"一句话依据"}。'
            "不要 Markdown、解释或思维过程。每次只推进一个工具调用。"
        ),
        max_chars=max_chars,
        recent_observations=6,
    )

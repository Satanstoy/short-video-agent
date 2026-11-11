from __future__ import annotations

from typing import Any

from .miniagent import RunStatus
from .session import Session


def build_report(
    *,
    session: Session,
    exchanges: list[Any],
    run_status: RunStatus,
    run_id: str,
    engine: str,
    model: str,
    mode_note: str,
    limits: dict[str, int],
) -> dict[str, Any]:
    finish = session.finish_payload or {}
    saved = session.saved_script()
    active = session.active_script()
    script = None
    if saved:
        script = session.scripts.get(saved["script_id"])
    if script is None:
        script = active
    script_id = script.get("script_id") if script else None
    estimate = next(
        (item for item in reversed(session.estimates) if item["script_id"] == script_id),
        None,
    )
    check = next(
        (item for item in reversed(session.rule_checks) if item["script_id"] == script_id),
        None,
    )
    failed = [item for item in exchanges if not item.result.ok]
    outcome = finish.get("status") if run_status.status == "completed" else (
        "failed" if run_status.status == "failed" else "incomplete"
    )
    revision_attempts = [
        {
            "script_id": item["script_id"],
            "total_duration_sec": item["total_duration_sec"],
            "target_duration_sec": item["target_duration_sec"],
            "within_target": item["within_target"],
        }
        for item in session.estimates
    ]
    cited_ids: list[str] = []
    for scene in (script or {}).get("scenes", []):
        for source_id in scene.get("source_ids", []):
            if source_id not in cited_ids:
                cited_ids.append(source_id)
    if not cited_ids:
        cited_ids = list((script or {}).get("material_ids", []))
    sources = []
    for material_id in cited_ids:
        material = session.materials.get(material_id)
        if material:
            sources.append({"id": material_id, "title": material.get("title"), "facts": material.get("facts", [])})
    risks = list(finish.get("risks") or [])
    if check:
        risks.extend(check.get("violations", []))
        for warning in check.get("warnings", []):
            if warning not in risks:
                risks.append(warning)
    return {
        "schema_version": "1.0",
        "run_id": run_id,
        "request_id": session.request.request_id,
        "engine": engine,
        "model": model,
        "mode_note": mode_note,
        "status": run_status.status,
        "outcome": outcome,
        "task_status": outcome,
        "stop_reason": run_status.stop_reason,
        "limits": {
            **limits,
            "used_steps": run_status.steps,
            "used_tool_calls": run_status.tool_calls,
        },
        "request": session.request.to_dict(),
        "title": (script or {}).get("title"),
        "storyboard": (script or {}).get("scenes", []),
        "estimated_total_sec": (estimate or {}).get("total_duration_sec"),
        "target_duration_sec": session.request.target_duration_sec,
        "duration_ok": bool((estimate or {}).get("within_target")),
        "material_sources": sources,
        "rule_check": check,
        "revision": {
            "attempts": revision_attempts,
            "revised_from": (script or {}).get("revised_from"),
            "final_version": (script or {}).get("version"),
        },
        "risks": risks,
        "script": {
            "script_id": script_id,
            "version": (script or {}).get("version"),
            "template": (script or {}).get("template"),
            "editable": bool((script or {}).get("editable")),
            "saved": bool(saved),
            "save_status": (saved or {}).get("save_status"),
            "published": False,
        },
        "saved_script": saved,
        "gap": finish.get("gap"),
        "plan_steps": [
            {
                "step": index,
                "tool": exchange.call.name,
                "status": "succeeded" if exchange.result.ok else "failed",
                "code": exchange.result.code,
                "summary": exchange.result.summary,
                "arguments": exchange.call.arguments,
            }
            for index, exchange in enumerate(exchanges, start=1)
        ],
        "degraded": bool(failed),
        "failed_tool_calls": [
            {"tool": item.call.name, "code": item.result.code, "summary": item.result.summary}
            for item in failed
        ],
        "decision_basis": finish.get("summary"),
        "summary": {
            "tools_succeeded": sum(1 for item in exchanges if item.result.ok),
            "tools_failed": len(failed),
            "estimates": len(session.estimates),
            "rule_checks": len(session.rule_checks),
        },
    }

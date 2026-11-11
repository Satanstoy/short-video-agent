from __future__ import annotations

import json

import pytest

from conftest import ROOT
from short_video.cli import approve_version, reject_version
from short_video.domain import BrandRules, MaterialStore, Templates, VideoRequest
from short_video.miniagent import ToolError
from short_video.session import Session
from short_video.storage import Storage
from short_video.task_agent import run_task_turn
from short_video.task_plan import TurnPlan, parse_intent
from short_video.task_runtime import TaskRuntime


def _create_plan() -> TurnPlan:
    return TurnPlan(intent="create", summary="生成第一版")


def _session() -> Session:
    return Session(
        request=VideoRequest.from_dict(
            {
                "request_id": "T-TASK",
                "topic": "字幕功能",
                "audience": "通勤人群",
                "platform": "抖音",
                "target_duration_sec": 30,
            }
        ),
        materials=MaterialStore.load(ROOT / "data"),
        rules=BrandRules.load(ROOT / "data"),
        templates=Templates.load(ROOT / "data"),
        state_dir=ROOT,
    )


def _create(store: Storage, request: dict) -> dict:
    task = store.create_task(request)
    result = run_task_turn(
        store,
        task["id"],
        "生成第一版",
        data_dir=ROOT / "data",
        force_intent="create",
    )
    assert result["turn_status"] == "completed"
    return store.get_task(task["id"])


def test_parse_intent_common_phrases():
    session = _session()
    task = {"id": "SV-T-TEST"}
    versions = [
        {"script_id": "VID-1", "version_no": 1, "parent_script_id": None, "total_duration_sec": 38},
        {"script_id": "VID-2", "version_no": 2, "parent_script_id": "VID-1", "total_duration_sec": 28},
    ]
    current = versions[-1]

    plan = parse_intent("再短一点", task=task, current=current, versions=versions, session=session)
    assert plan.intent == "revise"
    assert plan.strategy == "next_template"
    assert plan.direction == "shorter"

    plan = parse_intent("换一个更直接的开头", task=task, current=current, versions=versions, session=session)
    assert plan.strategy == "patch_scenes"
    assert plan.scene_patches[0]["scene"] == 1

    plan = parse_intent("不要提续航", task=task, current=current, versions=versions, session=session)
    assert plan.strategy == "drop_facts"
    assert plan.remove_source_ids == ["P02"]

    plan = parse_intent("改成 35 秒", task=task, current=current, versions=versions, session=session)
    assert plan.strategy == "next_template"
    assert plan.target_duration_sec == 35

    plan = parse_intent("回到上一版", task=task, current=current, versions=versions, session=session)
    assert plan.intent == "rollback"
    assert plan.target_script_id == "VID-1"

    plan = parse_intent("就用这版", task=task, current=current, versions=versions, session=session)
    assert plan.intent == "approve_suggestion"
    assert "approve 2" in plan.summary

    plan = parse_intent("帮我处理一下", task=task, current=current, versions=versions, session=session)
    assert plan.intent == "unknown"


def test_task_create_and_revision_flow(tmp_path):
    store = Storage(tmp_path / "agent.db")
    request = json.loads((ROOT / "examples/02-over-duration/input.json").read_text(encoding="utf-8"))
    task = _create(store, request)
    assert task["current_script_id"]
    versions = store.list_versions(task["id"])
    assert [(item["version_no"], item["total_duration_sec"], item["status"]) for item in versions] == [
        (1, 38, "draft"),
        (2, 28, "validated"),
    ]

    result = run_task_turn(store, task["id"], "换一个更直接的开头", data_dir=ROOT / "data")
    assert result["turn_status"] == "completed"
    versions = store.list_versions(task["id"])
    assert len(versions) == 3
    assert versions[-1]["status"] == "validated"
    assert versions[-1]["content"]["scenes"][0]["narration"] != versions[-2]["content"]["scenes"][0]["narration"]

    result = run_task_turn(store, task["id"], "改成 35 秒", data_dir=ROOT / "data")
    assert result["turn_status"] == "completed"
    versions = store.list_versions(task["id"])
    assert versions[-1]["total_duration_sec"] == 34
    assert versions[-1]["template"] == "compact"

    result = run_task_turn(store, task["id"], "回到上一版", data_dir=ROOT / "data")
    assert result["turn_status"] == "completed"
    assert result["intent"] == "rollback"
    assert store.get_task(task["id"])["current_script_id"] == versions[-2]["script_id"]

    approval = approve_version(store, task["id"], 3)
    assert approval["decision"] == "approved"
    assert store.get_task(task["id"])["status"] == "approved"
    store.close()


def test_drop_facts_removes_source(tmp_path):
    store = Storage(tmp_path / "agent.db")
    request = {
        "request_id": "SV-MULTI",
        "topic": "字幕 续航",
        "audience": "通勤人群",
        "platform": "抖音",
        "target_duration_sec": 45,
        "style": "自然口语",
    }
    task = _create(store, request)
    before = store.get_version(task["current_script_id"])
    assert "P02" in before["content"]["material_ids"]

    result = run_task_turn(store, task["id"], "不要提续航", data_dir=ROOT / "data")
    assert result["turn_status"] == "completed"
    after = store.get_version(store.get_task(task["id"])["current_script_id"])
    assert "P02" not in after["content"]["material_ids"]
    assert all("P02" not in (scene.get("source_ids") or []) for scene in after["content"]["scenes"])
    store.close()


def test_shorter_than_minimum_does_not_create_version(tmp_path):
    store = Storage(tmp_path / "agent.db")
    request = json.loads((ROOT / "examples/02-over-duration/input.json").read_text(encoding="utf-8"))
    task = _create(store, request)
    before = len(store.list_versions(task["id"]))

    result = run_task_turn(store, task["id"], "再短一点", data_dir=ROOT / "data")
    assert result["turn_status"] == "completed"
    assert "已经是最短模板" in result["assistant_text"]
    assert len(store.list_versions(task["id"])) == before
    store.close()


def test_finish_completed_requires_save_in_turn(tmp_path):
    store = Storage(tmp_path / "agent.db")
    request = json.loads((ROOT / "examples/02-over-duration/input.json").read_text(encoding="utf-8"))
    task = store.create_task(request)
    runtime = TaskRuntime(store, task, "TURN-FAKE", _create_plan())
    with pytest.raises(ToolError) as exc:
        runtime.finish("completed", "done")
    assert exc.value.code == "EVIDENCE_MISMATCH"
    store.close()


def test_reject_current_falls_back_to_previous(tmp_path):
    store = Storage(tmp_path / "agent.db")
    request = json.loads((ROOT / "examples/02-over-duration/input.json").read_text(encoding="utf-8"))
    task = _create(store, request)
    result = run_task_turn(store, task["id"], "换一个更直接的开头", data_dir=ROOT / "data")
    current = store.get_task(task["id"])["current_script_id"]
    assert current
    reject_version(store, task["id"], 3, "开头太慢")
    updated = store.get_task(task["id"])
    assert updated["current_script_id"] != current
    assert store.get_version(updated["current_script_id"])["version_no"] == 2
    store.close()


def test_revision_rejects_foreign_task(tmp_path):
    store = Storage(tmp_path / "agent.db")
    request = json.loads((ROOT / "examples/02-over-duration/input.json").read_text(encoding="utf-8"))
    task_a = _create(store, request)
    task_b = store.create_task(request)
    runtime = TaskRuntime(store, task_b, "TURN-FAKE", _create_plan())
    with pytest.raises(ToolError) as exc:
        runtime.revise_script(task_a["current_script_id"])
    assert exc.value.code == "TASK_MISMATCH"
    store.close()


def test_tool_calls_are_persisted(tmp_path):
    store = Storage(tmp_path / "agent.db")
    request = json.loads((ROOT / "examples/02-over-duration/input.json").read_text(encoding="utf-8"))
    task = store.create_task(request)
    result = run_task_turn(
        store, task["id"], "生成第一版", data_dir=ROOT / "data", force_intent="create"
    )
    calls = store.list_tool_calls(result["turn_id"])
    tools = [item["tool"] for item in calls]
    assert "search_materials" in tools
    assert "draft_script" in tools
    assert "estimate_duration" in tools
    assert "check_rules" in tools
    assert "save_script" in tools
    assert "finish" in tools
    store.close()


def test_review_and_rollback_keep_current_candidate_consistent(tmp_path):
    from short_video.cli import rollback_version

    store = Storage(tmp_path / "agent.db")
    request = json.loads((ROOT / "examples/02-over-duration/input.json").read_text(encoding="utf-8"))
    task_id = _create(store, request)["id"]
    approval = approve_version(store, task_id, 2)
    assert approve_version(store, task_id, 2)["id"] == approval["id"]
    with pytest.raises(ValueError, match="已批准"):
        reject_version(store, task_id, 2)
    with pytest.raises(ValueError, match="尚未通过校验"):
        rollback_version(store, task_id, 1)

    run_task_turn(store, task_id, "换一个更直接的开头", data_dir=ROOT / "data")
    reject_version(store, task_id, 3, "保留原开头")
    with pytest.raises(ValueError, match="已被驳回"):
        rollback_version(store, task_id, 3)
    assert store.get_task(task_id)["current_script_id"] == approval["script_id"]
    assert store.get_task(task_id)["status"] == "approved"
    assert [version["status"] for version in store.list_versions(task_id)] == [
        "draft", "approved", "rejected",
    ]
    store.close()


def test_live_turn_records_resolved_model_without_network(tmp_path, monkeypatch):
    from short_video import task_agent

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")

    class FakeEngine:
        def __init__(self, *, model, registry, user_prompt, **kwargs):
            assert model == "test-model"
            assert "此前用户：" in user_prompt
            assert "approve" not in registry.names()

        def next_action(self, state):
            return task_agent.ToolCalls(
                [task_agent.ToolCall("finish", {"status": "needs_clarification", "summary": "想改哪一幕？"})]
            )

    monkeypatch.setattr(task_agent, "OpenAIEngine", FakeEngine)
    store = Storage(tmp_path / "agent.db")
    request = json.loads((ROOT / "examples/02-over-duration/input.json").read_text(encoding="utf-8"))
    task_id = _create(store, request)["id"]
    result = run_task_turn(store, task_id, "改一下", data_dir=ROOT / "data", engine_name="live")
    assert result["turn_status"] == "needs_clarification"
    assert store.get_turn(result["turn_id"])["model"] == "test-model"
    assert store.list_tool_calls(result["turn_id"])[0]["tool"] == "finish"
    store.close()

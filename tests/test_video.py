from __future__ import annotations

import json

from conftest import ROOT
from short_video.domain import BrandRules, MaterialStore, Templates, VideoRequest
from short_video.session import ScriptStore, Session
from short_video.tools import build_tools, _build_scenes, _check


def test_normal_script_is_saved_and_checked(run_case):
    output, _ = run_case("01-normal")
    assert output["task_status"] == "completed"
    assert output["estimated_total_sec"] == 38
    assert output["target_duration_sec"] == 45
    assert output["duration_ok"] is True
    assert output["rule_check"]["passed"] is True
    assert output["script"]["saved"] is True
    assert output["script"]["published"] is False
    assert output["material_sources"][0]["id"] == "P01"


def test_over_duration_is_revised_from_38_to_28(run_case):
    output, _ = run_case("02-over-duration")
    assert output["task_status"] == "completed"
    assert output["estimated_total_sec"] == 28
    assert output["target_duration_sec"] == 30
    assert output["duration_ok"] is True
    attempts = output["revision"]["attempts"]
    assert [item["total_duration_sec"] for item in attempts] == [38, 28]
    assert attempts[0]["within_target"] is False
    assert attempts[-1]["within_target"] is True
    assert output["revision"]["revised_from"] is not None


def test_storyboard_durations_sum_to_estimate(run_case):
    output, _ = run_case("02-over-duration")
    assert sum(scene["duration_sec"] for scene in output["storyboard"]) == output["estimated_total_sec"]
    assert all(scene["narration"] for scene in output["storyboard"])
    assert all(scene["subtitle"] for scene in output["storyboard"])


def test_missing_key_info_returns_needs_input(run_case):
    output, _ = run_case("03-missing-info")
    assert output["task_status"] == "needs_input"
    assert output["request"]["missing_fields"] == ["platform", "target_duration_sec"]
    assert output["script"]["script_id"] is None
    assert output["storyboard"] == []


def test_no_material_does_not_generate_script(run_case):
    output, _ = run_case("04-no-material")
    assert output["task_status"] == "no_material"
    assert output["script"]["script_id"] is None
    assert output["storyboard"] == []
    assert output["material_sources"] == []


def test_estimate_failure_preserves_draft_but_not_completion(run_case):
    output, _ = run_case("05-tool-failure", faults=["estimate_duration:2"])
    assert output["task_status"] == "tool_error"
    assert output["script"]["script_id"] is not None
    assert output["script"]["saved"] is False
    assert output["duration_ok"] is False
    assert [item["code"] for item in output["failed_tool_calls"]] == [
        "INJECTED_FAILURE",
        "INJECTED_FAILURE",
    ]


def test_impossible_duration_fails_clearly(run_case):
    output, _ = run_case("06-impossible-duration")
    assert output["task_status"] == "cannot_meet_duration"
    assert output["duration_ok"] is False
    assert output["estimated_total_sec"] == 38
    assert output["gap"]["target_duration_sec"] == 15
    assert output["gap"]["min_duration_sec"] == 28
    assert output["gap"]["over_by_sec"] == 13
    assert output["script"]["saved"] is False


def test_no_fabricated_effect_claim(run_case):
    output, _ = run_case("02-over-duration")
    script_text = json.dumps(output["storyboard"], ensure_ascii=False)
    assert "80%" not in script_text
    assert "效率提升" not in script_text


def test_rule_checker_rejects_unsupported_claim():
    request = VideoRequest.from_dict(
        {
            "request_id": "T-1",
            "topic": "字幕功能",
            "platform": "抖音",
            "target_duration_sec": 45,
        }
    )
    materials = MaterialStore.load(ROOT / "data")
    session = Session(
        request=request,
        materials=materials,
        rules=BrandRules.load(ROOT / "data"),
        templates=Templates.load(ROOT / "data"),
        state_dir=ROOT,
    )
    scenes, _ = _build_scenes(session, session.templates.base, "字幕功能", "通勤人群", ["P01"])
    script = {
        "script_id": "TEST",
        "title": "测试",
        "total_duration_sec": sum(scene["duration_sec"] for scene in scenes),
        "scenes": scenes,
        "material_ids": ["P01"],
    }
    scenes[2]["claim"] = "效率提升 80%"
    session.scripts["TEST"] = script
    result = _check(session, "TEST")
    assert result["passed"] is False
    assert any(item["code"] == "UNSUPPORTED_CLAIM" for item in result["violations"])


def test_rule_checker_rejects_banned_words():
    request = VideoRequest.from_dict(
        {"request_id": "T-2", "topic": "字幕功能", "platform": "抖音", "target_duration_sec": 45}
    )
    session = Session(
        request=request,
        materials=MaterialStore.load(ROOT / "data"),
        rules=BrandRules.load(ROOT / "data"),
        templates=Templates.load(ROOT / "data"),
        state_dir=ROOT,
    )
    scenes, _ = _build_scenes(session, session.templates.base, "字幕功能", "通勤人群", ["P01"])
    scenes[0]["narration"] = "这是行业第一的方案"
    session.scripts["TEST2"] = {
        "script_id": "TEST2",
        "title": "测试",
        "total_duration_sec": sum(scene["duration_sec"] for scene in scenes),
        "scenes": scenes,
        "material_ids": ["P01"],
    }
    result = _check(session, "TEST2")
    assert result["passed"] is False
    assert any(item["code"] == "BANNED_CLAIM" for item in result["violations"])


def test_save_before_duration_and_rule_checks_is_rejected(tmp_path):
    request = VideoRequest.from_dict(
        {"request_id": "T-3", "topic": "字幕功能", "platform": "抖音", "target_duration_sec": 45}
    )
    session = Session(
        request=request,
        materials=MaterialStore.load(ROOT / "data"),
        rules=BrandRules.load(ROOT / "data"),
        templates=Templates.load(ROOT / "data"),
        state_dir=tmp_path,
    )
    registry = build_tools(session, ScriptStore(tmp_path))
    draft = registry.execute(
        "draft_script",
        {
            "topic": "字幕功能",
            "platform": "抖音",
            "target_duration_sec": 45,
            "material_ids": ["P01"],
        },
    )
    assert draft.ok
    result = registry.execute("save_script", {"script_id": draft.data["script_id"]})
    assert result.ok is False
    assert result.code == "DURATION_NOT_VERIFIED"


def test_claim_must_belong_to_cited_source():
    request = VideoRequest.from_dict(
        {"request_id": "T-4", "topic": "字幕功能", "platform": "抖音", "target_duration_sec": 45}
    )
    session = Session(
        request=request,
        materials=MaterialStore.load(ROOT / "data"),
        rules=BrandRules.load(ROOT / "data"),
        templates=Templates.load(ROOT / "data"),
        state_dir=ROOT,
    )
    scenes, _ = _build_scenes(session, session.templates.base, "字幕功能", "通勤人群", ["P01"])
    scenes[2]["claim"] = "续航 12 小时"
    scenes[2]["source_ids"] = ["P01"]
    session.scripts["TEST3"] = {
        "script_id": "TEST3",
        "title": "测试",
        "total_duration_sec": sum(scene["duration_sec"] for scene in scenes),
        "scenes": scenes,
        "material_ids": ["P01"],
    }
    result = _check(session, "TEST3")
    assert result["passed"] is False
    assert any(item["code"] == "UNSUPPORTED_CLAIM" for item in result["violations"])


def test_multi_material_claims_cite_the_right_source():
    request = VideoRequest.from_dict(
        {"request_id": "T-5", "topic": "字幕功能", "platform": "抖音", "target_duration_sec": 45}
    )
    session = Session(
        request=request,
        materials=MaterialStore.load(ROOT / "data"),
        rules=BrandRules.load(ROOT / "data"),
        templates=Templates.load(ROOT / "data"),
        state_dir=ROOT,
    )
    scenes, _ = _build_scenes(
        session, session.templates.base, "字幕功能", "通勤人群", ["P01", "P02"]
    )
    assert scenes[2]["claim"] == "支持字幕"
    assert scenes[2]["source_ids"] == ["P01"]
    assert scenes[3]["claim"] == "续航 12 小时"
    assert scenes[3]["source_ids"] == ["P02"]
    session.scripts["TEST4"] = {
        "script_id": "TEST4",
        "title": "测试",
        "total_duration_sec": sum(scene["duration_sec"] for scene in scenes),
        "scenes": scenes,
        "material_ids": ["P01", "P02"],
    }
    result = _check(session, "TEST4")
    assert result["passed"] is True


def test_target_35_uses_compact_template(run_case):
    output, _ = run_case("07-target-35")
    assert output["task_status"] == "completed"
    assert output["estimated_total_sec"] == 34
    assert output["target_duration_sec"] == 35
    assert output["duration_ok"] is True
    assert output["script"]["template"] == "compact"
    attempts = output["revision"]["attempts"]
    assert [item["total_duration_sec"] for item in attempts] == [38, 34]
    assert attempts[0]["within_target"] is False
    assert attempts[-1]["within_target"] is True


def test_rule_checker_rejects_unsupported_narration_assertion():
    request = VideoRequest.from_dict(
        {"request_id": "T-6", "topic": "字幕功能", "platform": "抖音", "target_duration_sec": 45}
    )
    session = Session(
        request=request,
        materials=MaterialStore.load(ROOT / "data"),
        rules=BrandRules.load(ROOT / "data"),
        templates=Templates.load(ROOT / "data"),
        state_dir=ROOT,
    )
    scenes, _ = _build_scenes(session, session.templates.base, "字幕功能", "通勤人群", ["P01"])
    scenes[2]["narration"] = "先看核心事实：支持字幕，续航翻倍。"
    session.scripts["TEST5"] = {
        "script_id": "TEST5",
        "title": "测试",
        "total_duration_sec": sum(scene["duration_sec"] for scene in scenes),
        "scenes": scenes,
        "material_ids": ["P01"],
    }
    result = _check(session, "TEST5")
    assert result["passed"] is False
    assert any(item["code"] == "UNSUPPORTED_ASSERTION" for item in result["violations"])


def test_multi_material_selection_is_not_input_order():
    request = VideoRequest.from_dict(
        {"request_id": "T-7", "topic": "字幕功能", "platform": "抖音", "target_duration_sec": 45}
    )
    session = Session(
        request=request,
        materials=MaterialStore.load(ROOT / "data"),
        rules=BrandRules.load(ROOT / "data"),
        templates=Templates.load(ROOT / "data"),
        state_dir=ROOT,
    )
    scenes, _ = _build_scenes(
        session, session.templates.base, "字幕功能", "通勤人群", ["P02", "P01"]
    )
    assert scenes[2]["claim"] == "支持字幕"
    assert scenes[2]["source_ids"] == ["P01"]
    assert scenes[3]["claim"] == "续航 12 小时"
    assert scenes[3]["source_ids"] == ["P02"]


def test_narration_scan_ignores_request_topic():
    request = VideoRequest.from_dict(
        {"request_id": "T-8", "topic": "续航提升", "platform": "抖音", "target_duration_sec": 45}
    )
    session = Session(
        request=request,
        materials=MaterialStore.load(ROOT / "data"),
        rules=BrandRules.load(ROOT / "data"),
        templates=Templates.load(ROOT / "data"),
        state_dir=ROOT,
    )
    scenes, _ = _build_scenes(
        session, session.templates.base, "续航提升", "通勤人群", ["P02"]
    )
    session.scripts["TEST6"] = {
        "script_id": "TEST6",
        "title": "续航提升｜抖音短视频",
        "topic": "续航提升",
        "audience": "通勤人群",
        "platform": "抖音",
        "style": "自然口语",
        "total_duration_sec": sum(scene["duration_sec"] for scene in scenes),
        "scenes": scenes,
        "material_ids": ["P02"],
    }
    result = _check(session, "TEST6")
    assert result["passed"] is True


def test_revise_requires_estimate_first(tmp_path):
    request = VideoRequest.from_dict(
        {"request_id": "T-6", "topic": "字幕功能", "platform": "抖音", "target_duration_sec": 30}
    )
    session = Session(
        request=request,
        materials=MaterialStore.load(ROOT / "data"),
        rules=BrandRules.load(ROOT / "data"),
        templates=Templates.load(ROOT / "data"),
        state_dir=tmp_path,
    )
    registry = build_tools(session, ScriptStore(tmp_path))
    draft = registry.execute(
        "draft_script",
        {
            "topic": "字幕功能",
            "platform": "抖音",
            "target_duration_sec": 30,
            "material_ids": ["P01"],
        },
    )
    assert draft.ok
    result = registry.execute(
        "revise_script",
        {"script_id": draft.data["script_id"], "target_duration_sec": 30},
    )
    assert result.ok is False
    assert result.code == "ESTIMATE_REQUIRED"

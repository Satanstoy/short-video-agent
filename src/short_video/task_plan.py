from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .session import Session


@dataclass
class TurnPlan:
    intent: str
    strategy: str | None = None
    direction: str | None = None
    target_duration_sec: int | None = None
    target_script_id: str | None = None
    scene_patches: list[dict[str, Any]] = field(default_factory=list)
    remove_source_ids: list[str] = field(default_factory=list)
    focus_keywords: list[str] = field(default_factory=list)
    instructions: str = ""
    clarification: str = ""
    summary: str = ""


HOOKS = [
    "字幕没开，观众三秒就走。",
    "先别划走，这个功能通勤时最有用。",
    "你有没有因为字幕错过关键信息？",
]

ENDINGS = [
    "想要模板，评论区扣 1。",
    "想看完整演示，评论区告诉我。",
    "先把这版存下来，下次直接套。",
]


def _version_by_no(versions: list[dict[str, Any]], version_no: int) -> dict[str, Any] | None:
    return next((item for item in versions if item["version_no"] == version_no), None)


def _version_by_duration(versions: list[dict[str, Any]], seconds: int) -> dict[str, Any] | None:
    matches = [item for item in versions if item["total_duration_sec"] == seconds]
    return matches[-1] if matches else None


def _find_rollback_target(
    text: str,
    current: dict[str, Any] | None,
    versions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    match = re.search(r"(?:v|第)\s*(\d+)\s*(?:版)?", text, re.IGNORECASE)
    if match:
        return _version_by_no(versions, int(match.group(1)))
    match = re.search(r"(\d+)\s*秒", text)
    if match:
        return _version_by_duration(versions, int(match.group(1)))
    if "上一版" in text or "上一个版本" in text or "前一个版本" in text:
        if current and current.get("parent_script_id"):
            return next(
                (item for item in versions if item["script_id"] == current["parent_script_id"]),
                None,
            )
        if current:
            previous = [item for item in versions if item["version_no"] < current["version_no"]]
            return previous[-1] if previous else None
    if "第一版" in text or "最初" in text:
        return versions[0] if versions else None
    return None


def _material_ids_for(text: str, session: Session) -> list[str]:
    needles = [part for part in re.split(r"[\s，。！？、]+", text) if part]
    hits: list[str] = []
    for material in session.materials.materials:
        material_id = str(material.get("id") or "")
        haystack = " ".join(
            [
                str(material.get("title") or ""),
                " ".join(str(tag) for tag in material.get("tags", [])),
                " ".join(str(fact) for fact in material.get("facts", [])),
            ]
        )
        if any(needle and needle in haystack for needle in needles):
            hits.append(material_id)
    return hits


def _scene_patch_for_hook(current: dict[str, Any] | None) -> dict[str, Any]:
    current_text = ""
    if current:
        scenes = current.get("content", {}).get("scenes") or []
        if scenes:
            current_text = str(scenes[0].get("narration") or "")
    hook = next((item for item in HOOKS if item != current_text), HOOKS[0])
    return {"scene": 1, "narration": hook}


def _scene_patch_for_ending(current: dict[str, Any] | None) -> dict[str, Any]:
    current_text = ""
    if current:
        scenes = current.get("content", {}).get("scenes") or []
        if scenes:
            current_text = str(scenes[-1].get("narration") or "")
    ending = next((item for item in ENDINGS if item != current_text), ENDINGS[0])
    return {"scene": 5, "narration": ending}


def parse_intent(
    message: str,
    *,
    task: dict[str, Any],
    current: dict[str, Any] | None,
    versions: list[dict[str, Any]],
    session: Session,
) -> TurnPlan:
    text = (message or "").strip()
    if not text:
        return TurnPlan(intent="unknown", clarification="你想改哪一部分？")

    if any(word in text for word in ("为什么", "怎么", "风险", "哪里", "是否", "能不能")):
        return TurnPlan(intent="question", summary=text)
    if text.endswith("?") or text.endswith("？"):
        return TurnPlan(intent="question", summary=text)

    if any(word in text for word in ("回到", "回退", "用回", "上一版", "上一个版本", "最初")):
        target = _find_rollback_target(text, current, versions)
        if target is None:
            return TurnPlan(
                intent="unknown",
                clarification="要回到哪一版？可以说“回到上一版”或“回到 v2”。",
            )
        return TurnPlan(
            intent="rollback",
            target_script_id=target["script_id"],
            summary=f"回退到 v{target['version_no']}",
        )

    if any(word in text for word in ("就用这版", "可以了", "通过", "这版不错", "没问题")):
        version_no = current["version_no"] if current else None
        return TurnPlan(
            intent="approve_suggestion",
            summary=f"请使用 /approve {version_no}" if version_no else "当前还没有可批准的版本。",
        )

    duration_match = re.search(r"(\d+)\s*秒", text)
    if duration_match and any(word in text for word in ("改", "换成", "目标", "调到", "做成", "到")):
        return TurnPlan(
            intent="revise",
            strategy="next_template",
            direction="target",
            target_duration_sec=int(duration_match.group(1)),
            instructions=text,
            summary=f"把目标时长改成 {duration_match.group(1)} 秒",
        )

    if any(word in text for word in ("再短", "短一点", "压缩", "缩短", "剪短")):
        return TurnPlan(
            intent="revise",
            strategy="next_template",
            direction="shorter",
            instructions=text,
            summary="换下一档更短的模板",
        )

    if any(word in text for word in ("再长", "长一点", "延长", "加长")):
        return TurnPlan(
            intent="revise",
            strategy="next_template",
            direction="longer",
            instructions=text,
            summary="换下一档更长的模板",
        )

    if "开头" in text:
        return TurnPlan(
            intent="revise",
            strategy="patch_scenes",
            scene_patches=[_scene_patch_for_hook(current)],
            instructions=text,
            summary="替换第 1 个分镜的开头",
        )

    if "结尾" in text:
        return TurnPlan(
            intent="revise",
            strategy="patch_scenes",
            scene_patches=[_scene_patch_for_ending(current)],
            instructions=text,
            summary="替换最后一个分镜的结尾",
        )

    for marker in ("不要提", "去掉", "删掉", "别说"):
        if marker in text:
            needle = text.split(marker, 1)[1].strip(" ，。！？")
            if not needle:
                break
            material_ids = _material_ids_for(needle, session)
            if material_ids:
                return TurnPlan(
                    intent="revise",
                    strategy="drop_facts",
                    remove_source_ids=material_ids,
                    instructions=text,
                    summary=f"删除素材 {', '.join(material_ids)} 的相关事实",
                )
            return TurnPlan(
                intent="unknown",
                clarification=f"没有找到和“{needle}”对应的素材事实，要删哪一条？",
            )

    if any(word in text for word in ("突出", "重点讲", "强调", "聚焦")):
        keyword = re.sub(r"^(突出|重点讲|强调|聚焦)", "", text).strip(" ，。！？")
        return TurnPlan(
            intent="revise",
            strategy="redraft",
            focus_keywords=[keyword] if keyword else [],
            instructions=text,
            summary=f"围绕“{keyword}”重新生成" if keyword else "重新生成脚本",
        )

    if any(word in text for word in ("重新生成", "换一版", "重来", "重写")):
        return TurnPlan(
            intent="revise",
            strategy="redraft",
            instructions=text,
            summary="重新生成一版",
        )

    return TurnPlan(
        intent="unknown",
        clarification="你想改时长、换开头、删某个卖点，还是回到旧版本？",
    )

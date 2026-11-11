from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from .domain import _tokens
from .miniagent import Tool, ToolError, ToolResult, ToolRegistry, stable_id
from .session import ScriptStore, Session


def _search(session: Session, query: str) -> dict[str, Any]:
    if not query.strip():
        raise ToolError("EMPTY_QUERY", "检索词不能为空")
    hits = session.materials.search(query)
    payload = {"query": query, "hits": hits, "hit_count": len(hits)}
    session.searches.append(payload)
    return payload


def _fact_records(
    session: Session, material_ids: list[str]
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for material_id in material_ids:
        material = session.materials.get(material_id)
        if material is None:
            raise ToolError("UNKNOWN_MATERIAL", f"素材 {material_id} 不存在")
        for fact in material.get("facts", []):
            fact_text = str(fact)
            if fact_text in seen:
                continue
            seen.add(fact_text)
            records.append(
                {"fact": fact_text, "source_id": material_id, "material": material}
            )
    return records


def _fact_score(record: dict[str, Any], query_tokens: set[str]) -> int:
    material = record["material"]
    text = " ".join(
        [
            record["fact"],
            str(material.get("title") or ""),
            " ".join(str(tag) for tag in material.get("tags", [])),
        ]
    )
    return len(query_tokens & _tokens(text))


def _pick_fact(
    records: list[dict[str, Any]], used: set[int], query: str
) -> tuple[int, dict[str, Any]]:
    query_tokens = _tokens(query)
    best_index = -1
    best_score = -1
    for index, record in enumerate(records):
        if index in used:
            continue
        score = _fact_score(record, query_tokens)
        if score > best_score:
            best_index = index
            best_score = score
    if best_index < 0 and records:
        # 镜头多于事实时允许复用，但仍选与当前镜头最接近的一条。
        for index, record in enumerate(records):
            score = _fact_score(record, query_tokens)
            if score > best_score:
                best_index = index
                best_score = score
    if best_index < 0:
        raise ToolError("NO_FACT", "素材没有可用事实，不能生成脚本")
    return best_index, records[best_index]


def _build_scenes(
    session: Session,
    template: list[dict[str, Any]],
    topic: str,
    audience: str,
    material_ids: list[str],
    focus_keywords: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not material_ids:
        raise ToolError("NO_SOURCE", "没有素材 ID，不能生成脚本")
    records = _fact_records(session, material_ids)
    if not records:
        raise ToolError("NO_FACT", "素材没有可用事实，不能生成脚本")
    facts = [record["fact"] for record in records]
    facts_text = "；".join(facts)
    used: set[int] = set()
    scenes: list[dict[str, Any]] = []
    for item in template:
        scene = {
            "scene": item["scene"],
            "name": item["name"],
            "duration_sec": int(item["duration_sec"]),
            "visual": str(item["visual"]).format(topic=topic, audience=audience),
            "narration": str(item["narration"]).format(
                topic=topic, audience=audience, facts=facts_text, fact=facts[0]
            ),
            "subtitle": "",
            "claim": None,
            "source_ids": [],
        }
        if item.get("claim_slot"):
            query = " ".join(
                [
                    topic,
                    audience,
                    " ".join(focus_keywords or []),
                    str(item.get("name") or ""),
                    str(item.get("visual") or ""),
                    str(item.get("claim_intent") or ""),
                ]
            )
            index, record = _pick_fact(records, used, query)
            used.add(index)
            fact = record["fact"]
            source_id = record["source_id"]
            scene["claim"] = fact
            scene["source_ids"] = [source_id]
            scene["narration"] = str(item["narration"]).format(
                topic=topic, audience=audience, facts=facts_text, fact=fact
            )
        scene["subtitle"] = scene["narration"]
        scenes.append(scene)
    return scenes, facts


def _draft(
    session: Session,
    topic: str,
    audience: str,
    platform: str,
    target_duration_sec: int,
    style: str,
    material_ids: list[str],
) -> dict[str, Any]:
    if target_duration_sec <= 0:
        raise ToolError("INVALID_DURATION", "目标时长必须大于 0")
    scenes, facts = _build_scenes(session, session.templates.base, topic, audience, material_ids)
    script = {
        "script_id": stable_id(
            "VID",
            {
                "topic": topic,
                "audience": audience,
                "platform": platform,
                "target": target_duration_sec,
                "material_ids": material_ids,
                "template": "base",
            },
        ),
        "version": 1,
        "template": "base",
        "title": f"{topic}｜{platform}短视频",
        "topic": topic,
        "audience": audience,
        "platform": platform,
        "style": style,
        "target_duration_sec": target_duration_sec,
        "scenes": scenes,
        "total_duration_sec": sum(scene["duration_sec"] for scene in scenes),
        "material_ids": material_ids,
        "facts": facts,
        "revised_from": None,
        "editable": True,
    }
    session.scripts[script["script_id"]] = script
    session.set_active_script(script)
    return script


def _estimate(session: Session, script_id: str) -> dict[str, Any]:
    script = session.scripts.get(script_id)
    if script is None:
        raise ToolError("UNKNOWN_SCRIPT", f"未知脚本 {script_id}")
    total = sum(scene["duration_sec"] for scene in script["scenes"])
    target = int(script["target_duration_sec"])
    result = {
        "estimate_id": stable_id("DUR", {"script_id": script_id, "total": total}),
        "script_id": script_id,
        "total_duration_sec": total,
        "target_duration_sec": target,
        "within_target": total <= target,
        "over_target_by_sec": max(0, total - target),
        "scene_durations": [
            {"scene": scene["scene"], "name": scene["name"], "duration_sec": scene["duration_sec"]}
            for scene in script["scenes"]
        ],
    }
    session.estimates.append(result)
    return result


def _template_options(session: Session) -> list[tuple[str, list[dict[str, Any]]]]:
    options: list[tuple[str, list[dict[str, Any]]]] = [("base", session.templates.base)]
    if session.templates.compact:
        options.append(("compact", session.templates.compact))
    if session.templates.compressed:
        options.append(("compressed", session.templates.compressed))
    return options


def _choose_template(
    session: Session,
    current: int,
    target: int,
    direction: str,
) -> tuple[int, str, list[dict[str, Any]]] | None:
    options = [
        (sum(int(item["duration_sec"]) for item in template), name, template)
        for name, template in _template_options(session)
        if template
    ]
    if direction == "shorter":
        candidates = [item for item in options if item[0] < current]
        return max(candidates, key=lambda item: item[0]) if candidates else None
    if direction == "longer":
        candidates = [item for item in options if item[0] > current]
        return min(candidates, key=lambda item: item[0]) if candidates else None
    candidates = [item for item in options if item[0] <= target]
    return max(candidates, key=lambda item: item[0]) if candidates else None


def _template_for_total(
    session: Session, total: int
) -> tuple[str, list[dict[str, Any]]]:
    for name, template in _template_options(session):
        if template and sum(int(item["duration_sec"]) for item in template) == total:
            return name, template
    return "base", session.templates.base


def _revise(
    session: Session,
    script_id: str,
    target_duration_sec: int | None = None,
    *,
    strategy: str = "next_template",
    direction: str = "target",
    scene_patches: list[dict[str, Any]] | None = None,
    remove_source_ids: list[str] | None = None,
    focus_keywords: list[str] | None = None,
    instructions: str = "",
) -> ToolResult:
    script = session.scripts.get(script_id)
    if script is None:
        raise ToolError("UNKNOWN_SCRIPT", f"未知脚本 {script_id}")
    current = sum(scene["duration_sec"] for scene in script["scenes"])
    version = int(script.get("version", 1)) + 1

    if strategy == "next_template":
        estimate = next(
            (item for item in reversed(session.estimates) if item["script_id"] == script_id),
            None,
        )
        if estimate is None:
            raise ToolError(
                "ESTIMATE_REQUIRED",
                "修改时长前必须先调用 estimate_duration，确认当前脚本状态",
            )
        requested_target = int(target_duration_sec or script["target_duration_sec"])
        chosen = _choose_template(session, current, requested_target, direction)
        if chosen is None:
            if direction == "longer":
                failure = ToolResult.failure(
                    "CANNOT_EXTEND",
                    f"没有比当前 {current} 秒更长的模板",
                    data={
                        "script_id": script_id,
                        "current_total_sec": current,
                    },
                )
                return failure
            failure = ToolResult.failure(
                "CANNOT_COMPRESS",
                f"脚本最短可压缩到 {session.templates.min_duration_sec} 秒，无法满足 {requested_target} 秒",
                data={
                    "script_id": script_id,
                    "min_duration_sec": session.templates.min_duration_sec,
                    "target_duration_sec": requested_target,
                },
            )
            session.revision_errors.append(
                {
                    "script_id": script_id,
                    "code": "CANNOT_COMPRESS",
                    "min_duration_sec": session.templates.min_duration_sec,
                    "target_duration_sec": requested_target,
                }
            )
            return failure
        total, template_name, template = chosen
        if total == current and template_name == script.get("template"):
            data = {**script, "revision": {"changed": False, "reason": "already_at_this_length"}}
            session.set_active_script(data)
            return ToolResult.success("当前模板已是最接近目标的长度", data)
        new_target = total if direction in {"shorter", "longer"} else requested_target
        scenes, facts = _build_scenes(
            session,
            template,
            script["topic"],
            script["audience"],
            list(script["material_ids"]),
            focus_keywords,
        )
        revised = {
            **script,
            "script_id": stable_id(
                "VID",
                {
                    "base": script_id,
                    "target": new_target,
                    "template": template_name,
                    "version": version,
                    "strategy": "next_template",
                },
            ),
            "version": version,
            "template": template_name,
            "target_duration_sec": new_target,
            "scenes": scenes,
            "total_duration_sec": sum(scene["duration_sec"] for scene in scenes),
            "revised_from": script_id,
        }
        session.scripts[revised["script_id"]] = revised
        session.set_active_script(revised)
        return ToolResult.success("脚本已换模板，等待重新估算", revised)

    if strategy == "patch_scenes":
        patches = list(scene_patches or [])
        if not patches:
            raise ToolError("INVALID_PATCH", "patch_scenes 至少需要一个分镜修改")
        revised = deepcopy(script)
        by_scene = {scene["scene"]: scene for scene in revised["scenes"]}
        for patch in patches:
            scene_no = int(patch.get("scene") or 0)
            scene = by_scene.get(scene_no)
            if scene is None:
                raise ToolError("UNKNOWN_SCENE", f"分镜 {scene_no} 不存在")
            for field in (
                "narration",
                "visual",
                "duration_sec",
                "claim",
                "source_ids",
                "subtitle",
            ):
                if field in patch:
                    scene[field] = patch[field]
            if "duration_sec" in patch:
                scene["duration_sec"] = int(scene["duration_sec"])
            if "narration" in patch and "subtitle" not in patch:
                scene["subtitle"] = patch["narration"]
        revised["total_duration_sec"] = sum(scene["duration_sec"] for scene in revised["scenes"])
        revised["script_id"] = stable_id(
            "VID",
            {
                "base": script_id,
                "strategy": "patch_scenes",
                "patches": patches,
                "version": version,
            },
        )
        revised["version"] = version
        revised["template"] = f"{script.get('template', 'base')}+patch"
        revised["target_duration_sec"] = int(target_duration_sec or script["target_duration_sec"])
        revised["revised_from"] = script_id
        session.scripts[revised["script_id"]] = revised
        session.set_active_script(revised)
        return ToolResult.success("分镜已修改，等待重新估算", revised)

    if strategy == "drop_facts":
        remove = set(remove_source_ids or [])
        if not remove:
            raise ToolError("INVALID_PATCH", "drop_facts 至少需要一个素材 ID")
        remaining = [
            material_id
            for material_id in script.get("material_ids", [])
            if material_id not in remove
        ]
        if not remaining:
            raise ToolError("NO_FACT", "删除后没有可用素材事实")
        template_name, template = _template_for_total(session, current)
        scenes, facts = _build_scenes(
            session,
            template,
            script["topic"],
            script["audience"],
            remaining,
            focus_keywords,
        )
        revised = {
            **script,
            "script_id": stable_id(
                "VID",
                {
                    "base": script_id,
                    "strategy": "drop_facts",
                    "remove_source_ids": sorted(remove),
                    "version": version,
                },
            ),
            "version": version,
            "template": template_name,
            "scenes": scenes,
            "total_duration_sec": sum(scene["duration_sec"] for scene in scenes),
            "material_ids": remaining,
            "facts": facts,
            "revised_from": script_id,
        }
        session.scripts[revised["script_id"]] = revised
        session.set_active_script(revised)
        return ToolResult.success("已删除指定素材事实，等待重新估算", revised)

    if strategy == "redraft":
        template_name, template = _template_for_total(session, current)
        scenes, facts = _build_scenes(
            session,
            template,
            script["topic"],
            script["audience"],
            list(script.get("material_ids", [])),
            focus_keywords,
        )
        revised = {
            **script,
            "script_id": stable_id(
                "VID",
                {
                    "base": script_id,
                    "strategy": "redraft",
                    "instructions": instructions,
                    "focus_keywords": focus_keywords or [],
                    "version": version,
                },
            ),
            "version": version,
            "template": template_name,
            "scenes": scenes,
            "total_duration_sec": sum(scene["duration_sec"] for scene in scenes),
            "facts": facts,
            "revised_from": script_id,
        }
        session.scripts[revised["script_id"]] = revised
        session.set_active_script(revised)
        return ToolResult.success("脚本已重新生成，等待重新估算", revised)

    raise ToolError("UNKNOWN_STRATEGY", f"未知修改策略 {strategy}")


_ASSERTION_NUMBER = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|小时|分钟|秒|天|倍|元|万|GB|g|mAh|毫安)"
)
_ASSERTION_TERMS = (
    "提升",
    "降低",
    "增长",
    "翻倍",
    "顶级",
    "行业领先",
    "领先",
    "效率",
    "省时",
    "省钱",
    "省电",
    "续航",
    "100%",
)


def _normalize_assertion(text: str) -> str:
    return re.sub(r"\s+", "", str(text)).lower()


def _assertion_terms(text: str) -> list[str]:
    terms: set[str] = set()
    for match in _ASSERTION_NUMBER.finditer(str(text)):
        terms.add(match.group(0))
    for term in _ASSERTION_TERMS:
        if term in str(text):
            terms.add(term)
    return sorted(terms)


def _narration_scan_text(script: dict[str, Any], scene: dict[str, Any]) -> str:
    text = str(scene.get("narration") or "")
    # 主题、受众、平台、风格是用户输入，可能本身含“续航”“提升”等词；
    # 扫描前先去掉，避免把用户措辞当成 Agent 编造的产品结论。
    for value in (
        script.get("topic"),
        script.get("audience"),
        script.get("platform"),
        script.get("style"),
    ):
        if value:
            text = text.replace(str(value), "")
    return text


def _check(session: Session, script_id: str) -> dict[str, Any]:
    script = session.scripts.get(script_id)
    if script is None:
        raise ToolError("UNKNOWN_SCRIPT", f"未知脚本 {script_id}")
    text = " ".join(
        [str(script.get("title") or "")]
        + [str(scene.get("narration") or "") for scene in script["scenes"]]
        + [str(scene.get("visual") or "") for scene in script["scenes"]]
    )
    violations: list[dict[str, Any]] = []
    for banned in session.rules.banned_claims:
        if banned and banned in text:
            violations.append({"code": "BANNED_CLAIM", "message": f"命中品牌禁词：{banned}"})
    missing_source_scenes: set[int] = set()
    for scene in script["scenes"]:
        claim = scene.get("claim")
        if claim:
            if session.rules.require_source and not scene.get("source_ids"):
                missing_source_scenes.add(scene["scene"])
                violations.append(
                    {"code": "MISSING_SOURCE", "message": f"分镜 {scene['scene']} 的声明没有素材 ID"}
                )
            source_facts: set[str] = set()
            for source_id in scene.get("source_ids", []):
                material = session.materials.get(source_id)
                if material is None:
                    violations.append(
                        {"code": "UNKNOWN_SOURCE", "message": f"分镜 {scene['scene']} 引用了不存在的素材 {source_id}"}
                    )
                    continue
                source_facts.update(str(fact) for fact in material.get("facts", []))
            if claim not in source_facts:
                violations.append(
                    {
                        "code": "UNSUPPORTED_CLAIM",
                        "message": f"分镜 {scene['scene']} 的声明不属于所引素材：{claim}",
                    }
                )
    all_facts_text = _normalize_assertion(
        " ".join(
            str(fact)
            for material_id in script.get("material_ids", [])
            for fact in (session.materials.get(material_id) or {}).get("facts", [])
        )
    )
    for scene in script["scenes"]:
        terms = _assertion_terms(_narration_scan_text(script, scene))
        if not terms:
            continue
        source_ids = list(scene.get("source_ids") or [])
        if not source_ids:
            if scene["scene"] not in missing_source_scenes:
                missing_source_scenes.add(scene["scene"])
                violations.append(
                    {
                        "code": "MISSING_SOURCE",
                        "message": f"分镜 {scene['scene']} 的旁白包含事实性表述，但没有素材 ID",
                    }
                )
            allowed_text = all_facts_text
        else:
            allowed_text = _normalize_assertion(
                " ".join(
                    str(fact)
                    for source_id in source_ids
                    for fact in (session.materials.get(source_id) or {}).get("facts", [])
                )
            )
        for term in terms:
            if _normalize_assertion(term) not in allowed_text:
                violations.append(
                    {
                        "code": "UNSUPPORTED_ASSERTION",
                        "message": f"分镜 {scene['scene']} 的旁白包含素材未支持的说法：{term}",
                    }
                )
    total = int(script["total_duration_sec"])
    if total > session.rules.max_duration_sec:
        violations.append(
            {"code": "DURATION_MAX", "message": f"总时长 {total}s 超过品牌上限 {session.rules.max_duration_sec}s"}
        )
    if total < session.rules.min_duration_sec:
        violations.append(
            {"code": "DURATION_MIN", "message": f"总时长 {total}s 低于品牌下限 {session.rules.min_duration_sec}s"}
        )
    warnings = []
    if not any(scene.get("claim") for scene in script["scenes"]):
        warnings.append("脚本没有引用产品事实")
    if not any("提升" in str(scene.get("narration") or "") for scene in script["scenes"]):
        warnings.append("未使用效果数据，脚本未添加效果结论")
    result = {
        "check_id": stable_id("CHK", {"script_id": script_id, "total": total}),
        "script_id": script_id,
        "passed": not violations,
        "violations": violations,
        "warnings": warnings,
        "checked_total_duration_sec": total,
    }
    session.rule_checks.append(result)
    return result


def _save(session: Session, store: ScriptStore, script_id: str) -> dict[str, Any]:
    script = session.scripts.get(script_id)
    if script is None:
        raise ToolError("UNKNOWN_SCRIPT", f"未知脚本 {script_id}")
    estimate = next((item for item in reversed(session.estimates) if item["script_id"] == script_id), None)
    check = next((item for item in reversed(session.rule_checks) if item["script_id"] == script_id), None)
    if estimate is None or not estimate.get("within_target"):
        raise ToolError("DURATION_NOT_VERIFIED", "保存前必须先估算且满足目标时长")
    if check is None or not check.get("passed"):
        raise ToolError("RULE_CHECK_REQUIRED", "保存前必须通过品牌规则检查")
    status, record = store.put(script)
    data = {
        "script_id": script_id,
        "save_status": status,
        "status": record.get("status", "saved"),
        "editable": True,
        "published": False,
        "total_duration_sec": script["total_duration_sec"],
    }
    session.save_attempts.append({"ok": True, "data": data})
    return data


def _finish(
    session: Session,
    status: str,
    summary: str,
    risks: list[Any] | None = None,
    gap: dict[str, Any] | None = None,
) -> dict[str, Any]:
    allowed = {
        "completed",
        "needs_input",
        "no_material",
        "cannot_meet_duration",
        "rule_failed",
        "tool_error",
        "save_failed",
    }
    if status not in allowed:
        raise ToolError("INVALID_STATUS", f"status 必须是 {sorted(allowed)} 之一")
    saved = session.saved_script()
    active = session.active_script()
    if status == "completed":
        if not saved:
            raise ToolError("EVIDENCE_MISMATCH", "没有已保存脚本，不能标记 completed")
        script_id = saved["script_id"]
        estimate = next((item for item in reversed(session.estimates) if item["script_id"] == script_id), None)
        check = next((item for item in reversed(session.rule_checks) if item["script_id"] == script_id), None)
        if not estimate or not estimate.get("within_target"):
            raise ToolError("EVIDENCE_MISMATCH", "缺少满足目标时长的估算")
        if not check or not check.get("passed"):
            raise ToolError("EVIDENCE_MISMATCH", "缺少通过的规则检查")
    if status == "needs_input" and not session.request.needs_input:
        raise ToolError("EVIDENCE_MISMATCH", "请求字段完整，不能标记 needs_input")
    if status == "no_material":
        if active or not session.searches or session.searches[-1].get("hits"):
            raise ToolError("EVIDENCE_MISMATCH", "已有素材或检索命中，不能标记 no_material")
    if status == "cannot_meet_duration":
        if not session.revision_errors:
            raise ToolError("EVIDENCE_MISMATCH", "缺少压缩失败的证据")
        if not session.estimates or session.estimates[-1].get("within_target"):
            raise ToolError("EVIDENCE_MISMATCH", "缺少超时长的估算证据")
    if status == "rule_failed":
        if not session.rule_checks or session.rule_checks[-1].get("passed"):
            raise ToolError("EVIDENCE_MISMATCH", "缺少规则检查失败的证据")
    payload = {"status": status, "summary": summary, "risks": risks or [], "gap": gap}
    session.finish_payload = payload
    return payload


def build_tools(session: Session, store: ScriptStore, faults: dict[str, int] | None = None) -> ToolRegistry:
    search_tool = Tool(
        name="search_materials",
        description="检索产品素材事实，返回素材 ID 与可引用事实。",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        handler=lambda query: ToolResult.success("素材检索完成", _search(session, query)),
    )
    draft_tool = Tool(
        name="draft_script",
        description="根据主题、平台、目标时长和素材生成分镜脚本。脚本只引用素材事实。",
        parameters={
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "audience": {"type": "string"},
                "platform": {"type": "string"},
                "target_duration_sec": {"type": "integer"},
                "style": {"type": "string"},
                "material_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["topic", "platform", "target_duration_sec", "material_ids"],
        },
        handler=lambda topic, platform, target_duration_sec, material_ids, audience="通用受众", style="自然口语": ToolResult.success(
            "脚本初稿生成",
            _draft(session, topic, audience, platform, target_duration_sec, style, material_ids),
        ),
    )
    estimate_tool = Tool(
        name="estimate_duration",
        description="按分镜逐项计算预计总时长，并判断是否超过目标时长。",
        parameters={
            "type": "object",
            "properties": {"script_id": {"type": "string"}},
            "required": ["script_id"],
        },
        handler=lambda script_id: ToolResult.success("时长估算完成", _estimate(session, script_id)),
    )
    revise_tool = Tool(
        name="revise_script",
        description="基于当前脚本做增量修改：换模板、改分镜、删事实或重新生成。每次生成新版本。",
        parameters={
            "type": "object",
            "properties": {
                "script_id": {"type": "string"},
                "target_duration_sec": {"type": "integer"},
                "strategy": {
                    "type": "string",
                    "enum": ["next_template", "patch_scenes", "drop_facts", "redraft"],
                },
                "direction": {
                    "type": "string",
                    "enum": ["target", "shorter", "longer"],
                },
                "scene_patches": {
                    "type": "array",
                    "items": {"type": "object"},
                },
                "remove_source_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "focus_keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "instructions": {"type": "string"},
            },
            "required": ["script_id"],
        },
        handler=lambda script_id, target_duration_sec=None, strategy="next_template", direction="target", scene_patches=None, remove_source_ids=None, focus_keywords=None, instructions="": _revise(
            session,
            script_id,
            target_duration_sec,
            strategy=strategy,
            direction=direction,
            scene_patches=scene_patches,
            remove_source_ids=remove_source_ids,
            focus_keywords=focus_keywords,
            instructions=instructions,
        ),
    )
    check_tool = Tool(
        name="check_rules",
        description="检查品牌禁词、事实来源、素材 ID 和时长上下限。",
        parameters={
            "type": "object",
            "properties": {"script_id": {"type": "string"}},
            "required": ["script_id"],
        },
        handler=lambda script_id: ToolResult.success("规则检查完成", _check(session, script_id)),
    )
    save_tool = Tool(
        name="save_script",
        description="保存已通过时长估算和规则检查的可编辑脚本。",
        parameters={
            "type": "object",
            "properties": {"script_id": {"type": "string"}},
            "required": ["script_id"],
        },
        handler=lambda script_id: _save_result(session, store, script_id),
    )
    finish_tool = Tool(
        name="finish",
        description="结束任务。completed 必须有已保存脚本、达标时长和通过的规则检查。",
        parameters={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": [
                        "completed",
                        "needs_input",
                        "no_material",
                        "cannot_meet_duration",
                        "rule_failed",
                        "tool_error",
                        "save_failed",
                    ],
                },
                "summary": {"type": "string"},
                "risks": {"type": "array"},
                "gap": {"type": "object"},
            },
            "required": ["status", "summary"],
        },
        handler=lambda status, summary, risks=None, gap=None: ToolResult.success(
            "任务结束", _finish(session, status, summary, risks, gap)
        ),
    )
    return ToolRegistry(
        [search_tool, draft_tool, estimate_tool, revise_tool, check_tool, save_tool, finish_tool],
        faults=faults,
    )


def _save_result(session: Session, store: ScriptStore, script_id: str) -> ToolResult:
    try:
        return ToolResult.success("脚本已保存", _save(session, store, script_id))
    except ToolError as exc:
        session.save_attempts.append({"ok": False, "error": exc.code})
        raise

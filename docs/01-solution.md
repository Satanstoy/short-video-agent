# 01 方案说明

## 1. 目标与不变量

目标：生成一份事实可追溯、能满足目标时长和品牌规则的可编辑脚本。

不变量：

1. 分镜里的产品声明必须来自素材事实，并带素材 ID。
2. 预计总时长必须等于分镜 `duration_sec` 之和。
3. 超过目标时长必须换合适的模板并重新估算，不能只改总时长数字。
4. 压缩到最短仍不达标时明确失败，不保存。
5. `completed` 必须有保存记录、达标估算和通过的规则检查。

## 2. 执行流程

```text
VideoRequest
  -> search_materials
       无结果 -> 改写查询一次
  -> draft_script（初稿 38 秒）
  -> estimate_duration
       -> 超目标 -> revise_script（按目标选 34 秒或 28 秒模板）
       -> 重新 estimate_duration
  -> check_rules
       -> 违规 -> finish(rule_failed)
  -> save_script
  -> finish(completed)
  -> report
```

`OfflineEngine` 和 `OpenAIEngine` 共用 `run_loop`、工具注册表、Trace 和报告。

## 3. 请求与素材

`VideoRequest` 必填 `topic`、`platform`、`target_duration_sec`。缺失时收集 `missing_fields`，只调用 `finish(needs_input)`，不检索、不生成。

素材在 `data/materials.json`：

```json
{"id": "P01", "facts": ["支持字幕"], "tags": ["字幕", "产品", "功能"]}
```

`search_materials` 用关键词和标签匹配。没有命中时改写一次；仍无命中返回 `no_material`，不生成空脚本。

## 4. 分镜与时长

初稿模板：

| 分镜 | 时长 |
| --- | ---: |
| 开场钩子 | 5 |
| 场景痛点 | 9 |
| 功能说明 | 14 |
| 画面演示 | 6 |
| 行动引导 | 4 |
| 合计 | 38 |

压缩模板有两档：

| 模板 | 分镜时长 | 合计 |
| --- | --- | ---: |
| compact | 5 + 7 + 12 + 6 + 4 | 34 |
| compressed | 4 + 6 + 10 + 4 + 4 | 28 |

`revise_script` 在所有不超过目标的模板里选总时长最接近目标的一档，生成新的脚本版本，`revised_from` 指回初稿；`estimate_duration` 对每个分镜求和，返回 `scene_durations`、`within_target` 和超出秒数。

目标 35 秒时选 compact 34，不会直接压到 28；目标 30 秒时 compact 34 超目标，选 compressed 28。`templates.json` 的 `min_duration_sec=28`，目标低于 28 时返回 `CANNOT_COMPRESS`，策略输出 `cannot_meet_duration` 和缺口，不保存。

## 5. 事实追溯

每个分镜包含：

```json
{
  "scene": 3,
  "duration_sec": 14,
  "narration": "先看核心事实：支持字幕。",
  "subtitle": "先看核心事实：支持字幕。",
  "claim": "支持字幕",
  "source_ids": ["P01"]
}
```

`check_rules` 做五类检查：

- 禁词：命中 `data/brand_rules.json` 的 `banned_claims`。
- 声明：`claim` 必须出现在该分镜所引素材的 facts 里。
- 来源：有 claim 的分镜必须有 `source_ids`，且 ID 存在。
- 旁白断言：扫描旁白里的数字、百分比和效果词，要求出现在所引素材事实里；扫描前去掉主题、受众等用户输入，避免误伤用户措辞。无来源的断言记 `MISSING_SOURCE`，有来源但事实不支持记 `UNSUPPORTED_ASSERTION`。
- 时长：总时长在品牌上下限内。

多素材时，`_build_scenes` 不按传入顺序分配事实，而是用镜头名、画面、`claim_intent` 和请求主题/受众做词面匹配，选与当前镜头最接近的素材事实。素材多于镜头时允许复用，但复用也按匹配分数选。

没有效果数据时，脚本不写“效率提升 80%”，规则检查把这条列为警告。旁白断言扫描是启发式护栏，不覆盖所有语义改写。

## 6. 失败处理

| 失败 | 行为 |
| --- | --- |
| 检索失败 | 改写查询重试一次；仍失败 `tool_error` |
| 无素材 | `no_material`，不生成分镜 |
| 脚本生成失败 | 重试一次；仍失败 `tool_error` |
| 估算失败 | 重试一次；仍失败 `tool_error`，初稿保留 |
| 压缩失败 | 目标低于 28 秒返回 `cannot_meet_duration` |
| 目标 35 秒 | 选 compact 34 秒，重新估算后继续 |
| 规则失败 | `rule_failed`，列出违规项，不保存 |
| 保存失败 | 重试一次；仍失败 `save_failed` |
| 步数/调用上限 | `incomplete`，不伪造完成 |

## 7. 保存与幂等

`ScriptStore` 以 `script_id` 为键写入 `scripts.json`。同一脚本重复保存返回 `existing`，不会产生第二条记录。保存前必须有同一脚本的达标估算和通过检查；否则返回 `DURATION_NOT_VERIFIED` 或 `RULE_CHECK_REQUIRED`。

## 8. 评估

25 个测试覆盖正常、38→28 修正、38→34 选择、分镜求和、缺字段、无素材、估算失败、不可满足时长、无编造效果、未支持声明、旁白额外断言、主题含效果词不误报、禁词、声明与所引素材不匹配、多素材来源映射、多素材非顺序分配、保存前置条件、SQLite 版本链、多轮改稿、回退和审核。7 组示例有 input/output/trace/manifest，`01-normal` 另有两轮保存验证幂等。

离线策略是确定性模板，不证明创作质量；`live` 模式可以让模型写文案和分镜，但工具层的时长、来源和规则校验不变。

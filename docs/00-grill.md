# 00 Grill 问题与回答（短视频脚本）

用法：先看 30 秒介绍，再按问题自测。答案里带代码和示例位置，现场可以直接打开分镜与 Trace。

## 30 秒介绍

创作者给主题、平台和目标时长，Agent 先检索素材事实，再生成有分镜、旁白、字幕和来源 ID 的脚本。初稿 38 秒、目标 30 秒时，它会压缩到 28 秒并重新估算；目标 15 秒做不到时明确失败。每个产品声明都要能在素材事实里找到，找不到就判违规，不写“效率提升 80%”这类没有依据的结论。

## 一、需求与边界

### Q1：为什么先列分镜时长再估算，而不是让模型直接说总时长？

总时长必须可核对。`draft_script` 生成每个分镜的 `duration_sec`，`estimate_duration` 对分镜求和，输出里还有逐项 `scene_durations`。如果模型直接写“大约 30 秒”，评审无法判断它是算出来的还是猜的。测试 `test_storyboard_durations_sum_to_estimate` 校验求和一致。

### Q2：参考实例是 38 秒压到 28 秒，具体怎么压？

初稿模板是 5 + 9 + 14 + 6 + 4 = 38 秒。目标 30 秒时，`revise_script` 在不超过目标的模板里选最接近的一档：compact 34 秒仍然超目标，只能选 compressed 的 4 + 6 + 10 + 4 + 4 = 28 秒，生成新的 `script_id` 和 `version=2`，再调用一次 `estimate_duration`。Trace 里能看到 38 的 rejected 和 28 的 accepted，见 `examples/02-over-duration/trace.json`。

目标 35 秒时，compact 34 秒不超目标，且比 28 秒更接近目标，所以选 compact，见 `examples/07-target-35`。这样不会把 35 秒需求直接压成 28 秒。

### Q3：为什么最短是 28 秒？目标 15 秒怎么办？

压缩模板已经去掉了场景铺垫和一半功能解释，再压就会丢失产品事实或行动引导。`templates.json` 把最紧的 `compressed` 模板和 `min_duration_sec` 都定为 28；目标低于 28 时 `revise_script` 返回 `CANNOT_COMPRESS`，策略输出 `cannot_meet_duration`，`gap.over_by_sec=13`，不保存。`examples/06-impossible-duration` 就是这个路径。

### Q4：为什么不能为了满足 15 秒删掉事实来源？

可以删镜头，但不能删到没有产品事实，也不能把多个事实压成未经素材支持的结论。当前选择是明确失败，让创作者改目标时长或补素材。这比交一个“15 秒但事实不全”的脚本更符合题目对事实可追溯的要求。

## 二、事实与规则

### Q5：怎么保证脚本没有编造产品事实？

`draft_script` 只从 `search_materials` 返回的 `facts` 里取文本，分镜的 `claim` 直接写事实原文；`check_rules` 再遍历所有 `claim`，要求它出现在所引素材的 `facts` 里，同时 `source_ids` 必须存在。除此之外，`check_rules` 会扫描每句旁白里的数字、百分比和效果词（提升、翻倍、续航等），要求它们出现在所引素材事实里；无来源的断言记 `MISSING_SOURCE`，有来源但事实不支持记 `UNSUPPORTED_ASSERTION`。`test_rule_checker_rejects_unsupported_claim` 把 claim 改成“效率提升 80%”，`test_rule_checker_rejects_unsupported_narration_assertion` 把旁白改成“续航翻倍”，都会失败。

这是启发式护栏，不是完整语义推理：扫描前会去掉主题、受众、平台、风格这些用户输入，避免把用户自己的措辞当成 Agent 编造；但同义改写、没有数字和效果词的暗示仍可能漏过，生产环境要接逐句 NLI 或人工审稿。

### Q6：P01 只有“支持字幕”，脚本会不会写成“提升效率”？

不会。脚本只使用 P01 的事实，功能说明和画面演示都围绕“支持字幕”。`check_rules` 还会给一条警告：“未使用效果数据，脚本未添加效果结论”。`test_no_fabricated_effect_claim` 检查分镜文本里没有 80% 和效率提升。

### Q7：品牌规则检查哪些东西？

五类：禁词（如“行业第一”“国家级”“100%”）、声明是否属于所引素材的事实、分镜是否带素材 ID、旁白里的数字/效果词是否有素材支持、总时长是否在品牌上下限内。违规项写进 `violations`，警告写进 `warnings`。规则在 `data/brand_rules.json`，检查逻辑在 `tools._check`。

### Q8：规则检查不通过时 Agent 会怎么做？

策略拿到 `passed=false` 后调用 `finish(rule_failed)`，把违规项作为 `risks` 返回，不保存脚本。当前 demo 没有做“自动改写禁词”的修复，因为禁词替换可能改变语义；先明确失败更安全。单元测试覆盖了禁词和未支持声明两条。

## 三、检索与生成

### Q9：检索不到素材时怎么处理？

先按主题检索一次，无结果就改写查询重试一次；两次都没有命中，`finish(no_material)`，不生成任何分镜。`examples/04-no-material` 问“量子计算”，输出没有 `storyboard`，`material_sources=[]`。这里没有加“产品”这种泛词去凑结果，避免把无关素材硬塞进脚本。

### Q10：多个素材时，事实怎么分配到分镜？

`_build_scenes` 先汇总所有素材的 facts，再按镜头名、画面、`claim_intent` 和请求主题/受众做词面匹配，选与当前镜头最接近的素材事实，`source_ids` 填对应素材 ID。不是按传入顺序硬分配：即使 `material_ids` 是 `["P02", "P01"]`，功能说明镜头仍会选 P01 的“支持字幕”，画面演示镜头再用 P02 的续航事实，`test_multi_material_selection_is_not_input_order` 覆盖这个顺序。

边界是词面匹配，不是 embedding 语义匹配；素材多、表达相近时仍可能错配，生产化要换成向量检索或让模型在候选事实里选择。

### Q11：为什么用模板生成，而不是让模型自由写？

模板保证每次都有完整分镜、时长、旁白和来源，验收可复现。自由生成更容易出现“写了产品事实但没标来源”“分镜时长加起来不对”这类问题。`live` 模式可以让模型决定文案和分镜，但工具层的时长、来源和规则校验不变。当前离线 demo 优先证明护栏，不证明创意质量。

### Q12：时长估算准吗？

估算是“分镜时长求和”，不是语音合成后的真实时长。它适合做约束检查，不适合做最终剪辑时间。真正上线要接 TTS 语速、镜头节奏和平台审片规则；当前 demo 明确标注这是估算值。

## 四、失败与状态

### Q13：估算工具连续失败时，初稿还在吗？

在。`examples/05-tool-failure` 注入两次估算失败，输出仍保留 `script.script_id` 和完整 `storyboard`，但 `duration_ok=false`、`script.saved=false`、`task_status=tool_error`。失败步骤也保留在 `plan_steps` 和 `failed_tool_calls` 里。

### Q14：保存失败会怎样？

`save_script` 连续失败两次就 `finish(save_failed)`，不把脚本说成已完成。保存的幂等键是 `script_id`，重复保存返回 `existing`。`examples/01-normal/output-run2.json` 就是第二次运行，`save_status=existing`，`scripts.json` 里只有一条。

### Q15：`finish(completed)` 的条件是什么？

必须有已保存脚本、同一脚本的达标估算、同一脚本通过的规则检查。少一项就返回 `EVIDENCE_MISMATCH`，循环继续。这样模型不能只写一句“已完成”就结束。

### Q16：缺平台或目标时长为什么不在 CLI 直接报错？

这是业务输入不完整，Agent 要给出结构化缺口。`VideoRequest.from_dict` 收集 `topic`、`platform`、`target_duration_sec` 的缺失字段，策略只调用 `finish(needs_input)`，不检索、不生成。`examples/03-missing-info` 输出缺平台和目标时长。

## 五、评估与取舍

### Q17：怎么证明 Agent 会根据检查结果调整？

Trace 的动作链是：初稿 38 → 估算超目标 → 压缩 28 → 重新估算 → 规则检查 → 保存。`examples/02-over-duration` 的 `revision.attempts` 同时保留 38 和 28 两次估算。这不是“生成一次就结束”的串行调用。

### Q18：这个 demo 最大的不足是什么？

脚本创作多样性有限，压缩只有 38/34/28 三档、不做逐镜头重写；事实分配是词面匹配，不是 embedding 语义匹配；旁白断言检查只覆盖数字、百分比和效果词，不做完整 NLI；品牌规则是合成规则，不覆盖真实广告法。它证明的是时长校验、事实追溯和失败处理，不证明内容质量。生产化要接真实素材库、TTS 时长、向量检索和更细的规则引擎。

### Q19：如果评审把目标时长改成 20 秒，会发生什么？

初稿 38 秒，估算超目标，`revise_script` 发现 20 < 28，返回 `CANNOT_COMPRESS`，输出 `cannot_meet_duration`，缺口 8 秒，不保存。这个分支和 15 秒共用同一条代码路径，区别只是 `gap.over_by_sec`。

### Q20：如果重做，优先改什么？

先把“按语义选事实”从词面匹配升级成向量检索或模型候选选择，避免多素材时错配；再把压缩改成逐镜头重写，允许在 28 秒以下保留核心事实；然后给旁白断言补逐句 NLI 或人工审稿；最后补真实模型的 live Trace，对比模型脚本和模板脚本的规则通过率。当前版本优先保证验收路径可复现。

### Q21：连续改稿如何保留证据？用户说“就用这版”会直接批准吗？

每轮在 SQLite 里留轮次和工具调用，修改产生带父版本的新脚本。改稿后需要重新估算和检查，成功保存才会更新当前候选。自然语言“就用这版”只会提示用户执行人工审核命令。回退只切换到已验证或已批准的候选，历史版本仍可比较和导出。见 [多轮 CLI 实录](../examples/08-multi-turn/transcript.txt)。

## 六、现场演示

```bash
python3 -m pytest -q

python3 run.py run \
  --input examples/02-over-duration/input.json \
  --state-dir /tmp/grill-video

python3 - <<'PY'
import json
d = json.load(open('/tmp/grill-video/output.json'))
print(d['task_status'], d['estimated_total_sec'], d['duration_ok'])
print([(x['total_duration_sec'], x['within_target']) for x in d['revision']['attempts']])
for scene in d['storyboard']:
    print(scene['scene'], scene['duration_sec'], scene['claim'], scene['source_ids'])
PY
```

现场重点看 `revision.attempts` 的 38/28、每个分镜的时长求和、`source_ids` 和 `rule_check.passed`。

## 七、不会答时的边界

- 被问创意质量：承认模板生成，demo 重点是时长和事实护栏。
- 被问真实拍摄时长：估算是分镜求和，不是 TTS 或成片时长。
- 被问广告法：规则是合成示例，不覆盖真实法规。
- 被问真实模型：离线 Trace 只证明流程，live 需要自备 key。

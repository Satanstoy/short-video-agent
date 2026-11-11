# 短视频脚本 Agent（项目题 4 · 小 demo）

输入主题、受众、平台、目标时长和风格，Agent 检索产品素材、生成分镜脚本、估算时长、检查品牌规则。初稿超时后压缩并重新估算；素材不够或时长做不到时明确失败，不编产品事实。

默认 `offline` 是确定性参考策略，用于复现验收；`live` 走 OpenAI-compatible function calling。离线结果不代表真实模型创作质量。

## 快速开始

```bash
python3 -m pytest -q
```

正常生成，目标 45 秒，初稿 38 秒：

```bash
python3 run.py run \
  --input examples/01-normal/input.json \
  --state-dir .runs/normal \
  --output /tmp/video-normal.json
```

目标 30 秒，初稿 38 秒，压缩到 28 秒：

```bash
python3 run.py run \
  --input examples/02-over-duration/input.json \
  --state-dir .runs/short \
  --output /tmp/video-short.json
```

多轮改稿，任务和版本存 SQLite：

```bash
python3 run.py task create \
  --input examples/02-over-duration/input.json \
  --db .runs/agent.db

python3 run.py task chat <task_id> --db .runs/agent.db
```

```text
> 换一个更直接的开头
> 改成 35 秒
> /diff 2 3
> /rollback 3
> /history
> /approve 3
> /export 3
```

`task` 命令保存任务、轮次、工具调用、版本、估算、规则检查和审核记录；`run` 保持原来的单轮 JSON 输出。

估算工具连续失败：

```bash
python3 run.py run \
  --input examples/05-tool-failure/input.json \
  --state-dir .runs/fail \
  --fault estimate_duration:2
```

[多轮 CLI 实录](examples/08-multi-turn/transcript.txt)展示了改开头、改目标时长、比较版本、回退、人工审核和 Markdown 导出。回退只接受通过校验或已批准的版本，每个版本保留一条审核决定。独立命令 `task rollback <task_id> --version 3` 与 REPL 的 `/rollback 3` 行为一致。

## 验收场景

| 场景 | 入口 | 实际结果 |
| --- | --- | --- |
| 正常生成 | `examples/01-normal` | 38 秒，目标 45 秒，规则通过，脚本保存 |
| 超时长修正 | `examples/02-over-duration` | 38 秒 → 压缩 28 秒 → 重新估算，目标 30 秒 |
| 缺关键信息 | `examples/03-missing-info` | `needs_input`，缺平台和目标时长，无脚本 |
| 无素材 | `examples/04-no-material` | `no_material`，不生成分镜，不编事实 |
| 工具失败 | `examples/05-tool-failure` | 估算失败两次，初稿保留，`tool_error`，不保存 |
| 无法满足时长 | `examples/06-impossible-duration` | 最短 28 秒，目标 15 秒，`cannot_meet_duration`，缺口 13 秒 |
| 目标 35 秒 | `examples/07-target-35` | 38 秒 → compact 34 秒，避免直接压到 28 秒 |
| 重复保存 | `examples/01-normal/output-run2.json` | 同一 script ID，`save_status=existing` |

每个示例目录有 `input.json`、`output.json`、`trace.json`、`manifest.json`，状态文件为 `scripts.json`。`01-normal` 另有两轮输出和 Trace。

## 超时长修正实例

`examples/02-over-duration` 的 Trace：

```text
search_materials   ok         命中 P01，事实“支持字幕”
draft_script       ok         初稿 38 秒
estimate_duration  ok         38 > 30，within_target=false
revise_script      ok         压缩版 28 秒，version=2
estimate_duration  ok         28 <= 30，within_target=true
check_rules        ok         passed=true
save_script        ok         VID-0A556D34E6
finish             ok         completed
```

输出关键字段：

```json
{
  "task_status": "completed",
  "estimated_total_sec": 28,
  "target_duration_sec": 30,
  "duration_ok": true,
  "material_sources": [{"id": "P01", "facts": ["支持字幕"]}],
  "revision": {
    "attempts": [
      {"total_duration_sec": 38, "within_target": false},
      {"total_duration_sec": 28, "within_target": true}
    ],
    "final_version": 2
  }
}
```

目标 15 秒时，`revise_script` 返回 `CANNOT_COMPRESS`，输出 `cannot_meet_duration`，`gap.over_by_sec=13`，不保存。

目标 35 秒时，`revise_script` 不直接选 28 秒压缩版，而是在不超目标的模板里选最接近的 `compact` 34 秒。见 `examples/07-target-35`：38 → 34，`script.template=compact`。

## 工具与护栏

| 工具 | 作用 | 关键校验 |
| --- | --- | --- |
| `search_materials` | 检索素材事实 | 空查询拒绝；无结果改写一次 |
| `draft_script` | 生成分镜、旁白、字幕、素材来源 | 必须有素材 ID 和事实；只引用素材事实 |
| `estimate_duration` | 逐分镜求和，判断是否超目标 | 脚本必须存在；每项时长相加 |
| `revise_script` | 按目标选最接近且不超时的模板，生成新版本 | 低于最短 28 秒返回 `CANNOT_COMPRESS` |
| `check_rules` | 检查禁词、事实来源、素材 ID、旁白断言、时长上下限 | 声明和旁白里的数字/效果词必须能在素材事实中找到 |
| `save_script` | 保存可编辑脚本 | 必须先达标估算并通过规则检查 |
| `finish` | 结束并汇报 | `completed` 必须有保存记录、达标估算、通过的检查 |

每个分镜都有 `duration_sec`、`narration`、`subtitle`、`claim`、`source_ids`。没有效果数据时，脚本不会写“效率提升 80%”；规则检查会提示未使用效果数据。

## 输出口径

- `task_status`：`completed` / `needs_input` / `no_material` / `cannot_meet_duration` / `rule_failed` / `tool_error` / `save_failed`。
- `title`、`storyboard`：标题和分镜；每个分镜有旁白、字幕、时长和来源。
- `estimated_total_sec`、`target_duration_sec`、`duration_ok`：最终估算与目标。
- `revision.attempts`：每次估算的时长和是否达标，能看到 38 → 28 或 38 → 34。
- `material_sources`：实际被分镜引用的素材 ID 和可引用事实。
- `rule_check`：是否通过、违规项、警告。
- `script`：脚本 ID、版本、模板、是否可编辑、是否保存、`published=false`。

## 目录

```text
src/short_video/
  miniagent.py   # 手写工具循环、Trace、OpenAI 适配器
  domain.py      # 请求、素材、品牌规则、模板
  session.py     # 会话状态、JSON 脚本存储
  tools.py       # 七个工具及护栏
  policy.py      # 离线参考策略
  report.py      # 输出组装
  runner.py      # 组装与执行
  cli.py         # 命令行
tests/           # 28 个测试
data/            # 合成素材、品牌规则、分镜模板
examples/        # 7 组单轮验收示例和 1 组多轮 CLI 实录
docs/00-grill.md # 面试追问与回答
scripts/         # 示例生成脚本
```

## 真实模型（可选）

默认离线模式不需要网络。要接真实模型，复制 `.env.example` 为 `.env`，填入 `OPENAI_API_KEY`、`OPENAI_MODEL`，运行命令加 `--engine live`。模型走标准 function calling，工具和校验与离线模式共用。多轮模式在 `task create` 和 `task chat` 上同样支持 `--engine live`，轮次与工具结果写入 SQLite。当前验收覆盖离线流程及模拟模型调用。

## 已知边界

- 脚本由分镜模板生成，创作多样性有限；真实模型可以替换模板，但仍要过分镜时长和事实校验。
- 压缩是三档模板（38/34/28），按目标选最接近且不超时的一档；不做逐镜头重写，也不保证填满目标时长。
- 旁白断言扫描是启发式：只拦数字、百分比和效果词，覆盖不了所有语义改写；完整事实校验仍要逐句 NLI 或人工审稿。
- 多素材按镜头意图和素材标签/标题做词面匹配，不是 embedding 语义匹配；素材多、表达相近时仍可能错配。
- 品牌规则是合成规则，不覆盖真实平台的全部广告法要求。
- 素材库只有三条事实，没有做大规模事实抽取。
- JSON 存储只保证单机顺序，不做跨机器并发。
- 不发布视频，只保存可编辑脚本。

实现细节见 [`docs/01-solution.md`](docs/01-solution.md)，多轮 Agent 设计见 [`docs/02-agent-design.md`](docs/02-agent-design.md)，面试追问见 [`docs/00-grill.md`](docs/00-grill.md)。

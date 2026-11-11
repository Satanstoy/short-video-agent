# 02 Agent 设计（多轮短视频脚本）

状态：多轮改稿、人工审核、回退、导出和 live 适配入口均已实现。离线端到端实录见 [examples/08-multi-turn](../examples/08-multi-turn/transcript.txt)，live 路径通过模拟模型验证。

目标：把现在的单轮脚本生成 demo 改成可连续改稿的 CLI Agent。一个任务可以经过多轮自然语言修改，每次修改产生不可变版本；用户可以查看版本差异、回退、审核和导出。离线策略与真实模型共用同一套工具、状态和校验。

## 1. 设计结论

- 形态：继续 CLI，新增 `task` 子命令和交互式 `chat`，保留现有单轮 `run`。
- 架构：单 Agent、单 ReAct 循环；每轮反复“选工具 -> 执行 -> 看结果”，不拆 planner / writer / checker。
- 用户：内容运营内部工具，一次服务一个任务，人工挑选和审核。
- 存储：SQLite 保存任务、轮次、工具调用、脚本版本、估算、规则检查、审核记录。
- 修改：优先增量修改当前版本，保留已通过的分镜和素材事实；增量做不到时重新生成。
- 审核：生成并通过检查后自动存为候选版本；`approve` 是人工命令，Agent 不能自己批准。
- 运行时：离线确定性策略负责可复现验收，真实模型负责创意；两者共用工具契约和状态机。

## 2. 用户流程

```text
创建任务（主题、受众、平台、目标时长、风格）
  -> Agent 检索素材、生成初稿、估算、检查、保存候选
  -> 用户看分镜和风险
  -> 用户说“再短一点”“换开头”“不要提续航”
  -> Agent 增量修改，产生新版本，重新估算和检查
  -> 用户对比版本、回退或继续改
  -> 用户 approve 某一版
  -> 导出分镜表或 JSON
```

一个任务只对应一个视频 brief。需要换主题时新建任务，不在旧任务里改 brief，避免版本链混在一起。

## 3. CLI 设计

保留现有命令：

```bash
python3 run.py run --input examples/02-over-duration/input.json --state-dir /tmp/video
```

新增任务命令：

```bash
python3 run.py task create --input examples/02-over-duration/input.json
python3 run.py task chat <task_id>
python3 run.py task show <task_id>
python3 run.py task history <task_id>
python3 run.py task diff <task_id> --from 2 --to 3
python3 run.py task approve <task_id> --version 3
python3 run.py task reject <task_id> --version 3 --reason "开头太慢"
python3 run.py task rollback <task_id> --version 3
python3 run.py task export <task_id> --version 3 --format md
python3 run.py task list
```

`task create` 创建任务并跑第一轮生成，打印 `task_id`、版本号、时长、规则检查结果和当前候选状态。

`task chat` 是 REPL。普通文本交给 Agent，斜杠命令直接操作本地状态：

```text
> 再短一点
> 换一个更直接的开头
> 不要提续航
> /history
> /diff 2 3
> /approve 3
> /export 3
> /quit
```

REPL 命令不经过模型：

- `/show`：当前版本摘要和分镜。
- `/history`：所有版本、时长、规则状态、审核状态。
- `/diff <from> <to>`：版本差异。
- `/approve <version>`：批准指定版本。
- `/reject <version> <reason>`：驳回指定版本。
- `/rollback <version>`：把当前版本指针移回通过校验或已批准的历史版本，历史记录继续保留。
- `/export <version> [md|json]`：导出。
- `/quit`：退出。

`approve` 和 `reject` 不做成 Agent 工具。审核是人的决定，Agent 只能建议，不能自己批准。

默认数据库路径 `.runs/agent.db`，可用 `--db` 覆盖。单轮 `run` 继续写 `state-dir` 下的 JSON，避免破坏现有示例和测试。

## 4. Agent 架构

单 Agent，单 ReAct 循环。每一轮用户消息启动一次循环，循环里反复“选择工具 -> 执行 -> 观察结果”，直到结束。

```text
用户消息
  -> 构造上下文：任务 brief、当前版本、最近轮次、约束
  -> next_action（离线策略 / 真实模型）
       -> ToolCalls / TextOnly / 澄清
  -> ToolRegistry 执行工具
       -> 参数校验、业务校验
       -> 每次调用写 tool_calls
       -> 版本、估算、检查、候选保存写 SQLite
       -> 结果作为 observation 回填
  -> 未结束则回到 next_action
  -> finish / 澄清 / 超限后写 turn 状态，生成回复
```

循环只有四个边界：

| 边界 | 负责 | 不负责 |
| --- | --- | --- |
| `next_action` | 看当前上下文和上一步 observation，选下一步 | 直接改脚本、写数据库 |
| `ToolRegistry` | 校验参数、执行工具、返回结构化结果 | 解释用户意图 |
| `TurnRecorder` | 记录工具调用、版本、估算、检查、轮次状态 | 决定下一步 |
| `Responder` | 把本轮工具结果和版本差异渲染成回复 | 发明工具没有返回的结论 |

离线模式和 live 模式只是 `next_action` 的实现不同：离线是确定性策略，live 是 function calling。工具、存储、校验、回复共用。多轮是外层 REPL 再启动一次循环，不引入第二个 agent，也不预先锁定整轮动作列表。

## 5. 多轮修改

### 5.1 意图

| 意图 | 示例 | 默认动作 |
| --- | --- | --- |
| `revise_duration` | “再短一点”“改成 30 秒”“长一点” | 换模板或改目标时长，再估算 |
| `revise_content` | “换开头”“结尾更有行动感”“这句改短” | 修改指定分镜，再估算和检查 |
| `revise_constraint` | “不要提续航”“突出字幕”“别说行业第一” | 删事实、换重点或加禁用项，再检查 |
| `rollback` | “回到上一版”“用回 28 秒那版” | 移动当前版本指针，不新建版本 |
| `approve` | “就用这版”“可以了” | 提示用户执行 `/approve`，Agent 不直接批准 |
| `question` | “为什么是 28 秒”“这版有什么风险” | 从估算、检查、版本记录回答，不改版本 |
| `unknown` | 无法归类 | 问一个澄清问题，不改版本 |

离线策略用固定短语和正则解析意图，真实模型通过 function calling 选择工具；两条路径都在同一个循环里根据 observation 调整下一步。意图只作为本轮元数据记录，不在开始时就锁定整轮动作。

### 5.2 修改策略

`revise_script` 接收 `strategy`，所有策略都生成新版本，不原地改旧版本。

| 策略 | 用途 | 行为 |
| --- | --- | --- |
| `next_template` | 时长微调 | 在 38/34/28 三档里选下一档；显式目标选不超过目标且最接近的一档 |
| `patch_scenes` | 换开头、改结尾、改某句 | 按 `scene_patches` 改指定分镜，保留其他分镜 |
| `drop_facts` | 不要提某个卖点 | 删除指定素材/事实；claim 镜头从剩余事实里重新选 |
| `redraft` | 大改或增量失败 | 用当前 brief 和修改说明重新生成 |

“再短一点”默认走下一档更低模板。当前 28 秒已经没有更低模板，返回 `CANNOT_COMPRESS`，Agent 说明最短 28 秒，并给出两个选择：接受 28 秒，或删减内容后再试。不会为了满足“更短”硬删事实镜头。

“再长一点”默认走下一档更高模板，新目标设为该模板总时长。28 秒的下一档是 34 秒；如果 34 秒超过品牌上限，直接失败并说明。

“换开头”走 `patch_scenes`，只改第 1 个分镜的旁白和画面，时长不变。离线策略从一组预置开头里选；真实模型自由写，但仍要过时长和事实检查。

“不要提续航”走 `drop_facts`，删除 P02 相关声明和来源。如果删除后没有可用事实，返回 `NO_FACT`，不生成空脚本。

### 5.3 澄清

以下情况只问一个问题，不产生版本：

- 修改指令同时包含互相冲突的要求，例如“再短一点，但保留所有分镜和事实”。
- “长一点”需要跳到更高模板，但会超过品牌上限。
- 用户说“换个方向”，但没有给主题、受众或风格。
- 要修改的分镜不存在，或一句话同时指向多个分镜。

澄清问题要带可选项，例如：“下一档是 34 秒，超过当前目标 32 秒；接受 34 秒，还是保持 28 秒？”

## 6. 版本模型

`script_id` 就是版本 ID，创建后不可变。新增字段：

| 字段 | 含义 |
| --- | --- |
| `task_id` | 所属任务 |
| `parent_script_id` | 父版本，第一版为 null |
| `version_no` | 任务内递增，1、2、3 |
| `status` | `draft` / `validated` / `approved` / `rejected` / `superseded` |
| `change_intent` | 这一版由什么意图产生 |
| `change_summary` | 一句话说明改了什么 |
| `created_by_turn_id` | 哪一轮产生 |

版本状态：

```text
draft        已生成，尚未通过估算和规则检查
validated    估算达标、规则通过，已存为候选
approved     人工批准
rejected     人工驳回
superseded   被更新的候选替代，历史保留
```

任务表只保存 `current_script_id` 指针。回退就是把指针移回旧版本，不复制、不删除。`approved` 版本不会被后续修改覆盖；用户要改已批准版本时，从它创建新版本，旧批准记录保留。

指针规则：

- 新一轮产生 `validated` 版本时，`current_script_id` 移到新版本；旧的 `validated` 版本标为 `superseded`。
- 批准指定版本时，`current_script_id` 移到该版本，任务状态变为 `approved`。
- 驳回当前版本时，指针移回最近的 `validated` 或 `approved` 版本；没有可回退版本就置空，任务回到 `active` 或 `waiting_input`。
- 回退命令只移动指针，不改变版本自身状态。

### 6.1 版本差异

`diff_versions(from, to)` 返回结构化差异：

```json
{
  "from_script_id": "VID-A",
  "to_script_id": "VID-B",
  "from_version": 2,
  "to_version": 3,
  "duration_delta_sec": 6,
  "target_delta_sec": 0,
  "scenes": [
    {
      "scene": 1,
      "change": "modified",
      "fields": {
        "duration_sec": {"from": 4, "to": 5},
        "narration": {"from": "还在纠结：字幕功能？", "to": "你有没有遇到这样的问题：字幕功能？"}
      }
    }
  ],
  "claims": {
    "added": [],
    "removed": [],
    "changed": []
  },
  "rule_check": {
    "from_passed": true,
    "to_passed": true,
    "new_violations": []
  }
}
```

CLI 默认打印摘要和变更分镜；`--json` 输出完整结构。

## 7. 工具契约

保留现有七个工具，新增任务和版本工具。

| 工具 | 作用 | 关键校验 |
| --- | --- | --- |
| `get_task` | 读任务 brief、当前版本、约束、最近轮次 | 任务必须存在 |
| `list_versions` | 列版本摘要 | 只能看当前任务 |
| `diff_versions` | 比较两个版本 | 两个版本都属于当前任务 |
| `search_materials` | 检索素材事实 | 空查询拒绝；无结果改写一次 |
| `draft_script` | 生成第一版或重新起草 | 必须有素材事实；只引用素材 |
| `revise_script` | 增量修改或换模板 | base 属于当前任务；策略参数完整；新版本声明仍有来源 |
| `estimate_duration` | 逐分镜求和 | 脚本必须存在 |
| `check_rules` | 禁词、声明、来源、旁白断言、时长 | 违规写 violations |
| `save_script` | 存候选版本 | 必须有同一版本的达标估算和通过检查 |
| `finish` | 结束本轮 | `completed` 必须有候选保存记录；`answered` 只回答不改状态；`needs_clarification` 只问一个问题 |

`finish` 在多轮里新增两个状态：`answered`（回答问题，没有版本变化）和 `needs_clarification`（等用户补充信息）。原有失败状态继续保留。

不提供 `approve_version` 工具。批准只走 CLI 命令。

### 7.1 `revise_script`

```json
{
  "script_id": "VID-A",
  "strategy": "patch_scenes",
  "target_duration_sec": 30,
  "direction": "target",
  "instructions": "开头更直接，不要提问式开场",
  "scene_patches": [
    {"scene": 1, "narration": "字幕没开，观众三秒就走。", "duration_sec": 4}
  ],
  "remove_source_ids": [],
  "focus_keywords": ["字幕"]
}
```

`script_id` 是 base 版本。`direction` 取 `target` / `shorter` / `longer`；`shorter` 和 `longer` 走下一档更低或更高模板，`target` 选不超过目标且最接近的一档。

返回：

```json
{
  "script_id": "VID-B",
  "parent_script_id": "VID-A",
  "version_no": 3,
  "template": "compressed",
  "total_duration_sec": 28,
  "changed_scenes": [1],
  "change_summary": "替换第 1 个分镜的开头，总时长不变"
}
```

校验顺序：

1. base 版本属于当前任务。
2. 策略要求的字段齐全；`patch_scenes` 至少一个 patch，`drop_facts` 至少一个 source。
3. patch 的分镜存在，时长大于 0，旁白不为空。
4. 修改后的 claim 必须属于所引素材事实；不能留下无来源的产品声明。
5. 计算总时长；超过品牌上限直接拒绝。
6. 写入新版本，状态 `draft`；后续必须调用 `estimate_duration` 和 `check_rules`。

### 7.2 幂等

- `revise_script` 的幂等键：`task_id + script_id + strategy + normalized_instructions + target + direction + scene_patches + remove_source_ids + focus_keywords`。
- 同一轮重试命中相同键时返回已有版本，不新建。
- `save_script` 以 `script_id` 为键；重复保存返回 `existing`。
- `approve` 以 `task_id + script_id` 为键；同一版本只能有一条审核决定，重复批准返回已有记录。

## 8. SQLite 设计

默认库 `.runs/agent.db`，开启 WAL、外键和 5 秒 busy timeout。

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  request_json TEXT NOT NULL,
  status TEXT NOT NULL,
  current_script_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE turns (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  user_text TEXT NOT NULL,
  assistant_text TEXT,
  intent TEXT,
  status TEXT NOT NULL,
  engine TEXT NOT NULL,
  model TEXT,
  created_at TEXT NOT NULL,
  completed_at TEXT,
  UNIQUE (task_id, seq)
);

CREATE TABLE tool_calls (
  id TEXT PRIMARY KEY,
  turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  tool TEXT NOT NULL,
  arguments_json TEXT NOT NULL,
  result_json TEXT,
  ok INTEGER NOT NULL,
  error_code TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (turn_id, seq)
);

CREATE TABLE script_versions (
  script_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  parent_script_id TEXT REFERENCES script_versions(script_id),
  version_no INTEGER NOT NULL,
  template TEXT NOT NULL,
  content_json TEXT NOT NULL,
  total_duration_sec INTEGER NOT NULL,
  status TEXT NOT NULL,
  change_intent TEXT,
  change_summary TEXT,
  created_by_turn_id TEXT REFERENCES turns(id),
  created_at TEXT NOT NULL,
  UNIQUE (task_id, version_no)
);

CREATE TABLE estimates (
  estimate_id TEXT PRIMARY KEY,
  script_id TEXT NOT NULL REFERENCES script_versions(script_id) ON DELETE CASCADE,
  total_duration_sec INTEGER NOT NULL,
  target_duration_sec INTEGER NOT NULL,
  within_target INTEGER NOT NULL,
  scene_durations_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE rule_checks (
  check_id TEXT PRIMARY KEY,
  script_id TEXT NOT NULL REFERENCES script_versions(script_id) ON DELETE CASCADE,
  passed INTEGER NOT NULL,
  violations_json TEXT NOT NULL,
  warnings_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE approvals (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  script_id TEXT NOT NULL REFERENCES script_versions(script_id) ON DELETE CASCADE,
  decision TEXT NOT NULL,
  reason TEXT,
  turn_id TEXT REFERENCES turns(id),
  created_at TEXT NOT NULL,
  UNIQUE (task_id, script_id)
);

CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_turns_task_seq ON turns(task_id, seq);
CREATE INDEX idx_versions_task_no ON script_versions(task_id, version_no);
CREATE INDEX idx_estimates_script ON estimates(script_id);
CREATE INDEX idx_checks_script ON rule_checks(script_id);
CREATE INDEX idx_approvals_task ON approvals(task_id);
```

JSON 列存文本，Python 层序列化；不依赖 SQLite 的 JSON1 扩展。

写入顺序：

1. 开始一轮：插入 `turns`，状态 `running`。
2. 每次工具调用：执行成功后写 `tool_calls`；版本、估算、检查在同一个事务里写。
3. 本轮结束：更新 `turns.status`、`assistant_text`、`completed_at`，更新 `tasks.current_script_id` 和 `updated_at`。
4. CLI 启动时把超过 10 分钟仍是 `running` 的轮次标记为 `interrupted`，避免悬挂。

## 9. 状态与失败

### 9.1 任务状态

```text
new            刚创建，还没有候选版本
active         有候选版本，可以继续改
waiting_input  等用户补充信息或确认
approved       用户已批准某一版
archived       不再使用，历史保留
```

### 9.2 轮次状态

```text
running             正在执行
completed           正常结束，有回复
needs_clarification 需要用户回答一个问题
failed              工具连续失败或执行超限
interrupted         进程中断
```

### 9.3 失败处理

| 失败 | 行为 |
| --- | --- |
| 检索连续失败 | 本轮 `failed`，返回 `SEARCH_FAILED`，不生成脚本 |
| 生成连续失败 | 本轮 `failed`，保留已有版本 |
| 估算连续失败 | 版本保留为 `draft`，不存候选，不标完成 |
| 目标低于最短时长 | `CANNOT_COMPRESS`，不新建可用版本，说明缺口 |
| 规则检查失败 | 版本保留为 `draft`，列出 violations，不存候选 |
| 保存失败 | 重试一次；仍失败本轮 `failed`，不声称候选已存 |
| 模型输出超步数 | 本轮 `failed`，保留工具已产生的记录 |
| 意图不明确 | `needs_clarification`，只问一个问题，不改版本 |

失败不能覆盖当前候选。`tasks.current_script_id` 只在候选保存成功或用户回退时变化。

## 10. 运行时

`next_action` 是唯一的分叉点。

### 10.1 离线策略

- 正则和关键词解析意图。
- 固定短语表覆盖“再短一点”“长一点”“换开头”“不要提 X”“回到上一版”“就用这版”。
- 修改策略确定：时长走 `next_template`，内容走 `patch_scenes`，删事实走 `drop_facts`，大改走 `redraft`。
- 预置开头和结尾变体，保证示例可复现。
- 不调用网络，输出稳定，适合测试和演示。

### 10.2 真实模型

- 模型上下文包含任务 brief、当前分镜、最近 5 个版本的摘要和最近 3 轮对话。每条历史消息截断至 600 字符，工具 schema 通过 function calling 传入。
- 模型只能通过工具读写状态；不能直接写 SQLite，不能自己批准。
- 每轮最多 12 步、20 次工具调用；超过返回 `incomplete`。
- 模型每一步都看到上一步工具结果，可以调整下一步；不要求一次给完整计划。
- 模型可以提出澄清问题，但一次只问一个。
- 工具结果里的 `ok=false` 必须被观察；模型不能把失败步骤说成成功。
- live 模式的输出文本不保证可复现，测试只断言工具序列、版本状态和关键字段。

### 10.3 循环边界

复用现有 `miniagent.run_loop`，新增持久化钩子：

- `model_decision`：记录模型这一步的文本和工具调用。
- `tool_result`：记录工具结果，写 `tool_calls`。
- `turn_start` / `turn_end`：写轮次状态和回复。
- 连续两次只输出文本不调工具，停止并返回 `model_no_tool_call`。
- 达到步数或工具调用上限，返回 `incomplete`，保留已产生的记录。
- `finish` 成功即结束循环；失败不能伪造终态。

### 10.4 上下文

每轮传给 `next_action` 的上下文限制在：

- 任务 brief 和当前约束。
- 当前版本摘要：版本号、模板、总时长、规则状态、分镜列表。
- 最近 3 轮的用户消息和 Agent 回复。
- 最近一次估算和规则检查的完整结果。
- 不传完整历史分镜；需要时通过 `list_versions` / `diff_versions` 读取。

## 11. 验收

### 11.1 单元测试

- 意图解析：10 条常见表达映射到正确意图。
- 版本链：parent、version_no、current 指针、回退。
- 差异：分镜增删改、时长变化、claim 变化、规则状态变化。
- SQLite：外键、唯一约束、事务回滚、迁移。
- 幂等：同一修改重复执行不产生第二版。
- 审核：只有 `validated` 版本能批准；跨任务批准被拒绝。

### 11.2 场景测试

| 场景 | 操作 | 预期 |
| --- | --- | --- |
| 首次生成 | 创建 30 秒任务 | v1 是 38 秒 `draft`，压缩得到 v2 28 秒 `validated`，候选已存 |
| 再短一点 | 当前 28 秒说“再短一点” | 不新建版本，直接说明最短 28 秒 |
| 换开头 | 说“换一个更直接的开头” | v3 只改第 1 个分镜，重新估算和检查 |
| 不要提续航 | 多素材任务说“不要提续航” | 新版本删除 P02 相关声明，规则通过 |
| 目标 35 秒 | 说“改成 35 秒” | v4 选 compact 34 秒 |
| 回退 | “回到上一版” | current 指针回 v3，历史不删 |
| 批准 | `/approve 3` | v3 `approved`，任务 `approved` |
| 重复修改 | 同一 base 上重复相同修改 | 第二次返回已有版本，版本数不增加 |
| 跨任务引用 | 用别的任务的 script_id 修改 | 工具返回 `TASK_MISMATCH` |

### 11.3 演示脚本

```bash
python3 run.py task create --input examples/02-over-duration/input.json
python3 run.py task chat <task_id>
```

```text
> 换一个更直接的开头
> 不要提续航
> /history
> /diff 1 3
> /approve 3
> /export 3
```

演示重点看三件事：每次修改都产生新版本；失败和成功都留在轮次记录里；批准只发生在人工命令。

## 12. 实施计划

### 阶段 1：存储与版本

- 新增 `src/short_video/storage.py`，实现 SQLite schema、迁移、任务/轮次/版本/估算/检查/审核仓库。
- 保留现有 `ScriptStore` JSON，单轮 `run` 不改。
- 测试：迁移、外键、版本链、唯一约束、事务回滚。

### 阶段 2：单 ReAct 循环与离线策略

- 复用 `miniagent.run_loop`，新增轮次持久化钩子和上下文构造。
- 扩展 `policy.py` 的 `next_action`，实现离线意图解析和 `revise_script` 四种策略。
- 新增 `task` 子命令和 `chat` REPL。
- 测试：意图、增量修改、版本差异、回退、澄清。

### 阶段 3：审核与导出

- `approve` / `reject` / `rollback` 命令和审核记录。
- Markdown 分镜表导出；JSON 导出保留完整字段。
- 任务列表和状态过滤。
- 测试：审核状态机、导出内容、跨任务保护。

### 阶段 4：真实模型

- 复用现有 OpenAI function calling 适配器，实现 live `next_action`。
- 提示词、上下文裁剪、步数上限、失败观察。
- 可选 live smoke 测试，需要自备 key。
- 记录模型名、耗时、工具调用序列。

### 阶段 5：演示与文档

- 新增 `examples/08-multi-turn/` 的 CLI transcript 和版本历史。
- 更新 README、解决方案文档和设计文档。
- 跑全量测试和示例重建。

## 13. 非目标

- 不做 Web UI；形态已定为 CLI。
- 不做多用户、登录、权限和并发写。
- 不接真实 TTS、视频渲染、发布平台。
- 不做向量检索；当前仍是素材库关键词匹配。
- 不做自动内容质量评分；规则只覆盖事实、时长和禁词。
- 不把审核交给 Agent；批准必须是人执行命令。

## 14. 默认决定

以下按默认值实现，评审时可以直接改：

| 项 | 默认 |
| --- | --- |
| 架构 | 单 Agent、单 ReAct 循环 |
| 数据库 | `.runs/agent.db` |
| 任务 ID | `SV-T-<8位十六进制>` |
| 版本号 | 任务内从 1 递增 |
| “再短一点” | 下一档更低模板；没有更低模板就失败 |
| “再长一点” | 下一档更高模板，新目标取该模板总时长 |
| 换开头 | 只改第 1 个分镜，时长不变 |
| 不要提 X | 删对应素材事实；没有事实可用就失败 |
| 回退 | 移动 current 指针，不新建版本 |
| 批准 | CLI 命令，Agent 无工具权限 |
| 单轮 `run` | 保持 JSON 输出，不改现有示例 |

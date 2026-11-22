# 技能（Skill）：六家 harness 怎么让 agent "学会新本事"

这篇讲一件具体的事：**当 agent 需要一套"什么时候该按什么流程做事"的知识时，各家把它放在哪、怎么交给模型、给谁用。**

不讲代码细节，只讲每个选择要解决的问题与代价。需要核对行号的看文末的证据索引。

对照六家：Codex（Rust）、pi-agent（TS）、DeepSeek Harness／DSH（TS）、hermes-agent（Python）、
learn-claude-code（教学版）、deepseek-reasonix（Go）。Claude Code 是闭源产品，只能从行为观察，
不计入结论。

---

## 0. 从一个场景说起

你的项目里有三十个技能：发版流程、数据库迁移规范、前端组件约定、代码评审清单……
当前这一轮任务只是"把 config 里那个拼写改了"。

问题不是"技能够不够"，而是三个互相独立的问题：

1. **清单怎么进上下文**：三十条描述常驻要花钱，一条都不给模型又不知道有这些东西；
2. **正文怎么取**：技能正文动辄几千字，全塞进去会挤掉真正的工作内容；
3. **谁能用、什么时候用**：是模型自己判断、用户点名，还是根本不进模型的视野。

六家的差异，基本上就是在这三个问题上的不同答案。

## 1. 清单放在哪：四个位置，各有代价

| 位置 | 谁这么做 | 好处 | 代价 |
| --- | --- | --- | --- |
| 系统提示常驻 | pi、hermes、learn-claude-code | 简单、一定被看到 | **改了前缀缓存全失效**；条数多了压预算 |
| 系统提示 + 预算裁剪 | Codex | 有预算上限，抗规模 | 需要实现分配器：压描述、丢整行、补"省略了 N 个" |
| 随轮次投影下发 | reasonix | 不碰前缀（注释原话 "rides the turn, never the prefix"），按内容去重 | 每轮都要判断发不发，实现更重 |
| 持久会话消息 | DSH | 能增量替换（digest 变才重发） | 要维护"已发过什么"的状态 |

**这一条对我们最扎心**：Gradus 现在把技能清单写进 `BASE_PROMPT` 的同一个系统提示里，
而系统提示是**前缀缓存的最前端**——`/reload` 换一个技能，整个前缀作废。上下文管理那边刚为
"摘要吃缓存"付出过代价，技能清单是同一个陷阱。

Codex 的做法值得抄一半：目录行只放 `name + description + locator`，总预算取**上下文窗口的 2%**
（封顶 10,000 token），不够就先压描述、再整行丢弃，并明确告诉模型"另外有 N 个没列出来"。
它把"技能清单"当成**有配额的资源**，而不是"有几条就全放"。

## 2. 正文怎么取：路径给模型自己去读，还是变成工具

| 做法 | 谁 | 表现 |
| --- | --- | --- |
| 清单带绝对路径，模型自己 `read` | pi、Codex（host skill）、Gradus | 不新增工具，schema 便宜；但"有没有真读""读了哪些"全靠提示词约定 |
| 专用工具取正文 | DSH `skill({name})`、hermes `skill_view`、reasonix `run_skill`、learn-claude-code `load_skill` | 统一包装、可审计、可加策略；代价是多一个常驻工具 schema |

Codex 的调研里有一句评价很到位：把"是否真读完、读了哪些资源、能否审计"全押在提示词约定上，
不如统一成一等公民工具。DSH 的 `<skill_content>` 包装和 reasonix 的 `<skill-pin>` 都是这个思路。

顺带一提，**渐进加载到第二层就够，不必到第三层**：六家里只有 hermes 与 reasonix 会主动把
`references/` 列出来、按需二次读；其余都是"正文里让你去读哪个文件"，把资源当普通文件处理。

## 3. 谁能用：三档，别混

| 档 | 机制 | 谁实现 |
| --- | --- | --- |
| 模型自主 | description 匹配即调用；或用专用工具 | 全部 |
| 用户显式 | `/skill:name`、`$name`、`@mention` | pi、hermes、reasonix、DSH、Codex |
| 自动路由 | 按 `triggers` / 命中文件路径推荐 | 只有 reasonix |

这里真正有意思的是**开关**，而不是"能不能用"：

- pi：`disable-model-invocation`（设了就只能用户调）；
- DSH：`modelInvocable` / `userInvocable` 两个正交开关，四种组合都支持；
- Codex：`policy.allow_implicit_invocation: false`——技能从目录里**隐藏**，但仍能按名字显式调用
  （注释原话 "Hidden skills remain explicitly readable"）；
- reasonix：`invocation: manual` / `disable-model-invocation` 不进模型清单。

这套开关解决的是很实际的问题：**有些技能不该被模型自己翻出来用**（涉及发布、删除、外部副作用），
但用户明确点名时应该能跑。四家里三种写法，DSH 的两个正交布尔最干净。

## 4. 权限收窄：六家里只有一家真的做了

这是这次调研最反直觉的结论。`allowed-tools` 几乎出现在所有技能的 spec 文档里，
但**真正落到执行层的只有 deepseek-reasonix**：

| 项目 | `allowed-tools` 的真实状态 |
| --- | --- |
| Codex | 全仓 Rust 端**没有解析或执行点**，只有 skill-creator 的校验脚本把它列进白名单 |
| pi | 文档里写了，全仓代码无命中 |
| DSH | `packages/skill` 下无命中 |
| hermes | 只被安全扫描器标成 "informational (low)"，插件侧只校验它是字符串 |
| learn-claude-code | 没有这个概念 |
| **reasonix** | 解析成 `Bash(git add:*)` 这类规范写法，**直接成为 subagent 的工具表**；`read-only: true` 剥掉写工具并换只读 bash 包装，测试固定 |

也就是说：**"技能声明自己能用的工具"这件事，在开源 harness 里基本还是文档承诺**。多数情况下
用户的真实感受是"我声明了，但它还是会调别的工具"。

对 Gradus 的含义：这一项可以**明确不做**，但要写清"不做"，别在文档里假装支持。

## 5. 失败与观测：fail-open 值得学

| 做法 | 谁 | 具体 |
| --- | --- | --- |
| 单个技能坏掉只跳过 | 全部 | 解析失败 → warning + 跳过，不中断启动 |
| 可选元数据 fail-open | Codex | `agents/openai.yaml` 坏了只 warn，注释原话 "Fail open: optional metadata should not block loading SKILL.md" |
| 诊断去重 | reasonix | 同一诊断只报一次，避免刷屏 |
| 使用计数 | hermes | `~/.hermes/skills/.usage.json` 记 view/use/patch 次数与最后使用时间 |
| 调用事件 | Codex | `codex.skill.injected` 带 invoke_type；还把"读了 SKILL.md""跑了技能脚本"识别成隐式调用 |
| 来源与漂移 | reasonix | `skills.lock` 记来源与摘要，状态机 Synced / LocalEdited / Diverged / SourceChanged |

**Codex 的"隐式调用识别"是很有意思的一招**：命令首 token 是 `python/node/bash` 且脚本落在
某个技能的 `scripts/` 下，或者读命令命中了那个 SKILL.md，就记为该技能的一次调用。这样观测口径
不再依赖"模型有没有礼貌地说我在用技能"。

## 6. 六家对比

| 维度 | Codex | pi | DSH | hermes | learn-cc | reasonix |
| --- | --- | --- | --- | --- | --- | --- |
| 清单位置 | developer 块，2% 预算 | system prompt | 持久 user 消息（增量） | system prompt + 快照缓存 | system prompt | 随轮次投影（去重） |
| 目录层级 | 4 scope + `.agents/skills` 逐级 | 用户 + 项目 + `.agents` | 6 档 rank | 4 层 tier | 单层 | 4 scope |
| 正文获取 | host 靠 read；executor 用工具 | read | `skill` 工具 | `skill_view` | `load_skill` | `run_skill`/`read_skill` |
| 用户显式 | `$name` / mention | `/skill:name` | `/<name>` | `/skill-name` | 无 | `/<name>` |
| 开关 | `allow_implicit_invocation` | `disable-model-invocation` | model/user 双开关 | 无 | 无 | `invocation: manual` |
| allowed-tools | **未实现** | **未实现** | **未实现** | 仅信息性 | 无 | **真执行** |
| 观测 | 调用事件 + 预算直方图 | 冲突诊断 | 目录 change 事件 | usage.json + linter | 无 | lock + 漂移状态机 |

## 7. Gradus 现在是什么样

| 项 | 现状 | 证据 |
| --- | --- | --- |
| 目录 | 只有项目级 `＜workspace＞/.gradus/extensions/skills/＜name＞/SKILL.md` | `extensions.py:30,111` |
| 元数据 | `name`（可选，缺省目录名）+ `description`（必填，缺失→警告并跳过） | `extensions.py:220-236` |
| 清单 | 系统提示里 `Skills (read the file when the description matches):` + `- name: desc (path)` | `extensions.py:147-155`、`session.py:504-514` |
| 正文 | 模型 `read_file` 那个路径；用户 `/skill:name` 展开（支持 `$ARGUMENTS`） | `extensions.py:93-106`、`session.py:1640` |
| 信任 | 项目级扩展目录必须先信任，否则跳过并告警 | `session.py:1175-1187` |
| 重载 | `/reload` 重读扩展并重建 Agent | `cli.py:125-135` |

也就是说：**Gradus 走的是 pi 那条路**（描述常驻 + 模型自己 read + 斜杠命令），
清单也放在系统提示里——和 pi 一样，继承了"清单变化污染前缀缓存"的问题。

### 已定方案（B：两段式披露 + 双根目录）

采纳的是「索引常驻 + 正文工具化」这一档（见 `design.md` §5.8.1）：

- 索引只放 `name: description`（描述截断到 200），**不带路径**；
- 正文由常驻工具 `use_skill(name, offset=0)` 提供，单次 8,000 字符、可续读、调用即记一次使用；
- 用户入口 `/skill:name` 保留，与工具共用使用计数（`Session.skills_used`，只在内存）；
- 双根目录：用户级 `~/.gradus/extensions`（总是加载、不需信任、只放 skills/commands）+
  项目级（信任后加载、可放 tools）；**同名时用户级优先**；
- 资源目录（`references/`、`scripts/`、`templates/`）只定约定，不自动列清单。

### 仍然没做的（都已确认不做或待定）

| 项 | 状态 | 理由 |
| --- | --- | --- |
| `allowed-tools` | **不做** | 六家里只有 reasonix 真做；其余都只写在文档里 |
| 只允许用户调用（`disable-model-invocation`） | **已做** | 不进索引、`use_skill` 拒绝、`/skill:` 照常 |
| 清单预算与"省略 N 个" | 待定（P3） | 技能数量真的上来才需要 |
| 用户级工具代码 | **不做** | 没有信任门控，只放文本类扩展 |
| 自动路由 / triggers | 不做 | 只有 reasonix 做，收益不明 |
| 使用记录落盘 | 暂不做 | 会话文件加第六种记录要改格式版本 |

### 建议顺序

| 优先级 | 做什么 | 为什么 |
| --- | --- | --- |
| P0 | 从 `tool_root` 加载扩展 | 真 bug，副本会话里技能读的是另一份文件 |
| P1 | 加 `use_skill(name)` 工具，正文由它返回并记录使用 | 去掉路径依赖、可审计；用户 `/skill:` 保留 |
| P2 | `frontmatter` 加 `disable-model-invocation`（默认 false） | 便宜、和 pi/DSH 语义一致；先解决"不该被模型自己翻出来"的技能 |
| P3 | 清单加预算与"省略 N 个"提示（参考 Codex 的 2% 预算） | 技能变多以后才需要，但结构现在就该留 |
| P4 | 用户级目录 + 附带资源清单（`references/` 等） | 生态层面的东西，等真的有人写技能再做 |

**明确不做的两件**：

- `allowed-tools`：六家里只有 reasonix 真做，而且是在 subagent 场景；我们短期不做，文档里要写清楚；
- `dependencies.tools` / `products`（Codex）：我们只有一个客户端、一个工具面，属于过度设计。

## 8. 一句话

**技能系统真正的分水岭不在"能不能加载"，而在两件小事上**：清单是"有预算的资源"还是"有几条放几条"，
以及清单放在**前缀里**还是**随轮次/会话消息**走。前者决定规模，后者决定每一次改动要不要重算缓存。

---

## 证据索引

- Codex：`codex-rs/skills/src/{parser,metadata,selection,mentions,invocation,lib}.rs`、
  `codex-rs/ext/skills/src/{catalog_prompt,render,fragments,tools/mod,loader/host,loader/metadata}.rs`
- pi-agent：`pi/packages/coding-agent/src/core/{skills.ts,system-prompt.ts}`、
  `src/core/agent-session.ts`、`src/core/diagnostics.ts`
- DSH：`packages/skill/skill/src/index.ts`、`packages/skill/skill-filesystem/src/index.ts`、
  `packages/skill/tool-skill/src/index.ts`、`docs/subsystems/skills.md`
- hermes-agent：`agent/{skill_utils.py,prompt_builder.py,skill_commands.py,skill_preprocessing.py}`、
  `tools/{skills_tool.py,skill_manager_tool.py,skill_usage.py,skills_guard.py}`
- learn-claude-code：`s07_skill_loading/code.py`、`s15_integrated_harness/code.py`、
  `tests/test_skill_loading.py`
- deepseek-reasonix：`internal/ext/skill/{skill,index,tools,supplements,allowed_tools}.go`、
  `internal/session/control/skill.go`、`internal/runtime/capability/capability.go`、
  `internal/ext/skilllock/lock.go`
- Gradus：`gradus/coding/extensions.py`、`gradus/coding/session.py`、`gradus/coding/cli.py`

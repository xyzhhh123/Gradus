# 变更记录

按日期倒序。只记「改了什么、为什么、怎么验证、去哪看细节」——过程性的试错留在
`problem.md`，设计理由留在各自的 `design.md`。

---

## 2026-11-21

### 一、AI 层：整条流式链显式关闭

**改了什么**：`post_sse` → `_post_with_retry` → `_parse_sse_async`，以及四个 provider 的
`stream()`，全部改成「谁打开谁关」：`finally: await events.aclose()`。`agent/loop.py` 的
provider 流也包了 `try/finally`。

**为什么**：`async for` 在 `break`（收到 `[DONE]`）或 `GeneratorExit` 时**不会**关闭迭代器。
被抛弃的生成器要等事件循环关闭才被 GC 收尾，收尾时外层正在 `athrow`，于是报
`RuntimeError: aclose(): asynchronous generator is already running`。

**怎么验证**：`test_break_after_done_closes_post_sse`（用「既迭代又记录 aclose」的跟踪对象，
去掉修复即失败）、`ProviderStreamCloseTest`；三个真机脚本的收尾噪声归零。

**细节**：`problem.md`「流式响应没关干净」、`ai/docs/design.md` 的 `transport.py` 小节。

### 二、观测：usage 账本的出口

**改了什么**：

- CLI：每轮结束打一行 `[in 4,211 · out 12 · cache 4,096 (97%)]`，新增 `/usage` 看会话累计；
- Web：新增 `usage/state` 帧（`{turns,inputTokens,outputTokens,cacheReadTokens,cacheWriteTokens}`），
  连接时发一次、每轮结束再发一次；状态栏显示 `轮数 · in · out · cache %`。

**为什么**：账本（`session` 文件里的 `usage` 记录）之前只有接口没有出口，成本看不见。

**怎么验证**：真机一轮确认两行输出；`UsageFormatTest`（4 条）、`UsageTotalsPayloadTest`（2 条）、
web `reduce.test.ts` 的 `usage/state` fold；`tsc` + `npm run build` 通过。

**细节**：`coding/docs/design.md`、`web/docs/design.md` 协议表、`coding_plan.md` M18。

### 三、配置：`GRADUS_MAX_TURNS`

**改了什么**：`Settings.max_turns`（默认 20），环境变量 `GRADUS_MAX_TURNS`、配置文件键
`max_turns`；`Session._connect_agent` 把它传给 `LoopConfig`。

**为什么**：仓库级修复任务动辄几十轮，20 轮会直接掐断（实测两题撞上限）。

**怎么验证**：`MaxTurnsTest`（默认 / 覆盖 / 非正整数报错）。

### 四、沙箱：六家对比与三条「不做」的决定

**改了什么**：

- 新增 `sandbox_reference.md`：codex / pi / DSH / hermes / reasonix / claude-code 六家的
  命令沙箱做法、平台矩阵、四个流派、失败模式；
- `sandbox.md` 新增 §4.1–4.4：谁在箱内、MCP 为什么在箱外、已有的几道闸、为什么不把 MCP 入箱；
- 明确不做：**docker 后端真机验证**、**MCP 进程入箱**、**Windows 断网（WFP）**。

**为什么**：这三条以前是「未定」，读文档的人会以为是漏了；现在写清理由与威胁模型边界。

**细节**：`sandbox.md` §4、§10、`coding_plan.md` M14 未定列、`README` 目录树。

### 五、真机验证：一条命令跑完

**改了什么**：`scripts/verify_all.py` 按顺序跑 `context` / `merge` / `branch` 三个脚本并汇总
退出码；`verify_merge_live.py` / `verify_branch_live.py` 补上成/败判定与退出码。

**为什么**：三个脚本各自能跑但没有统一入口，容易忘记跑。

**细节**：`coding_plan.md`「完成定义」第 5 条。

### 六、评测基建：`bench/`（本次最大的一块）

新增目录，**切片式**跑 SWE-bench 子集：

| 文件 | 作用 |
| --- | --- |
| `common.py` | 任务/结果模型、追加式结果存储、Wilson 区间、固定报告格式 |
| `run.py` | 切片运行器：`--minutes` / `--max-tasks` / `--timeout` / `--mode` / `--max-turns` |
| `agent.py` | 宿主直跑执行器（调试用，**无边界**） |
| `container.py` | 容器执行器：每题一次性容器、实例镜像、挡答案域名、自检参数 |
| `container_task.py` | 容器内驱动：跑 Gradus → 取 patch → 自检 → 失败再修 |
| `grade.py` | 官方 harness 判定 + **gold 基线对照** + 实时日志 |
| `prepare.py` | 克隆题目仓库（强制 LF，避免容器里出现「整棵树都改了」） |
| `make_tasks.py` | 从 `SWE-bench/SWE-bench_Verified` 生成题单（跨仓库、避开超大任务） |
| `Dockerfile` / `Dockerfile.agent-instance` | 空盒子 / 实例镜像盒子 |

关键决定与教训：

1. **容器即边界**：agent 在容器里用 `sandbox: local`（受限令牌只在 Windows 可用）；
2. **用实例镜像当 agent 环境**（P2）：仓库依赖已在 `conda testbed` 里，Gradus 另装一套
   py3.13（`uv`），模型改完能 `conda run -n testbed pytest ...` 自测；
3. **挡答案来源**（P1）：容器加 `--add-host github.com:0.0.0.0` 等 8 个域名。**不能
   `--network none`**——模型 API 本身也要走网络（试过，3 轮就 `agent:provider` 挂）；
4. **轮数预算提醒**（P3）：75% / 90% 各注入一条提醒，只追加消息、不破坏前缀缓存；
5. **收尾自检**（P4）：复制仓库 → 打官方 `test_patch` → 跑 `FAIL_TO_PASS` → 失败把输出喂回模型
   再修，最多 2 次；结果里记 `checks` / `self_check`；
6. **gold 基线对照**：判定前先跑标准答案，`gold` 也挂的测试记为环境问题（ENV），
   不算模型回归——实测 `psf__requests-1724` 的 `test_mixed_case_scheme_acceptable`
   因容器 SSL 证书校验失败而挂，gold 同样挂。

实测数据（`deepseek-v4.1-flash`，容器模式）：

| 配置 | 题 | 轮数 | 输入 | 自检 | 官方判定 |
| --- | --- | --- | --- | --- | --- |
| 空盒子 + 联网 | flask-5014 | 27 | 253k | 模型自己装 pytest | PASS |
| 实例镜像 + 挡答案 | flask-5014 | 30 | 354k | 模型用 conda testbed | PASS |
| **实例镜像 + P3 + P4** | flask-5014 | **18** | **234k** | `checks=1 pass` | **PASS** |
| 宿主直跑（无边界） | requests-1142 | 24 | 285k | — | PASS |
| 空盒子 + 联网 | requests-1724 | 33 | 448k | — | ENV（gold 也挂） |

### 七、评测基建的补强（B 组）

**改了什么**：

1. **失败分类 + 未判定**：`common.summarize` 把「跑完但没判定」单列成 `unjudged`，不再混进
   `failures`；失败按 `超时` / `agent:<kind>` / `container失败` / `workspace缺失` / `测试未过` 归类；
2. **`--workers` 并发**：切片运行器支持同时跑 N 题（信号量 + `asyncio.to_thread`）；
   `--max-tasks` 先截断批次，单题异常不影响其他题；
3. **`bench/cleanup.py`**：按题单清实例镜像、清 `sweb.eval.*` 容器，支持 `--dry-run`；
   `grade.py` 判定后自动清容器（harness 自己的清理步骤缺 docker CLI，之前每次都留 2–4 个）；
4. **README**：补两种执行模式、关键参数、目录树、真实缺口。

**怎么验证**：`RunManyTest`（并发峰值 ≤ workers、`max-tasks` 生效）、`bench/tests` 15 条通过；
`cleanup.py --dry-run` 列出 7 个待清镜像；全量 970 passed。

### 八、统一挂载点：`/workspace` → `/testbed`

**改了什么**：agent 容器的仓库挂载点从 `/workspace` 改成 `/testbed`，并把容器的工作目录也设成
`/testbed`；自检副本从 `/.check-N` 挪到 `/tmp/check-N`；指令提示改成「你的工作区就是 `/testbed`，
包是 editable 指向这里，不需要复制文件」。

**为什么**：实例镜像里仓库就在 `/testbed`，conda 环境里的 `flask` 是 `pip install -e /testbed`
装的——测试永远 import 镜像里那份。挂 `/workspace` 时，模型改完还得 `cp` 到 `/testbed` 才能测到，
这是纯损耗，还容易让它以为"测试没反应"。

**验证**：同一道 `pallets__flask-5014`，一次只改一个变量，四次对照：

| 配置 | 轮数 | 输入 token | 缓存命中率 | 自检 | 官方判定 |
| --- | --- | --- | --- | --- | --- |
| 空盒子（python:3.13 + gradus），联网 | 27 | 253k | 94.1% | 模型自己 `pip install pytest` | PASS |
| 实例镜像 + 挡答案域名（P1+P2） | 30 | 354k | 94.7% | 模型用 `conda testbed`，需 `cp` 到 `/testbed` | PASS |
| + 预算提醒 + 收尾自检（P3+P4） | 18 | 234k | 92.4% | harness 自检 `checks=1 pass` | PASS |
| **+ 统一挂载点** | **15** | **87k** | **89.7%** | `checks=1 pass` | **PASS** |

缓存命中率 = `cache_read / input`，由 `TurnEnd` 事件的 usage 累计而来（`bench/common.py`
的 `cache_ratio`，报告里也有一行 `cache N%`）。**轮数越少，命中率越低**：第一轮必然是 0
（整段前缀都是新的），分母也被压小；长任务稳定在 95–97%（详见 `bench/runs/*/tasks.jsonl`）。

`git diff` 在 `/testbed` 里取，patch 路径仍是 `a/src/...` 相对路径，**判定侧不受挂载点影响**。

### 九、容器内轨迹保留

**改了什么**：`container_task.py` 逐事件写 `<task>.trajectory.jsonl`（`turn_start` / `tool_call`
含参数 / `tool_result` 含输出与耗时 / `turn_end` 用量 / `error` / `end`），**边跑边 flush**；
会话原文仍在 `sessions/*.jsonl`，补丁在 `<task>.diff`。新增 `bench/trace.py` 快速看轨迹
（轮数、工具分布、失败调用、最近 N 次调用，`--full` 看完整输出）。

**为什么**：没有轨迹就没法优化——只能看到"通过 / 没通过"，看不到模型在容器里到底试了什么。
实测：seaborn 那题 60 轮 1.6M token 里，大半是 `git clone` 上游仓库翻提交历史（联网时），
这种结论只有轨迹能给出。

**怎么验证**：`bench/trace.py --run-id unified-flask --task pallets__flask-5014` 能打印出
14 次工具调用的分布与一次 `edit` 失败原因（"Read ... before editing it"）。

### 十、已知缺口（下次别忘）

- **实例镜像要先建**：`swebench/sweb.eval.*` 不在 Docker Hub，只能由官方 harness 现场构建；
  第一次跑某题先 `grade.py --with-gold`（顺带把基线也跑了）；
- 每题镜像 1–5 GB，`cleanup.py` 目前**手动**触发，跑大批量前要自己算磁盘；
- seaborn / pylint 两题在 60 轮上限下失败，**尚未**用新盒子 + P3/P4 + 100 轮重跑
  （决定不做对照，记成结论而不是待办）；
- 评测本身还没做 pass@k / trials 统计（`--trials` 参数在，逻辑未接）。

---

## 2026-11-21（下午）：exec 执行环境

**改了什么**

- 新增 `gradus/coding/execenv.py`：`ExecEnvironment` attach 到已存在的容器，命令走
  `docker exec -i -w <映射路径> <容器> ...`；`recycle()` 只杀 exec 子进程，`aclose()` 不删容器；
- `config`：`sandbox: exec` + `GRADUS_SANDBOX_CONTAINER` / `_WORKSPACE` / `_PYTHON`，缺容器名直接报错；
- `session._build_environment`：新增 `exec` 分支，**不建 workspace 副本**（容器一次性）；
- `bench`：新增 `exec_mode.py`（起容器 → 九条预检 → 会话 → 自检 → 取 patch → 删容器）、
  `trace_writer.py`（两种模式共用轨迹格式）、`run.py --mode exec --self-checks N`；
- 修跨平台写文件：`gradus/coding/tools/files.py` 与 `bench/common.py` 全部改成**显式 LF**。

**为什么**

Gradus 要求 Python ≥3.13，而 SWE-bench 实例镜像是 3.9/2.7；之前把 Gradus 塞进实例镜像，被迫
搞出「双解释器 + uv + editable 路径 + 统一挂载 + 离线 wheel」一整套。把「跑 Gradus 的环境」和
「执行命令的环境」拆开后，这些复杂度全部消失。

**怎么验证**

- 单测 988 passed（新增 `test_execenv.py` 10 条、`test_exec_mode.py` 5 条、LF 写入回归 1 条）；
- 真容器跑通（`bench/runs/exec-local3`）：30 轮 / 322,974 输入 / 92.7% 缓存 / 34 次工具调用，
  轨迹 138 行；自检把官方 `test_patch` 打进副本、跑 `pytest tests/test_blueprints.py` → **60 passed**；
- 补丁 1.8 KB、纯 LF。

**踩到的坑（都已修）**

1. 宿主侧文件工具 `Path.write_text` 在 Windows 写 CRLF → git 把整文件算成改动；
2. bench 写 `test_patch` 也是 CRLF → 容器里 `git apply` 永远失败；
3. 自检命令拼成了 `python3 python -m pytest`（解释器前缀与命令重复）；
4. 自检副本里残留 `git add -N` 的 intent-to-add → `git apply -3` 报「lacks necessary blob」。

**没做完（外部条件）**

**P4 初步对照（本地替身镜像，非官方分）**：同一道 flask-5014，旧 `container` 模式 18 轮 / 234k 输入 /
墙钟 163s；新 `exec` 模式 18 轮 / 190k 输入 / 墙钟 55s，自检都是 60 passed。token 少 19%，
达到验收标准（≤ 旧模式 1.2 倍）。

- **官方判定（P3）已完成：`resolved: true`**。官方镜像拉不到（Docker Hub 不可达），用本地按同配方
  重建的实例镜像（`python:3.11` + miniconda `testbed` + base commit 仓库 + 同版本依赖）替代，
  命名与数据集里的 `image` 一致；harness 用实例自带的 `eval_script` 判定：
  `FAIL_TO_PASS` 通过、`PASS_TO_PASS` 60 条全过。
- 顺带在 `bench/swenv` 里给 harness 打了个补丁：`eval.sh` 改为 LF 写入（Windows 上默认 CRLF 会让 bash 跑不了）。

---

## 2026-11-22：第一次 exec 评估（4 题，官方判定）

**配置**：`--mode exec`、官方 SWE-bench 实例镜像、`--max-turns 60`、`--self-checks 1`、单并发。

| 题 | 轮 | 输入 | 缓存 | 官方判定 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `psf__requests-1766` | 25 | 481,880 | 93.8% | **ENV** | FAIL_TO_PASS 6/6 全过；2 个 PASS_TO_PASS 是环境问题（gold 同样挂） |
| `psf__requests-1921` | 44 | 697,332 | 93.9% | **ENV** | FAIL_TO_PASS 全过；3 个 PASS_TO_PASS 环境问题（gold 同样挂） |
| `pylint-dev__pylint-4661` | 55 | 1,108,446 | 95.8% | **resolved: true** | 运行到 20 分钟超时被中断，但已写出的补丁通过判定 |
| `pylint-dev__pylint-4970` | 60 | 1,141,425 | 97.0% | **resolved: true** | 自检 `pass` |

**口径**：raw `resolved` 2/4；按 gold 基线折抵环境问题后 4/4（ENV 的两题目标测试全过、
失败项与 gold 完全一致）。ENV 判定需要 gold 基线，基线在 `H:\bench\baselines\`。

**评估过程中修掉的 exec 模式问题**

1. `TraceWriter.record(kind=...)` 与 `AgentError.kind` 撞名 → 参数改名 `event_type`；
2. **shell 工具按宿主平台注册**：Windows 上给了 `powershell`，但容器是 Linux → 改成一个规则：
   `docker` / `exec` 后端只注册 `bash`；
3. `codemode` 的 harness 在宿主 scratch、容器看不见（`map_path` 报越界）→ exec 模式暂不注册；
4. 自检解释器：`conda run` 会被老仓库的 `requests` 通过 PYTHONPATH 盖住而崩 → 直接用
   `/opt/miniconda3/envs/testbed/bin/python`，并且**只跑 FAIL_TO_PASS 的节点**（整文件跑会把
   环境导致的无关失败算进来）；
5. 自检不再设 PYTHONPATH（靠 cwd 导入仓库），避免污染环境依赖；
6. exec 模式补上**宿主侧超时**（原来只有容器内 timeout，宿主的 prompt 不会被打断）；
7. 老仓库工作区 CRLF 与索引不一致：起容器后 `git checkout -- .` 对齐；预检对「仅行尾差异」放行。

## 2026-11-22（第二批）：4 题官方判定全过

`--mode exec`、官方镜像、`--max-turns 60`、`--self-checks 1`、单并发：

| 题 | 轮 | 输入 | 自检 | 官方判定 |
| --- | --- | --- | --- | --- |
| `pytest-dev__pytest-10081` | 21 | 211,928 | pass | **resolved: true** |
| `sphinx-doc__sphinx-10323` | 45 | 674,175 | pass | **resolved: true** |
| `sympy__sympy-11618` | 49 | 777,085 | fail | **resolved: true** |
| `pydata__xarray-2905` | 49 | 839,703 | pass | **resolved: true** |

**累计（8 题）**：raw `resolved` **6/8**；按 gold 基线折抵环境问题后 **8/8**。
其中 requests 两题的失败项全是网络类（postbin / SSL / 重定向），gold 同样挂。

新增 `bench/results.py`：把多轮 report.json 汇总成表，自动做 ENV 折抵（网络类测试也算环境），
`--json` 可导出。

## 2026-11-22（第三批）：4 题，3 过 1 挂

| 题 | 轮 | 输入 | 自检 | 官方判定 |
| --- | --- | --- | --- | --- |
| `matplotlib__matplotlib-13989` | 47 | 601,492 | pass | **resolved: true** |
| `sympy__sympy-12096` | 59 | 1,164,409 | fail | **resolved: true** |
| `scikit-learn__scikit-learn-10297` | 45 | 771,037 | pass | **resolved: true** |
| `django__django-10097` | 24 | 292,633 | fail | **FAIL**（7 个 FAIL_TO_PASS 全挂） |

django 那题：模型改的是 `django/core/validators.py` 的 URL 正则，而判定要求的
`FAIL_TO_PASS` 是 `auth_tests.test_templates` 的 7 个模板测试；自检时官方 `test_patch`
甚至应用失败（`does not match index`）。属于**真实失败**（不是环境折抵），保留在分子外。

**累计（12 题）**：raw `resolved` **9/12**；含 env 折抵（requests 两题）**11/12**。

**评估期间又修了两个 exec 模式问题**：

1. 容器起失败时（例如大仓库 `git checkout` 超时）容器会泄漏 → 起容器失败也走 `docker rm -f`；
2. 预检的 Python 检查用 `python -V`，输出在 **stderr**，被当成失败（scikit 那题直接被拦下）→ 改成
   `python -c 'print(sys.version)'` 并合并 stdout/stderr；大仓库 `git status` 超时从 120s 提到 600s。

## 2026-11-22：多 agent 修复

审查 `gradus/coding/multiagent.py` + `tools/multiagent.py` 后修的四处：

1. **`wait` 不传超时会无限等**：工具把 `None` 透给 `asyncio.wait(timeout=None)`，而工具描述
   与 `AgentTeam.wait` 的默认值都是 600 秒。现在缺省显式取 `DEFAULT_WAIT_SECONDS`；
2. **并发 `spawn` 名字会撞**：名字原来是 `sub-{counter+1}` 算完才 `await connect`，两个并发
   spawn 都会拿到 `sub-1`，后者覆盖注册表、前者失联。改成先占号再 await；
3. **`aclose` 撞上 in-flight spawn 会漏子 agent**：`connect` 期间 `aclose` 已经清空注册表，
   新会话回来后照样注册并 `create_task`。现在 connect 之后复查 `_closed`，是则关会话、放回节点、抛错；
4. **工具“看得见、一调就报错”**：`Session.connect` 以前无条件建团队槽位，于是所有会话都注册
   `spawn_agent` 却没人回填团队。新增 `Session.connect(..., multiagent=False)`：不建槽位、
   不注册工具；bench 两条连接路径都显式传 `False`。

顺带：`AgentTeam(max_agents=8)` 限制子 agent 数量；`wait` 里“取消会顺带取消子 agent”的错误注释改正。

测试：`test_multiagent.py` 37 条（新增 5 条：默认超时、并发命名、aclose-during-spawn、
数量上限、无槽位 attach），全量 **992 passed**。

## 2026-11-22：多 agent 接入 bench（并修掉 spawn 的 NameError）

**接入**

- `bench/run.py` 新增 `--multiagent`；`exec_mode` 在开关打开时建 `AgentTeam`、
  `session.attach_team(team)`，收尾 `aclose`；
- 子 agent 用量单独记账：`TaskResult` 新增 `multiagent / subagents / sub_input / sub_output`，
  轨迹新增 `multiagent` 与 `subagent_turn` 事件；报告多一行 `subagents N 个 / 子 token X`；
- 启用了多 agent 时会往用户消息追加一段 `DELEGATION_HINT`（提示用 task 工具建节点、`spawn_agent` 派活）；
  `GRADUS_MA_FORCE=1` 可再加一句“必须派活”，仅用于冒烟。

**修掉的 bug**：`Session.spawn` 里引用了不存在的局部变量 `skill_usage`，
**任何一次真实 spawn 都会 `NameError`**（以前从没跑过真实派活，所以一直没暴露）。
改成 `self._skill_usage`。

**冒烟结果**（`pytest-dev__pytest-10081`，强制派活）

| 项 | 单 agent（历史） | 多 agent（eval-ma5） |
| --- | --- | --- |
| 判定 | resolved | **resolved: true** |
| 主轮数 | 21 | 59 |
| 主输入 | 211,928 | 971,298（缓存 96.2%） |
| 子输入 / 输出 | — | 772,578 / 31,934（49 轮） |
| 合计输入 | 211,928 | **1,743,876（≈8.2×）** |
| 墙钟 | 4.5m | 19.8m |

**观察**：不加“必须派活”的提示时，两题（pytest / django）都是 0 次 `spawn_agent` ——
模型倾向于自己直接改；只有强制提示才会派活。也就是说 multi-agent 目前是“能用但不会自发用”，
是否值得默认开启要另做成本/收益判断。

## 2026-11-22（续）：先规划后执行 + 类型化通信 + 文件所有权

**1. `plan_tasks`：一次建完整张 DAG**

- 参数 `nodes: [{key, subject, description, blocked_by}]`，服务端先全量校验
  （key 唯一、描述非空、依赖指向已知 key、环检测），再建节点、连边、置 `graph.planned`；
- `AgentTeam(require_plan=True)`（bench 默认开启）：图没规划、或节点没有 briefing，
  `spawn_agent` 直接拒绝；
- 以前是“模型可以随便建一个空节点就派活”，现在必须先把 DAG 写出来。

**2. 文件所有权：父子不能改同一批文件**

- `spawn_agent(files=[...])` 声明子 agent 运行期间独占的路径；
- 父 agent 的 `write_file` / `edit` / `delete_file` 碰到被占路径直接失败：
  `pkg/a.py is owned by sub-1 right now: do not edit it...`；
- 子 agent 结束/关闭自动解除；派活返回值也写明 handoff。

**3. 通信**

- 消息带 `kind`：`text | question | result | status`，渲染成
  `<message from=sub-1 kind=question>`；
- 子 agent 完成/失败时**主动通知 lead**（`[subagent sub-1] task X completed … result: …`），
  不必只能靠 `wait` 轮询。

**测试**：1002 passed（新增：计划建图/环检测/未知 key、无计划拒绝派活、kind 校验、
scope 阻断父写、完成通知）。

## 2026-11-22（收尾）：删掉 multi-agent 的“关闭模式”

多 agent 是常驻能力，不该有开关。改动：

- `Session.connect` 删除 `multiagent` 参数，团队槽位永远创建，`spawn_agent` 等工具永远注册；
- `bench/run.py` 删除 `--multiagent`，`exec_mode` 与 `container_task` 不再传该参数；
- bench 每次跑都会建 `AgentTeam(require_plan=True)` 并记账子 agent 用量；
- `GRADUS_MA_FORCE=1` 仅保留为“强制派活”的冒烟开关，不影响能力注册。

现在唯一的“门槛”是**先规划**：`spawn_agent` 前必须 `plan_tasks` 建好 DAG。

## 2026-11-22（第四批 / batch5）：4 题全过，且 0 次派活

`--mode exec`、官方镜像、多 agent 常驻（无开关）、`--max-turns 60`、`--self-checks 1`、单并发：

| 题 | 轮 | 输入 | 自检 | 官方判定 |
| --- | --- | --- | --- | --- |
| `django__django-10880` | 41 | 440,380 | fail | **resolved: true** |
| `scikit-learn__scikit-learn-10844` | 42 | 551,928 | pass | **resolved: true** |
| `pydata__xarray-3095` | 52 | 789,644 | pass | **resolved: true** |
| `sphinx-doc__sphinx-10435` | 23 | 234,033 | pass | **resolved: true** |

合计 2.0M 输入 / 91.4k 输出 / 缓存 96% / 40 分钟。

**关键观察**：多 agent 已常驻且 `plan_tasks` 门槛就位，但四道题**全部 0 次 `spawn_agent`**——
模型仍然选择自己干完。说明「模型自决」下它不认为这些任务需要分工；想观察多 agent 的真实收益，
需要挑单 agent 会失败/超长上下文的题，而不是常规题。

**累计（16 题）**：raw `resolved` **13/16**；含环境折抵（requests 两题）**15/16**。


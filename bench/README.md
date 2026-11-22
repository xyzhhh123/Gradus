# 评测（bench）

跑公开榜的 SWE-bench 子集，**按题独立、可切片、可续跑**。

## 为什么是切片

评测天然是「按题」的：一题一个仓库副本、一条结果记录。所以可以「跑几道停一下，下次接着跑」，
不必占一整块时间。这里的三条约定保证切片能拼成一轮：

1. **参数冻结**：开跑写 `plan.json`（模型、commit、题单、trials），之后不改；
2. **追加记账**：每题结果立刻追加进 `tasks.jsonl`，中断最多损失正在跑的那一题；
3. **只在两题之间停**：到截止时间/题数上限就等当前题跑完再退，不半路掐断。

## 用法

```bash
# 一片：45 分钟，最多 12 题，单题超时 15 分钟
python bench/run.py --run-id swe-01 --tasks bench/tasks.jsonl \
    --minutes 45 --max-tasks 12 --timeout 900 \
    --mode container --max-turns 100 --workers 2

# 汇总（可随时跑，n 没跑完就写 n/总数）
python bench/report.py --run-id swe-01

# 判定（先跑 gold 基线，环境问题不算模型回归）
python bench/grade.py --run-id swe-01 --with-gold

# 清理 harness 容器与实例镜像
python bench/cleanup.py --containers
python bench/cleanup.py --tasks bench/tasks.jsonl --images
```

## 两种执行模式

> 第三条路（agent 在自己盒子里、命令 `docker exec` 进实例容器）的方案见
> `coding/docs/exec_environment.md`。


| 模式 | 边界 | 环境 | 用途 |
| --- | --- | --- | --- |
| `--mode container` | 容器 | **实例镜像**：仓库依赖在 `conda testbed`，Gradus 另装 py3.13 | 旧路径，作对照 |
| `--mode exec` | 容器 | Gradus 在宿主，命令 `docker exec` 进实例容器 | **推荐**（见 `coding/docs/exec_environment.md`） |
| `--mode host` | **无** | 宿主 Python | 只调 harness 逻辑，别用来看分数 |

`--mode exec` 说明：

- 需要官方实例镜像（`swebench/sweb.eval.*`）；本地没有时会失败，需先由 harness 构建；
- `--self-checks N`：收尾自检最多回喂 N 次（0 表示不跑）；
- 题单可带两个可选字段：`allow_network`（true 时不禁 github/pypi，仅本地替身环境用）、
  `setup_cmd`（容器起来后先执行的准备命令，例如 `pip install -e . pytest`）。

关键参数：

- `--sandbox local`：容器本身就是边界，不要再叠副本；
- `--max-turns 100`：轮数上限（默认 60）；
- `--workers 2`：同时跑几题（每题一个容器，注意内存）；
- `--allow-network`：例外放行网络（默认屏蔽 github / pypi / HF——联网等于能去查上游答案）。

## 目录

```
bench/
  common.py          # 任务/结果/记账/Wilson/汇总格式
  run.py             # 切片运行器（--workers 并发）
  agent.py           # 宿主直跑执行器（调试用，无边界）
  container.py       # 容器执行器：实例镜像 + 挡答案域名 + 自检参数
  container_task.py  # 容器内驱动：跑 Gradus → patch → 自检 → 失败再修
  grade.py           # 官方判定：gold 基线对照 + 实时日志 + 自动清容器
  prepare.py         # 克隆题目仓库（强制 LF）
  make_tasks.py      # 从 SWE-bench Verified 生成题单
  report.py          # 汇总
  results.py         # 多轮判定汇总 + ENV 折抵（未判定 / 失败分类）
  cleanup.py         # 清 harness 容器与实例镜像
  Dockerfile                    # 空盒子（python:3.13 + gradus）
  Dockerfile.agent-instance     # 实例镜像盒子（+ uv py3.13 + conda testbed）
  tasks.jsonl        # 题单（一行一题）
  runs/<id>/         # plan.json / tasks.jsonl / patches/ / sessions/
  tests/             # 离线测试：不联网、不跑模型
```

## 题单格式

```json
{"id": "django__django-11099", "instruction": "……issue 原文……",
 "workspace": "D:/bench/work/django__django-11099", "repo": "django/django",
 "base_commit": "abc123", "test_cmd": "python -m pytest tests/xxx -q"}
```

- `workspace`：已 checkout 到 `base_commit` 的仓库副本；执行器只在这里跑 `gradus`，
  结束后 `git add -A -N && git diff` 取 patch；
- `test_cmd`：**本地快速判定**用。真正的判定仍应交给 SWE-bench 官方 harness；
  没给命令时 `resolved` 记 `null`，不算进通过率分母；
- 会话文件写到 `runs/<id>/sessions/<task>/`，**不污染仓库**（否则会进 patch）。

## 每题记什么

| 字段 | 含义 |
| --- | --- |
| `resolved` | 通过 / 未过 / `null`（没判定） |
| `input` / `output` / `cache_read` / `cache_write` | 用量（来自 `TurnEnd` 事件） |
| `turns` | 模型轮数 |
| `tool_calls` / `tool_errors` | 工具调用次数与失败次数 |
| `compactions` / `trimmed` | 会话文件里的压缩记录 / 兜底裁剪记录条数 |
| `failure_class` | `超时` / `patch空` / `测试未过` / `agent:<kind>` |
| `wall_seconds` | 墙钟，**只用于排期与超时，不作为效果指标** |
| `checks` / `self_check` | 收尾自检跑了几次、结果 |

## 轨迹：容器里到底发生了什么

**每次运行都会保留三份轨迹**，优化靠它们：

| 文件 | 内容 |
| --- | --- |
| `runs/<id>/container/<task>/<task>.trajectory.jsonl` | 逐事件：`turn_start` / `tool_call`（含参数）/ `tool_result`（含输出、是否出错、耗时）/ `turn_end`（用量）/ `error` / `end` |
| `runs/<id>/container/<task>/sessions/*.jsonl` | 原始会话文件：完整消息、工具结果、`usage` 记录 |
| `runs/<id>/container/<task>/<task>.diff` | 这一题产出的补丁 |

轨迹是**边跑边 flush** 的，超时被杀也不会丢已经发生的步骤。快速查看：

```bash
python bench/trace.py --run-id slice-01 --task pallets__flask-5014
```

它打印：轮数 / 工具分布 / 失败调用 / 最近 N 次调用；`--full` 打印完整输出。

## 报告长什么样

```
Gradus abc1234 | deepseek-v4.1-flash | swe-bench-verified(20/20) | trials=1
resolved   7/20  (35%, Wilson 18–57%)
tokens     4.1M in / 92.0k out / cache 63%
per solved 586.0k tokens
turns/task 23 (median)   tools 820   tool-error 49
compaction 0   trimmed 0
failures   patch空 5 | 测试未过 6 | 超时 2
wall       192m（仅排期参考）
```

**别报孤立的百分比**：20 题的 Wilson 区间有 ±20 个点，切片适合攒数据，不适合单次下结论。

## 磁盘与缓存（本机现状，2026-11 核实）

评测会大量吃磁盘（每题一个 Docker 镜像 1–2 GB），**全部放在 H 盘**，不碰 C：

| 东西 | 位置 | 现状 |
| --- | --- | --- |
| Docker 磁盘镜像（vhdx） | `H:\DockerData\DockerDesktop\DockerDesktopWSL` | 25.8 GB，由 Docker Desktop 的 `CustomWslDistroDir` 指定 |
| 题目工作区（仓库副本） | `H:\bench\work\<instance_id>` | 题单里的 `workspace` 写绝对路径；跑完可删 |
| 结果 / patch / 会话 | `bench/runs/<id>/` | 很小，跟着仓库走 |
| npm 缓存 | `H:\cache\npm` | 2.0 GB，`npm config set cache` 已固化 |
| uv 缓存 | `H:\cache\uv` | 1.5 GB，`UV_CACHE_DIR` 用户级环境变量 |
| pip 缓存 | `H:\cache\pip` | `PIP_CACHE_DIR` 用户级环境变量 |

要点：

- **C 盘只剩 23 GB 左右**，别往上面放镜像或仓库副本；
- 环境变量对**新开的进程**生效——已开着的终端/IDE 读不到，重开一个就行；
- `C:\Windows\Temp` 里 Gradus 测试留下的 `gradus-*` 目录，启动时会由 `cleanup_temp` 回收；
  受限令牌建的少数目录 DACL 受保护，删不掉也不占空间（几 MB）。

## 还没做的

- **实例镜像要先建**：容器模式用的 `swebench/sweb.eval.*` 不在 Docker Hub 上，只能由官方
  harness 现场构建。第一次跑某题时先 `bench/grade.py ... --with-gold`（它顺手把 gold 基线
  也跑了），或直接 `-p gold` 跑一次判定；
- 判定后要清残留：`cleanup_containers()` 已自动清 `sweb.eval.*`，但**实例镜像不会自动删**
  （每个 1–5 GB），用 `bench/cleanup.py` 按需清；
- `--trials` 参数在，但 pass@k 的统计没接。

## 挂载点约定

**仓库挂到 `/testbed`**，和实例镜像里 conda 环境的 editable 安装路径一致——这样
`conda run -n testbed python -m pytest` 测的就是模型刚改的代码，不需要复制文件。
`git diff` 在里面取，patch 仍是相对路径，判定侧不受影响。

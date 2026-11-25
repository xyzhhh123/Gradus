# Terminal-Bench

把 Gradus 作为 agent 接进 [Harbor](https://harborframework.com) 跑
[Terminal-Bench 4.1](https://tbench.ai)（66 题）。和 SWE-bench 那套完全独立：
入口是 `harbor_agent.py`，框架用 `uv tool install harbor` 装，题目定义和 bundle
都在 `.cache/` 下、不进版本库。

**踩过的坑单独记在 [`terminal-bench-pitfalls.md`](terminal-bench-pitfalls.md)**——
容器装不上依赖、Windows 上的符号链接、无人值守的三处截断、判定日志的假信号、
轮数数错的两倍偏差，都在那份里。动手前建议先扫一遍。

## 目录

| 路径 | 内容 | 入库 |
| --- | --- | --- |
| `harbor_agent.py` | `GradusAgent`，Harbor 的 `BaseAgent` 实现 | 是 |
| `build_bundle.sh` | 构建便携 bundle | 是 |
| `run_tb_suite.ps1` | 按顺序运行 Terminal-Bench 任务并汇总结果 | 是 |
| `tests/test_harbor_agent.py` | adapter 的离线回归测试 | 是 |
| `terminal-bench-pitfalls.md` | 环境、截断、日志和轮数踩坑记录 | 是 |
| `.cache/tb-tasks/` | 66 道题的定义（约 507 MB） | 否 |
| `.cache/gradus-bundle/` | standalone CPython 3.13 + Gradus（约 523 MB） | 否 |

## 一次性准备

```bash
# 1. 框架本身（是 CLI 工具，不是包依赖）
uv tool install harbor

# 2. 便携 agent bundle（需要 Docker）
bash bench/build_bundle.sh

# 3. 题目定义
harbor download "terminal-bench/terminal-bench@4.1.0" -o bench/.cache/tb-tasks
```

第 2 步存在的原因：Terminal-Bench 的镜像可能是**裸 `ubuntu:24.04`**——没有
curl、没有 git、没有 python，而且部分题里 apt 连不上。所以「在任务容器里现装
Gradus」这条路过不了所有镜像。改成上传一个固定 bundle 后，每道题的 setup 完全
一致，且只要几秒。三条约束（自带解释器、无符号链接、自带 `LD_LIBRARY_PATH`）
的由来写在 `build_bundle.sh` 的头部注释里。

每次修改 `gradus/` 后，开始 Terminal-Bench 前都必须重新构建 bundle。旧 bundle
会继续上传旧代码，分数不再代表当前工作区。适配器会把 Gradus 的
`[error] <kind>: <message>` 写入 `AgentContext.metadata["gradus_error"]`，
这样即使 CLI 以 0 退出，也能在 Harbor 结果中追溯 runtime error。

每个 trial 结束前，适配器还会在容器删除前归档：

```text
agent/sessions/*.jsonl          # 完整 Gradus 消息、工具调用与结果、usage
agent/agent-events.jsonl       # CLI 逐事件日志
agent/gradus-agent.log         # 终端渲染输出
```

即使 provider 超时或 agent 异常结束，也会执行归档；下载失败只记 warning，不阻断 verifier。

## 跑一道题

```bash
export PYTHONPATH="$PWD"
harbor run -y -e docker -d "terminal-bench/terminal-bench@4.1.0" \
    -i "terminal-bench/html-js-filter" -k 1 \
    -a bench.harbor_agent:GradusAgent \
    -m deepseek-v4.1-flash
```

- `-i` 要用全名 `terminal-bench/<task>`，只写题目名会报 "No tasks matched"。
- `-a module.path:ClassName` 从 `PYTHONPATH` 导入适配器。
- `-e docker` 是本地跑，不需要云账号。

66 题里有 **3 道在 `task.toml` 里声明了 `gpus >= 1`**，CPU 机器跑不了：
`fp8-rmsnorm-gemm`、`jax-speedrun-gpu`、`math-eval-grader`。

## 先验证打分器，再看 Gradus 的分

在相信任何 Gradus 得分之前，跑一次参考解：

```bash
harbor run -y -e docker -d "terminal-bench/terminal-bench@4.1.0" \
    -i "terminal-bench/html-js-filter" -k 1 -a oracle
```

`oracle` 会套用题目自带的 `solution/`。不消耗模型 token，约五分钟。

- oracle 得 1.0 → 打分器正常，Gradus 的 0 分是真的没解出来。
- oracle 也得 0 → 环境或验证器坏了，Gradus 的分数没有意义。

**这一步别省。** 某道题的验证器在**满分那次**和**失败那次**都会打印
`RUN BATCH N (contains 0 tests)` 和 `No execution detected`——那是批处理器的
显示习惯，不是故障。没有 oracle 基线，很容易把「模型不行」误判成「打分器坏了」。

## 适配器参数

用 `--ak name=value` 传；凭证默认读宿主的 `~/.gradus/config.json`。

| 参数 | 默认 | 为什么 |
| --- | --- | --- |
| `max_turns` | 250 | Gradus 默认 20；长流程和 GUI 题需要更多轮 |
| `max_output_tokens` | 32768 | 默认 8192 会截断长 heredoc，截断后这一轮直接结束 |
| `timeout_sec` | 28800 | 对齐 Terminal-Bench 的 8 小时 agent 预算 |

适配器默认把 `max_turns` 提到 250，并在首轮任务结束后向同一个 Gradus 会话追加一次
通用复盘提示：检查修改过的文件、运行最相关的验证、修复发现的问题。两次 run 的
usage 会累加；如果前一次 provider error 后一次已恢复，会记录 `gradus_errors`，但不会把
最终状态误判为失败。

适配器还会把 Gradus 的 stdout 存到 `<job>/agent/gradus-agent.log`，并解析末尾
`[done] N turns, in X / out Y tokens` 回填 `AgentContext`，这样 Harbor 能报 token 用量。

任务通过 `environment.skills_dir` 提供 skills 时，适配器会把该目录复制到容器的
`~/.gradus/extensions/skills/`，并在 Harbor 中声明 `capabilities.skills=True`。这样
不需要修改任务镜像，也不会把某个题目的 skills 路径写死进 Gradus。

批量脚本把以下结果视为无效并允许重跑：任务未结束、Harbor trial 异常、最终仍有
`gradus_error` 且 reward 为 0。真实答错但没有运行时/环境错误仍然记为有效 0 分。
检测到 `GoUsageLimitError` 时会记录当前题并停止整批，避免余额不足后连续启动容器。

## 怎么数轮数

会话文件每轮会写**两条**带 `usage` 的记录：账本条目
（`{"type": "usage", ...}`）和助手消息本身
（`{"role": "assistant", ..., "usage": {...}}`）。所以 `grep -c usage` 得到的大约
是真实轮数的两倍。要数准：

```bash
grep -c '"type": "usage"' <workspace>/.gradus/sessions/*.jsonl
```

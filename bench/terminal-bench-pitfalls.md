# Terminal-Bench 踩坑笔记

把 Gradus 接进 Harbor 跑 Terminal-Bench 4.1 时踩过的坑，按「现象 → 原因 → 修法」记。
每条都是实测撞出来的，不是推测。按出现顺序排列，从环境到判定再到计数。

## 环境：容器里装不上东西

### 1. 容器里没有 git，`uv tool install git+...` 直接失败

**现象**

```
error: Failed to download and build `gradus @ git+https://...`
  cause: Git executable not found
gradus exited with 127
```

**原因**：`python:3.x-slim` 和 `ubuntu:*` 里都没有 git。而且这条命令被 `| tail` 接了管道，
退出码变成 `tail` 的 0，**适配器以为安装成功了**，到运行阶段才炸成 127。

**修法**：用 tarball 代替 git，并且**不要在安装命令后面接管道**（要接就加 `set -o pipefail`）：

```
uv tool install --python 3.13 https://github.com/<owner>/<repo>/archive/refs/heads/main.tar.gz
```

### 2. 裸 `ubuntu:24.04` 里 curl / wget / git / python3 全都没有

**实测**

```
$ docker run --rm ubuntu:24.04 bash -lc 'command -v <tool>'
curl     缺
wget     缺
git      缺
python3  缺
```

**原因**：66 道题里有 12 道的基础镜像就是裸 `ubuntu:24.04`，什么都得自己装。

**修法**：**不要在任务容器里装 agent**。改成预构建一个便携 bundle，代码里 `upload_dir`
传进去。理由和三条约束见 `build_bundle.sh` 头部。

### 3. apt 在容器里挂死

**现象**：`apt-get update` 跑了 9 分钟没动静；`timeout 40 apt-get update` 返回 `rc=124`。

**原因**：这台机器上容器默认**不走宿主代理**，到 ubuntu 归档的请求被丢。

**修法与实测对比**

| 条件 | 结果 |
| --- | --- |
| 带 `HTTP_PROXY=http://host.docker.internal:7897` | `rc=0`，38 秒成功 |
| 不带 | `rc=124`，40 秒超时 |

关键：**容器里不能写 `127.0.0.1:7897`**（那指向容器自己），必须 `host.docker.internal`。

另外注意 Docker Desktop 的设置里 `OverrideProxyHTTP` 可能已经填了
`http://127.0.0.1:7897`，但 `docker run` **不会自动注入**到容器环境变量里——构建阶段
Docker Desktop 会处理，运行阶段要自己传。

## Windows + Docker 的文件搬运

### 4. `docker cp` 不能还原符号链接

**现象**

```
symlinkat /usr/local/bin/python3.13 gradus\venv\bin\python:
    A required privilege is not held by the client.
```

复制中途失败，只出来 21 个文件 0 MB。

**原因**：Windows 上创建符号链接需要管理员或开发者模式，`docker cp` 没法做。

**修法**：导出前把 bundle 里**所有软链解引用成实体文件**：

```dockerfile
RUN find /opt/gradus -type l | while read -r l; do \
      cp -a "$(readlink -f "$l")" "$l.real" && rm -f "$l" && mv "$l.real" "$l"; \
    done
RUN test -z "$(find /opt/gradus -type l)"
```

`python -m venv --copies` 还不够——它本身还会留一个 `lib64 -> lib`，要另外 `rm -f`。

### 5. venv 拷到别的发行版跑不起来

**现象**

```
/opt/gradus/venv/bin/python: error while loading shared libraries:
    libpython3.13.so.1.0: cannot open shared object file
```

**原因**：在 `python:3.13-slim`（Debian）里建的 venv，python 二进制依赖
`/usr/local/lib/libpython3.13.so`，裸 Ubuntu 里没有这个库。

**修法**：用 **python-build-standalone**（`uv python install`），它自带全部 `.so`：

```dockerfile
RUN uv python install 3.13 --install-dir /opt/gradus/py
```

运行时把它的 lib 目录加进加载路径：

```bash
export LD_LIBRARY_PATH=$(ls -d /opt/gradus/py/cpython-3.13*/lib 2>/dev/null | head -1)
```

### 6. `ls -d` 的 glob 匹配到两行，把 `LD_LIBRARY_PATH` 拼坏

**现象**：报的路径明显不是合法值。

**原因**：`uv python install` 会在 `py/` 下**同时留两个目录**
（`cpython-3.13-linux-x86_64-gnu` 和 `cpython-3.13.18-linux-x86_64-gnu`），
`ls -d ...*` 输出两行，`$(...)` 展开成含换行的字符串。

**修法**：加 `| head -1`。

### 7. 从 GitHub archive 装代码会拿到缓存的旧快照

**现象**：本地已经改了 prompt 并推送，重新构建 bundle 后里面还是旧代码。

**原因**：`.../archive/refs/heads/main.tar.gz` 走 CDN，推送后短时间内仍返回旧的打包结果。

**修法**：构建时**从本地源码树装**，而不是从远端拉：

```dockerfile
COPY . /src
RUN uv pip install --python /opt/gradus/venv/bin/python /src
```

顺带好处：bundle 精确反映工作区，改完不用先推送就能测。

### 8. 导出后忘了删中间容器

`docker create --name xxx img` → `docker cp` → **必须 `docker rm xxx`**。
忘了就会留一个 `Created` 状态的容器，下次同名创建直接冲突。

## 无人值守运行

### 9. 没有 `--yes`，每条命令都被拒

**现象**（agent 日志）

```
允许 bash {"command": "ls -la /app/packet/"}？ [y/N]
[tool] bash error 2ms: 用户拒绝
...
Shell access is blocked.
```

**原因**：Gradus 的命令确认默认要求人工输入 `y`，非交互环境里 stdin 立刻 EOF，等于全拒。
结果是模型只能读文件、一条命令都跑不了。

**修法**：给 CLI 加 `--yes`（已实现），适配器在容器里用 `gradus --yes`。
实测：19 次自动放行，0 次拒绝。

### 10. `max_turns=20` 对评测题远远不够

一道 79 轮的题会在第 20 轮被砍。适配器默认传 100；GUI 类任务实测需要约 230 轮
（19 个案例 × 每个约 12 轮）。

### 11. `max_output_tokens=8192` 截断长工具调用，整轮直接结束

**现象**：跑 11 轮就 `[done]`，全程 **0 次 `write_file`**，产物没生成。

**原因**：最后一轮 `out=8192`，正好是上限。模型写一个大型 heredoc 就把输出预算烧光，
被截断后这一轮没有工具调用，循环判定为"结束"。

**修法**：适配器传 `GRADUS_MAX_OUTPUT_TOKENS=32768`。

### 12. 适配器自己写死的超时

**现象**

```
RuntimeError: Command timed out after 3600 seconds
  at bench/harbor_agent.py line ... in run -> environment.exec(...)
```

跑满一小时后被杀，误以为是 Gradus 停止。

**修法**：对齐 Terminal-Bench 的单题预算，默认 `timeout_sec=28800`（8 小时）。

### 13. 流会静静挂死，直到 8 小时超时

**现象**：进程 `utime` 完全不动、TCP 连接挂着、一个事件也不来，持续 17 分钟。

**原因**：`httpx` 的 `read=120s` 量的是 socket 读取，**每收到一个字节就重置**。
上游持续发 SSE 保活注释（`: ping`）就永不超时，而注释又被解析层丢掉，上层只看到沉默。

**修法**：在 `gradus/ai/transport.py` 加事件级看门狗，量「两次**解析出的事件**的间隔」。
详见 `gradus/ai/docs/design.md` 的「停滞看门狗」一节。

### 14. Windows 控制台 GBK 编码让 harbor 收尾时崩

**现象**：任务跑完了，但打印汇总表时抛

```
UnicodeEncodeError: 'gbk' codec can't encode character '\u2022'
```

**修法**：跑之前设 `PYTHONIOENCODING=utf-8` 和 `PYTHONUTF8=1`。

### 15. 后台跑 harbor 一定要加 `-y`

否则遇到宿主环境确认之类的交互提示会一直卡住，看起来像"没反应"。

## 判定：别被日志迷惑

### 16. 任务名必须全名

**现象**

```
ValueError: No tasks matched the filter(s) ['html-js-filter'].
There are 66 tasks available in this dataset.
Example task names: ['terminal-bench/atrx-vep-crispr', ...]
```

**修法**：`-i "terminal-bench/html-js-filter"`。

### 17. `contains 0 tests` / `No execution detected` 是噪声，不是故障

某道题的验证器在**拿到 1.0 的 oracle 运行里**同样打印 28 次
`RUN BATCH N (contains 0 tests)` 和 `No execution detected`。

**教训**：看到这两行就去怀疑打分器，是错误的方向。

### 18. 任何 Gradus 得分之前，先跑一次 oracle

```bash
harbor run -y -e docker -d "terminal-bench/terminal-bench@4.1.0" \
    -i "terminal-bench/<task>" -k 1 -a oracle
```

- oracle 1.0 → 打分器正常，Gradus 的 0 是真没解出来
- oracle 0 → 环境/验证器坏了，Gradus 的分数没有意义

只花约五分钟、不消耗模型 token。**这一步省掉过一次，结果把「模型不行」误判成
「打分器坏了」，整个方向跑偏。**

### 19. 三道题需要 H100

`task.toml` 里声明 `gpus >= 1` 的三道，CPU 机器跑不了：
`fp8-rmsnorm-gemm`、`jax-speedrun-gpu`、`math-eval-grader`（都是 `gpu_types = ["H100"]`）。

注意：在 `task.toml` 里 grep `gpu` 会命中大量描述文字，**要 grep 的是
`gpus = N` 这个字段**，否则会把几十道题误判成 GPU 题。

## 计量：数错了会得出错误结论

### 20. `grep -c usage` 数出来的是轮数的两倍

**现象**：轮数从 52 跳到 104，看起来像翻倍异常，甚至超过 `max_turns=100`。

**原因**：会话文件每轮写**两条**带 `usage` 的记录：

1. 用量账本：`{"type": "usage", ...}`
2. 助手消息：`{"role": "assistant", ..., "usage": {...}}`（`session.py` 序列化时带上的）

**修法**：严格数类型：

```bash
grep -c '"type": "usage"' <workspace>/.gradus/sessions/*.jsonl
```

单轮会话实测：总行数 4、`grep -c usage` = **2**、真实 `type=usage` = **1**。

### 21. GUI 类任务的轮数不是文档类任务能比的

| 任务 | 形态 | 轮数 |
| --- | --- | --- |
| `legacy-utility-triage` | 19 个案例，每个要在 VNC GUI 里录入 | 约 230 |
| `html-js-filter` | 单文件，写一个过滤器 | 75 |

所以按平均轮数估预算会严重低估 GUI 类任务。这类题一轮只能推进一步（截图 → OCR →
判断 → 下一步）。

## Docker 磁盘

### 22. 删镜像不会还空间，`vhdx` 只增不减

**现象**：清掉 17 GB 镜像后，`docker_data.vhdx` 还是 30 GB，H 盘一点没多。

**原因**：Docker Desktop 的 WSL2 数据盘是 VHDX，只增长、不自动收缩。

**修法**（需要管理员）：

```powershell
wsl --shutdown
diskpart
  select vdisk file="H:\DockerData\DockerDesktop\DockerDesktopWSL\disk\docker_data.vhdx"
  attach vdisk readonly
  compact vdisk
  detach vdisk
  exit
```

实测一次 44.31 GB → 12.18 GB，H 盘可用 +32 GB。

**前置条件**：`vmmemWSL` 必须真的退出。关掉 Docker Desktop 窗口不够——WSL 虚拟机
仍然握着文件，压缩会报 `ERROR_SHARING_VIOLATION`。必须再 `wsl --shutdown`，
并用 `Get-Process vmmemWSL` 确认没了。

另外 `wsl --manage <distro> --set-sparse true` **对数据盘无效**：它只作用于发行版
自己的根文件系统（那个只有 0.1 GB），Docker 的数据盘是另外挂的。

## 观测一个正在跑的 trial（监控阶段）

### 23. 会话文件只在「一轮结束」时写记录

**现象**：`tail` 会话文件看到最后一条时间戳是 6 分钟前，判定"卡住了"。

**原因**：`usage` 记录在 `TurnEnd` 才追加。当前这一轮哪怕正在流式生成几十秒，
文件里也**没有任何新记录**——"最后活动时间"是「上一轮结束时间」，不是「最后一个事件」。

**修法**：判断是否卡住，看**进程 CPU 时间**而不是文件时间戳：

```bash
awk '{print $14}' /proc/<pid>/stat   # 间隔 30 秒取两次，增长说明在处理流
```

### 24. 按 `gradus` 关键字找进程，会命中 bash 包装层

**现象**：量到 `utime=2` 且始终不变、`wchan=do_wait`，于是判定"进程阻塞、没在干活"。
实际情况是它在正常跑。

**原因**：容器里的进程树是

```
bash -c 'export LD_LIBRARY_PATH=... && printf ... | gradus'   ← 先匹配到它
  └─ /opt/gradus/venv/bin/python .../gradus --yes              ← 真正干活的
```

`case "$c" in *gradus*)` 会**先命中 bash 那层**。它天然 `utime` 很小、`wchan=do_wait`
（在等子进程），于是得出完全相反的结论。

**修法**：精确匹配 python 可执行路径，并取最后一个匹配：

```bash
for x in /proc/[0-9]*; do
  c=$(tr '\0' ' ' < $x/cmdline 2>/dev/null)
  case "$c" in */venv/bin/python*/gradus*) echo ${x#/proc/};; esac
done | tail -1
```

### 25. 探针脚本别内联进 `docker exec`

**现象**：`docker exec <c> bash -lc "python -c \"...\""` 里的引号层层嵌套，最后报
`unexpected EOF while looking for matching '`，或者被 PowerShell 提前吃掉变量。

**修法**：本地写文件，`docker cp` 进去再执行：

```powershell
docker cp .\probe.py "${c}:/tmp/probe.py"
docker exec $c /opt/gradus/venv/bin/python /tmp/probe.py
```

容器里自带 `/opt/gradus/venv/bin/python`，不需要另装。

### 26. PowerShell 里 `$pid` 是只读自动变量

**现象**：`$pid = ...` 静默失败，后续所有 `/proc/$pid/stat` 测量全落空，
输出一片空白却不报错。

**修法**：换个名字（`$gpid`）。同类只读变量还有 `$host`、`$home`、`$input`、`$args`。

### 27. Harbor 启动时会拉 LiteLLM 的价格表

**现象**：job 日志开头出现

```
LiteLLM:WARNING: ... Failed to fetch remote model cost map from
https://raw.githubusercontent.com/BerriAI/litellm/... ReadTimeout
```

重试 3 次、约 30 秒后回退本地备份。

**原因**：Harbor 进程在宿主上跑，不走容器代理，也不读 Docker Desktop 的代理设置。

**影响**：仅拖慢启动，不影响结果。要消掉就给宿主进程设 `HTTPS_PROXY`。

### 28. 中断的 run 会留下验证器容器

**现象**：掐掉一次 GUI 题的 run 后，`docker ps` 里还挂着
`<task>__xxxx__verifier__trial-main-1`，并且会一直跑。

**原因**：验证器是独立容器（`environment_mode = "separate"`），杀 agent 不会连带它。

**修法**：跑新题前先清：

```powershell
docker ps -a --format '{{.Names}}' | Select-String 'verifier'
docker rm -f $(docker ps -aq --filter "name=<task>")
```

另外：agent 阶段结束后 `env-main` 容器消失、`verifier__trial-main-1` 出现，
**不要以为任务已经结束**——判定还在后面。

### 29. 每道题都要上传 523 MB 的 bundle

`setup()` 用 `upload_dir` 把整包传进容器，本地 docker 网络约 20–30 秒/题。
60 题就是 20–30 分钟的纯上传开销。

要压可以裁 bundle（删 `ensurepip`、`test`、`tkinter`、`__pycache__`），
但要注意别删掉 `py/cpython-*/lib` 里的 `.so`——那是自带解释器赖以为生的。

### 30. 测试目录里的旧 bundle 会让整轮结果失真

**现象**：Terminal-Bench 正常跑完，但 bundle 里的 `session.py`、prompt 或工具实现比工作区旧；
本地代码已经改了，容器里仍在跑旧逻辑。

**原因**：`harbor_agent.py` 上传的是 `bench/.cache/gradus-bundle/gradus`，不会自动增量同步。
2026-11-24 的一次 `html-js-filter` 运行中，bundle 的 `session.py` 哈希与工作区不同，
codemode 也缺少新的 `search_tools()`，因此该次分数不能代表当前源码。

**修法**：每次修改 `gradus/` 后、开始 Terminal-Bench 前重新构建：

```bash
bash bench/build_bundle.sh
```

也可以先比哈希：

```powershell
Get-FileHash gradus/coding/session.py
Get-FileHash bench/.cache/gradus-bundle/gradus/venv/lib/python3.13/site-packages/gradus/coding/session.py
```

### 31. Gradus 报了 provider error，但 Harbor 结果看起来没有异常

**现象**：agent 日志末尾有：

```text
[error] provider: timeout: ConnectTimeout:
```

但 `result.json` 的 `exception_info` 是 `null`，trial 看起来正常结束。

**原因**：CLI 捕获 `AgentError`、打印错误并回到 REPL；stdin 随后 EOF，进程退出码仍为 0。
适配器原来只检查非零退出码，因此结构化结果看不到运行时错误。

**修法**：`harbor_agent.py` 现在解析 `[error] <kind>: <message>` 并写入
`AgentContext.metadata["gradus_error"]`，同时仍保留 verifier 对已落盘产物的判定。

**实测**：重建 bundle 后的 `html-js-filter` 重跑中，trial metadata 已出现：

```json
{"gradus_error":{"kind":"provider","message":"timeout: ConnectTimeout:"}}
```

同时 verifier 正常判分，并暴露最终文件自身的
`AttributeError: 'Attribute' object has no attribute 'value'`。

### 32. 容器删除后才发现没有完整轨迹

**现象**：trial 结束后只剩 `gradus-agent.log`，工具参数和结果只有预览；
`/app/.gradus/sessions/*.jsonl` 随容器删除，无法完整回放。

**原因**：Terminal-Bench 的 Harbor 适配器原来只把 CLI 的终端输出写到 host，
没有主动下载容器内会话文件。

**修法**：`harbor_agent.py` 现在在 `run()` 的 `finally` 中下载：

- `/app/.gradus/sessions` → `agent/sessions/`
- `/tmp/gradus-agent-events.jsonl` → `agent/agent-events.jsonl`

CLI 同时带 `--log` 启动，因此 provider 超时或异常退出后也保留逐事件日志。

**实测**：live smoke 的 job 中已经出现：

```text
agent/sessions/20261124T144216-cd1759.jsonl
agent/agent-events.jsonl
```

边界：如果进程在 Agent 正常收尾前被强制 SIGINT，session 里可能只有 usage 账本，
消息尚未提交；此时 `agent-events.jsonl` 仍保留已发生的工具结束事件。正常 provider error
会走 AgentError 收尾并提交已完成轮次。

### 33. 高轮数任务结束后还应该复盘

**现象**：任务第一次跑完时可能只完成了“能运行”的版本，没有主动检查边界条件、隐藏测试或
自己刚改坏的逻辑；GUI/长流程任务还可能超过旧的 100 轮上限。

**修法**：适配器默认 `max_turns=250`，并在首轮 stdin 结束后追加同一会话内的通用复盘提示：
对照原始任务检查改动、运行相关验证、修复问题并报告实际检查。两次 run 在同一个 session
文件中形成连续历史，usage 由适配器累加。

### 34. 任务声明 skills_dir 时，Harbor 会在模型启动前拒绝 agent

**现象**：`cumulative-layout-shift` 只跑了 26 秒，未创建会话：

```text
ValueError: Agent 'gradus' does not support skills;
configured skills require capabilities.skills = True.
```

**原因**：任务在 `environment.skills_dir` 中提供了 skills，但 adapter 既没有声明
`capabilities.skills=True`，也没有把 Harbor 的任意 skills 路径接入 Gradus 自己的
`~/.gradus/extensions/skills/`。

**修法**：`GradusAgent.capabilities` 声明 skills；`setup()` 在 bundle 检查通过后执行：

```text
mkdir -p ~/.gradus/extensions/skills
cp -R <skills_dir>/. ~/.gradus/extensions/skills/
```

路径来自 Harbor 的 `self.skills_dir`，没有写死题目名。回归测试覆盖能力声明和复制命令。

### 35. quota 用尽也会生成 reward=0，不能当作真实答错

**现象**：2026-11-25 的批量运行里，多道题在 8 到 10 分钟内结束，verifier 给出 `0.0`，
但会话 JSONL 只有一行 `{"type":"session",...}`，没有任何模型消息或工具调用。

**原因**：provider 返回 HTTP 429 `GoUsageLimitError`。CLI 捕获错误后正常退出并进入 EOF，
Harbor 随后仍会跑 verifier，于是“没开始回答”被包装成一个看似合法的 0 分。

**修法**：批量脚本同时检查：

- job/trial 是否正常结束；
- `exception_info` 是否为空；
- reward 为 0 时，trial metadata 是否仍有最终 `gradus_error`；
- 错误文本是否为 `GoUsageLimitError`。

只有无异常、无最终 runtime error 的 0 分才是真实失败。遇到 Go 用量上限时停止整批，
记录 invalid attempt，等待额度恢复后重跑。169 字节、只有 session header 的轨迹本身就是
“模型还没有开始请求”的快速判据。

### 36. Harbor Docker 不支持 `allow_internet=false`

**现象**：`batched-eval-parity` 未进入模型阶段：

```text
Trial failed: network_mode='no-network' is not supported by EnvironmentType.DOCKER
environment. Environment providers must enforce the requested network policy or reject the task.
```

**原因**：题目通过 `allow_internet=false` 要求无网络运行，当前 Harbor Docker provider
明确拒绝该策略，而不是忽略它。

**修法**：批量脚本读取 `task.toml`，把这类题记录为 `skip:`，避免覆盖策略后破坏 benchmark
语义，也避免恢复时反复启动必然失败的容器。当前题库里是 `batched-eval-parity` 和
`lake-temp-glm`；升级 Harbor 或改用支持网络策略的 provider 后再跑。

### 37. Bundle 检查通过后，任务容器仍可能退出

**现象**：`freecad-spring-clip` 的 task 环境启动了 5 分多钟，然后出现：

```text
bundle check (rc=1): container ... is not running
RuntimeError: gradus bundle did not run in the task container
```

**原因**：不是 bundle 内容错误，而是任务容器在 agent setup 完成前退出。该题声明 4 GB、
2 CPU、10 GB storage，可能是镜像启动、Docker Desktop 或宿主资源竞争。

**处理**：这类 `RuntimeError`/exit 137 属于环境错误，记为 invalid，资源恢复后重跑。
不要仅凭 `bundle check` 字符串把它归因成 Gradus 安装失败。2026-11-25 中断时 Docker 中
唯一残留是本轮被终止的 `freight-dispatch-shift` 两个 compose 容器。

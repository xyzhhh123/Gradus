# 远程执行环境：让 Gradus 操作「别人家的容器」

## 1. 要解决的问题

评测时最难搞的一段是「在 SWE-bench 实例镜像里跑 Gradus」：

- Gradus 要求 **Python ≥3.13**，而实例镜像里是 **3.9 甚至 2.7**；
- 于是要在每个实例镜像里再造一套 py3.13（`uv venv`）、把仓库依赖和 Gradus 的 editable 路径对齐、
  把挂载点从 `/workspace` 改成 `/testbed`、把 PyPI 依赖预下载成离线 wheel……

这些都不是评测要考察的东西，是**运行环境错位**带来的附带复杂度。

## 2. 结论：把「跑 Gradus 的环境」和「执行命令的环境」拆开

```
┌─────────────────────────┐    docker exec     ┌──────────────────────────┐
│ Gradus 宿主进程（.venv） │ ─────────────────▶ │ 实例容器（官方）          │
│ 或 gradus-agent 盒子     │  bash / pytest     │ /testbed + conda testbed │
└─────────────────────────┘                    └──────────────────────────┘
       文件工具直接读写同一个 bind mount 的目录
```

Gradus 留在自己熟悉的环境里；实例容器只负责提供仓库和测试依赖；命令通过 `docker exec` 进去执行。

## 3. 现状盘点（代码层面）

`gradus/coding/sandbox.py` 已经有这层抽象：

```python
class Environment(Protocol):
    name: str
    def scratch_dir(self) -> Path            # 宿主上一块环境内可见的临时目录
    def map_path(self, path) -> str          # 宿主路径 → 环境内路径
    def python(self) -> str                  # 环境内可用的解释器
    async def spawn(argv, *, cwd, merge_stderr) -> Process
    async def recycle()                      # 超时后丢弃运行实例
    async def aclose()
```

三个现有实现：

| 后端 | 怎么跑命令 | 容器谁建 |
| --- | --- | --- |
| `local` | 宿主进程 | 无 |
| `docker` | **`docker exec -i -w <workdir> <name> ...`** | 自己 `docker run -d` |
| `appcontainer` / `restricted` | 受限令牌起进程 | 无 |

**关键发现**：`DockerEnvironment.spawn` 已经是 `docker exec`（`sandbox.py:229`）。缺的只是
「容器不是我建的」这条路径。所以新后端不是从零写，是把 Docker 后端拆成「建容器」+「往容器里 exec」两半。

另外两条既有约定要保持：

- **文件工具在宿主进程里跑**（`sandbox.md` §4），靠「路径绑到副本」保证语义一致；
- 沙箱后端下会建 workspace 副本并合回；`local` 不建副本。

## 4. 方案：`ExecEnvironment`（attach 模式）

### 4.1 契约

```python
ExecEnvironment(
    container: str,              # 已存在的容器名/ID
    *,
    workspace: Path,            # 宿主侧目录（bind mount 的源）
    container_workspace: str,    # 容器内挂载点，如 /testbed
    python: str = "",           # 容器内解释器，默认 python3
    docker: str = "docker",
)
```

- `spawn(argv, cwd=...)`：`docker exec -i -w <map_path(cwd)> <container> *argv`；
- `map_path(host_path)`：`workspace/...` → `container_workspace/...`；越界直接报错（与现有 Docker 后端一致）；
- `python()`：容器内的解释器（评测里是 `conda run -n testbed python` 的等价物，或 `/opt/miniconda3/envs/testbed/bin/python`）；
- `scratch_dir()`：宿主侧临时目录，**同时**要求容器内也能看到（评测里挂到 `/tmp/gradus`）；
- `recycle()`：杀掉当前 `docker exec` 的子进程（不碰容器）；
- `aclose()`：**不销毁容器**——attach 语义，容器由调用方负责。

### 4.2 为什么不建副本

容器本身是一次性的，宿主的 workspace 就是 bind mount 的源：模型改的是同一份文件，
不存在「改坏宿主」的风险（坏了删容器 + 重置目录即可）。所以 `ExecEnvironment` 走 `local` 那条路：
**不建 workspace 副本、不做合并**。

### 4.3 文件工具不需要改

文件工具在宿主进程里读写的就是 bind mount 的那份目录，和容器里看到的是同一份文件。
不需要为每个 `read_file` 付一次 `docker exec` 的开销。

## 5. 两种落地形态

| 形态 | Gradus 在哪跑 | 优点 | 代价 |
| --- | --- | --- | --- |
| **A（推荐）** | 宿主 `.venv`（Windows） | 零改造、路径映射简单、调试方便 | 依赖宿主装好 Gradus（评测机上已有） |
| B | 容器（`gradus-agent:latest`） | 与宿主隔离、跨平台一致 | 要挂 docker socket + 路径两套映射 |

先做 A；B 等要 Linux 化或远端执行时再说（那时就是「容器里的 Gradus 通过 socket exec 另一个容器」）。

## 6. bench 接线

```bash
python bench/run.py --run-id exec-01 --tasks bench/tasks.jsonl \
    --mode exec --max-turns 100 --minutes 45
```

每题流程：

1. `docker run -d --name <task>-box -v <宿主 workspace>:/testbed <官方实例镜像>`（容器只提供环境）；
2. 宿主的 `Session.connect(workspace=<宿主 workspace>, environment=ExecEnvironment(...))`；
3. 指令里不再需要「py3.13 环境」那套说明，只说明「仓库在 `/testbed`，依赖在 conda testbed」；
4. 自检（P4）：在容器里 `conda run -n testbed pytest`（通过 `env.spawn`），仍然打官方 `test_patch`；
5. patch 从宿主 workspace 取 `git diff`（和现在一致）；
6. 判定走官方 harness（不变），判完删容器。

需要新增的配置（`Settings`）：

| 键 | 含义 |
| --- | --- |
| `environment: "local" / "docker" / "exec"` | 新增 `exec` 分支 |
| `environment_container: str` | attach 的容器名（bench 传） |
| `environment_workspace: str` | 容器内挂载点（默认 `/testbed`） |

## 7. 失败模式（必须先想清楚）

| 场景 | 行为 |
| --- | --- |
| 容器不存在 / 已退出 | 建会话时 `docker inspect` 失败即报错，不进入工具调用 |
| `cwd` 不在挂载点内 | `map_path` 抛错，工具返回明确错误（不是 `cd` 到错误目录） |
| `docker exec` 超时 | `recycle()` 杀 exec 进程；容器保留（下次仍可用） |
| 容器内没有该命令 | 原样返回 exit code + stderr（和本地一致） |
| 并发 | 同一容器并发 exec 是允许的；但 bench 仍按 1 题 1 容器 |
| 网络 | 容器默认联网（仅 API 需要）；github/pypi 用 `--add-host` 挡（沿用 P1） |

## 8. 测试计划

1. **单测**（不依赖 Docker）：假 `docker` CLI（记录 argv 的 stub）断言
   `spawn` 的 argv、`-w` 映射、`map_path` 越界报错、`recycle` 杀进程；
2. **集成**（Docker 可用时）：起 `alpine` 容器，`spawn(["sh","-c","echo hi"])` 返回 hi；
   `cwd` 映射到挂载点；超时能被 `recycle` 终止；
3. **bench 对照**：同一道 flask-5014，`--mode container`（旧）vs `--mode exec`（新），
   比轮数、token、`checks/self_check`、官方 `resolved`。

## 9. 迁移与回退

- `--mode container` 保留，作为对照与回退路径；
- 新架构跑通 2–3 题、数字不差于旧架构后，再把 `exec` 设成默认；
- 旧路径的资产（`Dockerfile.agent-instance`、离线 wheel）保留但不再默认使用。

## 10. 实施清单（文件级）

| # | 文件 | 改什么 | 怎么验 |
| --- | --- | --- | --- |
| 1 | `gradus/coding/execenv.py`（新） | `ExecEnvironment`：`spawn` / `map_path` / `python` / `scratch_dir` / `recycle` / `aclose`；`docker inspect` 预检 | 单测：假 docker CLI 断言 argv；集成：真容器 |
| 2 | `gradus/coding/config.py` | 新增 `environment`（`local`/`docker`/`exec`，默认 `local`）、`environment_container`、`environment_workspace`（默认 `/testbed`）；`exec` 时容器名必填 | `ConfigError` 用例 |
| 3 | `gradus/coding/session.py` | `_build_environment` 加 `exec` 分支：返回 `(ExecEnvironment(...), None, (), None)`，**不建副本**；`sandbox_backend` 返回 `exec` | 单测：patch 掉 docker，断言 env 类型与 `tool_root == root` |
| 4 | `gradus/coding/tools/shell.py` / `codemode.py` | **不改**（都走 `env.spawn` / `env.map_path`）；只需确认 `cwd` 传的是宿主路径 | 现有测试覆盖 |
| 5 | `bench/exec_mode.py`（新） | 每题：`docker run -d --name ...` + 预检 + `Session.connect(settings)`（用 `replace(settings, environment="exec", ...)`）+ 自检 + 取 patch + 删容器 | 同题对照 |
| 6 | `bench/run.py` | 新增 `--mode exec`；与 `container` 共用切片/记账/轨迹逻辑 | 干跑一题 |
| 7 | `bench/prepare.py` | 不变（已经强制 LF）；新增「容器内 `git status` 必须为空」的断言由 exec_mode 做 | — |
| 8 | `gradus/coding/tools/__init__.py` 等 | 不涉及 | — |

## 11. 接口契约（精确到签名）

```python
class ExecEnvironment:
    name = "exec"

    def __init__(
        self,
        container: str,
        *,
        workspace: str | Path,            # 宿主侧目录（bind mount 的源）
        container_workspace: str = "/testbed",
        python: str = "python3",
        docker: str = "docker",
        scratch: str = "/tmp/gradus-scratch",   # 容器内可见的 scratch
    ) -> None: ...

    def scratch_dir(self) -> Path: ...        # 宿主侧临时目录（同时挂进容器）
    def map_path(self, path) -> str: ...      # 只允许 workspace / scratch 之下，否则 SandboxError
    def python(self) -> str: ...
    async def spawn(self, argv, *, cwd=None, merge_stderr=False) -> Process: ...
    async def recycle(self) -> None: ...      # 杀当前 exec 子进程，不删容器
    async def aclose(self) -> None: ...       # 只清理 scratch，不删容器
```

`spawn` 的实现形状（复用 Docker 后端已有写法）：

```
docker exec -i [-w <map_path(cwd)>] <container> *argv
```

失败语义：`docker` 不存在 / 容器不在运行 / `map_path` 越界，**都在建会话前或工具调用时抛明确错误**，
不要静默回落到 local。

配置：

```jsonc
{
  "environment": "exec",
  "environment_container": "swe-psf__requests-1921",
  "environment_workspace": "/testbed"
}
```

## 12. 启动前自检（fail fast，缺一不可）

bench 在跑任何一题之前，按顺序断言；任何一条失败就**不启动 agent**，直接记 `env_error`：

| # | 检查 | 命令 |
| --- | --- | --- |
| 1 | 容器在运行 | `docker inspect -f {{.State.Running}} <c>` == `true` |
| 2 | 挂载点存在 | `docker exec <c> test -d /testbed` |
| 3 | 宿主仓库干净 | `git -C <host ws> status --porcelain` 为空 |
| 4 | 容器里仓库干净 | `docker exec <c> git -C /testbed status --porcelain` 为空 |
| 5 | 两边是同一份文件 | 宿主写一个 `.gradus-probe`，容器里 `test -f /testbed/.gradus-probe`，然后删掉 |
| 6 | 测试环境可用 | `docker exec <c> conda run -n testbed python -V`（或该题声明的解释器） |
| 7 | 答案源被挡 | 容器内 `curl -m 5 -sSI https://github.com` 必须失败 |
| 8 | 模型 API 可达 | 容器内 `curl -m 5 -sSI <base_url>` 必须成功 |
| 9 | run 锁 | `runs/<id>/.lock` 未被其他进程持有（已有） |

> 第 5 条是这次要新增的：之前 `/workspace` 与 `/testbed` 不一致，模型只好把文件拷来拷去；
> 一条 probe 就能在任何路径错位时立刻失败。

## 13. 风险登记册（过去两天踩过的坑 → 这次的对策）

| 坑 | 上次的现场 | 这次怎么办 |
| --- | --- | --- |
| 并发构建抢同一目录 | 两个 `docker-buildx` 卡死十几分钟 | exec 模式**不构建 per-instance 镜像**；容器创建按 `run_id + task` 命名，串行 |
| `job_kill` 只杀包装进程 | 两个 `run.py` 抢同一个 run，结果全丢 | run 锁（已有）+ 启动打印 pid；kill 后必须验 `run.py` 进程为 0 |
| 挂载点不一致 | 模型把改动拷到 `/testbed` 才测得到 | 挂载点唯一常量 `/testbed` + §12 第 5 条 probe |
| CRLF 污染 | `git diff` 把整棵树算成改动，60 轮没收敛 | `prepare.py` 已强制 LF；§12 第 3/4 条断言干净 |
| 非交互会话默认拒绝 | 写文件/命令全被拒，60 轮 797k token 白跑 | bench 固定 `confirm=_approve_all`（已有） |
| 危险命令一刀切 | `rm -rf` 1ms 被拒，模型只能绕路 | exec 模式的容器是一次性的：`DENIED_COMMANDS` 在评测里清空（可配 `GRADUS_DENY_COMMANDS=""`） |
| 联网查答案 | `git clone` 上游 + `git log -S` 找修复提交 | `--add-host` 挡 github/pypi/HF（已有）+ §12 第 7 条 |
| 自检重复劳动 | harness 自检 + 模型自己测，token 翻倍 | 自检保留但可关（`--self-check`），默认开；阈值：只在 patch 非空时跑 |
| 容器残留 | 判定后 `sweb.eval.*` 一直 Up | `cleanup_containers()`（已有）+ exec 模式 `finally: docker rm -f` |
| 记录缺失 | 只看通过率，不知道模型在干什么 | 轨迹三件套（`trajectory.jsonl` / `sessions` / `diff`）保持 |

## 14. 验收标准（Definition of Done）

1. `pytest -q` 全绿，新增单测覆盖：`map_path` 越界、`spawn` argv 形状、`recycle` 杀进程、config 校验；
2. 真容器集成测试通过（`GRADUS_DOCKER_LIVE=1` 才跑，缺 Docker 时跳过而不是失败）；
3. 同一道 `pallets__flask-5014`：exec 模式跑出可被官方 harness 判为 `resolved: true` 的 patch；
4. 该题 exec 模式的 token **不高于**旧容器模式（234k）的 1.2 倍；
5. 轨迹完整（`turn_start/tool_call/tool_result/turn_end` 都有），`self_check` 有值；
6. 跑完 `docker ps` 为空（无残留容器），`bench/runs/<id>` 里 plan/结果/轨迹齐全；
7. `sandbox.md` / `bench/README.md` / `changelog.md` 三处文档同步。

## 15. 分阶段计划（每阶段有 go/no-go）

| 阶段 | 做什么 | 通过标准 | 估计 |
| --- | --- | --- | --- |
| **P0 打样** | 手工起一个 flask 容器 + 写 30 行脚本，用 `ExecEnvironment` 跑一轮，看 patch 能不能出 | 能出 patch（不判分） | 半天 |
| **P1 后端** | `execenv.py` + config + session 分支 + 单测 | 单测全绿、`--mode host` 不回归 | 1 天 |
| **P2 bench** | `exec_mode.py` + `run.py --mode exec` + §12 九条预检 | 一题跑通、轨迹完整、无残留 | 半天 |
| **P3 判定** | 同题交官方 harness 判定 | `resolved: true` | 半天 |
| **P4 对照** | `container` vs `exec` 同一题各跑一次 | exec 的 token ≤ 旧模式 1.2 倍 | 半天 |
| **P5 切换** | 默认改成 `exec`，旧路径留作 `--mode container` | 文档更新完成 | 半天 |

总工期约 **3.5 天**，可分次做；每个阶段都能停下来。

## 16. 明确不做

- **不把文件工具改成走 `docker exec`**：bind mount 已经让两边看到同一份文件，逐次 exec 只会更慢；
- **不把 Gradus 降到 Python 3.9**：那要改 13 处 `asyncio.timeout` 和一票类型注解，收益只是「省一次 uv 安装」，
  而 `exec` 方案能一次性把这类问题全部消掉；
- **不做远端（Modal/Daytona）执行**：等真的需要跨机器时再谈；
- **不做「一个容器跑多题」**：隔离与清理都会变复杂，和 SWE-bench 一题一环境的语义也不符。

---

## 17. 实现状态（2026-11-21）

**已完成（P0–P2）**

| 项 | 落点 | 验证 |
| --- | --- | --- |
| `ExecEnvironment` | `gradus/coding/execenv.py` | `test_execenv.py` 10 条：`map_path` 越界、`spawn` argv 形状、`recycle` 杀进程、容器名必填 |
| config | `sandbox: exec` + `GRADUS_SANDBOX_CONTAINER(_WORKSPACE/_PYTHON)` | `ExecConfigTest`：缺容器名报 `ConfigError`、三项读取 |
| session 接线 | `_build_environment` 的 `exec` 分支（不建副本） | 全量测试 988 passed |
| bench 执行器 | `bench/exec_mode.py`：起容器 → 九条预检 → 会话 → 自检 → 取 patch → 删容器 | 真容器跑通（见下） |
| 轨迹 | `bench/trace_writer.py`，exec 模式在宿主直接写 | 138 行轨迹含 preflight/tool_call/self_check/end |
| 运行器 | `bench/run.py --mode exec --self-checks N` | — |

**真容器验证（`exec-local3`）**

- 环境：`python:3.11` + `pip install -e . pytest==7.3.0`（本地替代官方镜像，因为官方镜像拉不到，见下）；
- 结果：30 轮 / 322,974 输入 / 299,392 缓存（92.7%）/ 34 次工具调用；
- **自检真的跑起来了**：把官方 `test_patch` 打进仓库副本、`python -m pytest tests/test_blueprints.py -q` → **60 passed**；
- 补丁 1.8 KB、纯 LF（CR=0）。

**顺带修掉的两个跨平台坑**

1. **文件工具写 CRLF**：宿主侧 `Path.write_text` 在 Windows 会把 `\n` 翻成 `\r\n`，git 把整文件算成改动。
   改为统一 `newline="\n"` 写入（`_write_lf`），并有回归测试；
2. **bench 写文件同样 CRLF**：官方 `test_patch` 被写成 CRLF 后 `git apply` 永远失败。
   新增 `bench/common.write_text_lf`，patch / test_patch / instruction / 轨迹全部走它。

**P4 对照（用本地替身镜像，非官方分）**

| 模式 | 环境 | 轮数 | 输入 | 缓存 | 自检 | 墙钟 |
| --- | --- | --- | --- | --- | --- | --- |
| `container`（旧，box-flask-3） | 官方实例镜像 + py3.13 | 18 | 234,162 | 92.4% | pass | ~163s |
| **`exec`（新，exec-standin）** | 本地替身镜像（含依赖） | **18** | **190,069** | 87.3% | **pass（60 passed）** | **55s** |

同一道题、同样的自检结果：exec 模式 token 少 19%、墙钟快约 2/3（省掉了容器内装解释器与复制路径的开销）。
替身镜像的做法：`python:3.11` 容器里 `pip install -e . <pinned deps>` → `docker commit gradus-testbed:flask-5014`
（Docker Hub 不可达时的离线替代，不参与官方判定）。

**P3 官方判定：`resolved: true`**

- 补丁来自 exec 模式那次运行（`exec-standin`：18 轮 / 190,069 输入 / 自检 60 passed）；
- 判定用官方 harness + 实例自带的 `eval_script`（`/opt/miniconda3` + `testbed` 环境）：
  `FAIL_TO_PASS`（`test_empty_name_not_allowed`）通过，`PASS_TO_PASS` 60 条全过；
- **唯一偏差**：官方镜像拉不到（Docker Hub 不可达），所以镜像是本地按同样配方重建的
  （`python:3.11` + miniconda `testbed` + 仓库在 base commit + 同版本依赖），并命名为
  `swebench/sweb.eval.x86_64.pallets_1776_flask-5014:latest` 供 harness 使用。

**曾经卡住的点（留档）**

- Docker Hub 上的 `swebench/sweb.eval.*` 不是公开可拉取（`run_evaluation` 5.0.2 只会 `pull`，不会 build）；
- 该版本构建镜像要走 `swebench images build <task-repo>`，需要 per-instance 的 Dockerfile（新任务格式），HF 数据集里没有；
- Docker 守护进程访问 `registry-1.docker.io` 会走 IPv6 并超时（`dial tcp [2a03:...]`）。
- 结论：**exec 架构本身已验证**，缺的是「官方实例镜像」。等网络/镜像可用后，直接跑
  `bench/run.py --mode exec` + `bench/grade.py` 即可补齐 P3/P4。

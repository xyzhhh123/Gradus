# 沙箱:六家 harness 怎么把命令关起来

这篇讲一件具体的事:**当 agent 要执行命令时,不同 harness 用什么把它关住、关到什么程度、关不住时怎么办。**

不讲代码细节,只讲每个选择要解决的问题、代价、以及为什么不同团队得出不同答案。需要核对行号的看文末证据索引。

对照六家:Codex CLI(Rust)、pi-agent(TS)、DeepSeek Harness/DSH(TS)、hermes-agent(Python)、deepseek-reasonix(Go)、Claude Code(TS,闭源)。
**Claude Code 是闭源产品**,结论来自它的 npm 打包产物字符串与官方 CHANGELOG/README,证据类型与前五家不同,文中会点明。

---

## 0. 要防的其实是五件不同的事

| 坏事 | 需要什么 | 谁能防 |
| --- | --- | --- |
| 改坏工作区 | 回滚能力(副本) | 只有 Gradus、hermes(容器/tmpfs) |
| 改坏工作区之外 | 写边界(ACL / 挂载 / 沙箱规则) | codex、DSH、reasonix、claude-code |
| 读到不该读的 | 读边界 | 只要做 `deny_read` 的那几家 |
| 联网 | 内核级断网 | 只有 codex 三平台都做了 |
| 恶意代码主动越狱 | 强隔离(容器/VM) | 都不在射程内,除了远端 VM 模式 |

这五件事的防御成本相差一个数量级。**看懂一家 harness 的定位,就看它默认把哪几件当成自己的责任。**

## 1. 四种流派

| 流派 | 代表 | 一句话 |
| --- | --- | --- |
| **默认不管,靠外挂** | pi-agent、hermes(默认) | 核心 `bash` 直接跑在宿主,隔离是可选扩展或换后端 |
| **OS 原生规则,默认开** | DSH、reasonix、codex | 用系统提供的机制(SBPL / bwrap / ACL / 受限令牌)就地约束 |
| **独立账户 + 规则** | codex(Windows) | 建沙箱账户、ACL、WFP 防火墙,进程以该身份起 |
| **换执行目标** | hermes(docker)、pi(Gondolin VM) | 不在宿主跑,把命令送进容器/VM/远端 |

**没有任何一家把 Docker 当默认**——最接近的是 hermes:它的 `docker` 是"隔离档",但默认仍是 `local`。

## 2. 默认姿态:这是最大的分水岭

| 家 | 默认 | 沙箱不可用时 |
| --- | --- | --- |
| pi-agent | **无沙箱** | 扩展不支持该平台 → **打印提示后继续裸跑**(fail-open) |
| hermes | **local 直跑** | 无降级路径(local 永远可用);docker 起不来是硬失败 |
| claude-code | **不启用**(要配 `sandbox` 才有) | `failIfUnavailable` 决定"启动即退出"还是"拒命令" |
| DSH | **`workspace-write`,默认开** | **fail-closed**,抛 `SANDBOX_UNAVAILABLE` 并点名缺什么 |
| reasonix | **enforce**(mac/Linux 空配置即开) | **fail-closed**,拒跑 bash 并给错误码 |
| codex | 平台绑定(mac/Linux 恒有,Windows 看开关) | Windows 无后端时不静默放行:强制 `Prompt`/`Forbidden` |

一句话:**pi 认为"没有边界"是用户的选择,DSH/reasonix 认为"没有边界"是故障。** 这个立场差异比技术差异重要——它决定用户第一次跑起来时,是"什么都没有"还是"默认就有"。

## 3. 文件系统:写边界是标配,读边界是选配

**写边界**,各家做法不同但都在做:

- **白名单挂载**(DSH/reasonix):`--ro-bind / /` 之后 `--bind` 放行写根;
- **白名单规则**(codex/DSH/mac):SBPL 里 `(deny default)` + 逐条 `allow file-write*`;
- **ACL**(codex Windows):给 workspace 授写 ACE,其余不授;
- **配置面**(claude-code):`filesystem.write.allowOnly` / `denyWithinAllow`,不配则 `--bind / /`(全根可写)。

**读边界只有一半人在做**:codex 有 `deny_read`(Windows 上把 `.ssh/.gnupg/.aws/.kube/.docker` 从 profile 读权限里排除),claude-code 有 `filesystem.read.denyOnly`,DSH 明写"读始终不受限",pi 的扩展有 `denyRead` 但默认不生效。

**三个细节值得单独记**:

1. **元数据强制只读**:codex 把 `.git/.agents/.codex/.aws` 即使落在可写根里也强制只读;reasonix 把 `.git` 的 config/hooks/commondir 单独只读——因为改 `.git/hooks` 等于拿到下一次任意执行。
2. **符号链接与 rename 逃逸**:codex 遇到"可写根内嵌 symlink"直接拒绝启动(注释原话 "symlinked writable roots are not supported"),并把"祖先目录 deny"放在策略末尾,防止被更宽的规则重新打开;reasonix 则给写根做 **身份 pin**,被替换/重定向即失效。
3. **`..` 与路径穿越**:codex 显式拒绝绝对路径、`..`、Windows 盘符切换。

## 4. 网络:只有一家三平台都真的断了

| 家 | 默认网络 | 实现 |
| --- | --- | --- |
| codex | **默认断网** | macOS:SBPL 不给 network 规则;Linux:`--unshare-net` + seccomp 再禁 `connect/accept/bind/listen`;Windows:**WFP 防火墙过滤** |
| DSH | **全通** | 策略词表里没有网络;bwrap 只 `--unshare-pid` |
| reasonix | 默认放行,可切 | bwrap `--unshare-net`;allowlist 走宿主 loopback 代理,每命令一个 token,云元数据地址恒拒 |
| claude-code | 不配就不限制 | 配了走宿主代理;Linux 用 `socat` 把 socket 桥进沙箱 |
| pi | 全通 | 扩展有域名白名单(默认 npm/pypi/github) |
| hermes | docker 默认放网 | 靠 egress 代理强制,`docker_network=false` 才 `--network=none` |

**工程含义**:断网不是"加个 flag",每个平台都要单独实现(SBPL 规则 / netns / WFP / seccomp)。这是为什么大多数 harness 只做写边界。

## 5. 进程与资源:普遍是短板

做的部分:

- **超时**:代码codex 10s、claude-code 120s、DSH 120s(上限 600s)、pi 可选无默认;
- **进程树收割**:进程组 TERM→KILL(codex/DSH/claude)、Windows Job Object + `taskkill /T`、bwrap `--die-with-parent` + `PR_SET_PDEATHSIG`;
- **PID 隔离**:codex/reasonix bwrap `--unshare-pid`(reasonix 未开);
- **降权**:codex`PR_SET_NO_NEW_PRIVS`、seccomp 禁 `ptrace/process_vm_*/io_uring`;
- **容器级**:hermes docker 有 `--cap-drop ALL`、`no-new-privileges`、`--pids-limit 2048`、CPU/内存限额。

**六家里没有一个在原生后端做 rlimit/cgroup 内存 CPU 限额。** 这是行业普遍欠账:超时和进程树是标配,资源配额只有容器路线顺带拥有。

## 6. 审批和沙箱:三种关系,只有一种是对的

| 关系 | 家 | 表现 |
| --- | --- | --- |
| **互为替代** | hermes、claude-code | 有沙箱就不问("auto-allow when sandboxed");没沙箱就回权限流 |
| **正交** | reasonix | 审批管"能不能做",沙箱管"做到哪";两者独立 |
| **沙箱内免问 + 拒绝后显式升级** | codex、DSH | 受限沙箱里非危险命令不问;被拒后由 agent 显式申请升权并触发新审批 |

「互为替代」最方便,也最危险:它默认"沙箱足够好,所以不用问",而沙箱恰好不覆盖的那部分(网络、读、MCP)就没有第二道闸了。

**升级通道**是这几家最值得学的设计:

- codex:沙箱失败 → `SandboxErr::Denied` → agent 主动发 `EscalatedSandboxWithRestrictions` → 触发审批。**不给"拒绝即自动放行"留后门**;
- DSH:升权阶梯是**闭表**(`read-only`→`workspace-write`/`danger-full-access`),`sandbox_permissions` 与 `justification` 必须成对且理由非空,批准粒度是 `allowed-once`。

## 7. 平台覆盖矩阵

| | macOS | Linux | Windows |
| --- | --- | --- | --- |
| codex | ✅ seatbelt | ✅ bwrap+seccomp(+legacy landlock) | ✅ 受限令牌/独立账户 + ACL + WFP |
| DSH | ✅ seatbelt | ✅ bwrap/landlock | ✅ Windows ACL(受限令牌) |
| reasonix | ✅ seatbelt | ✅ bwrap | ⚠️ 受限令牌,需显式开;不拦删/读/网 |
| claude-code | ✅ sandbox-exec | ✅ bwrap+socat | ❌ 不支持(WSL2 可以) |
| pi | ✅ 扩展 | ✅ 扩展 | ❌ 扩展不支持,提示后裸跑 |
| hermes | — | — | 全平台都能跑,但隔离只在容器后端(默认没有) |
| **Gradus** | ❌ | ❌ | ✅ 受限令牌(写边界) |

**Windows 上真正做全的只有 codex**:受限令牌/独立账户起进程、ACL 管文件、WFP 管网络。

## 8. 失败与观测:别用 stderr 猜

- codex 把拒绝归一化成 `FileSystemSandboxViolation` / `NetworkSandboxViolation` 事件,走 tracing;
- DSH 用错误码(`sandbox.no_bubblewrap`、`bwrap_namespace_denied`…)并在错误文本里点名"该装什么";
- reasonix 的拒绝可归因到具体命令和策略;
- **但三家里有两家承认"拒绝判定是启发式"**:codex 注释原文 "We don't have a fully deterministic way to tell if our command failed because of the sandbox",靠退出码 + 关键词表;DSH 也写"拒绝靠 stderr 方言推断"。

这是一个诚实的已知缺陷:**用 stderr 关键词判断"是不是沙箱拒绝"会把用户程序自己的 `permission denied` 误判成沙箱拒绝**,进而触发不该触发的升级审批。

## 9. 六家对比

| 维度 | codex | pi | DSH | hermes | reasonix | claude-code |
| --- | --- | --- | --- | --- | --- | --- |
| 默认边界 | 平台绑定,默认开 | 无 | workspace-write | 无(local) | enforce | 不启用 |
| 不可用时 | 不静默放行 | 提示后裸跑 | fail-closed | 硬失败 | fail-closed | 看配置 |
| 写边界 | SBPL/ACL/bwrap | 扩展配置 | 挂载白名单 | 容器 | 挂载 + 写根 pin | 配置面 |
| 读边界 | ✅ deny_read | 扩展可选 | ❌ | ❌ | ✅ forbid_read | ✅ denyOnly |
| 网络 | ✅ 三平台断网 | 扩展白名单 | ❌ | 容器内 | 代理 + allowlist | 可配 |
| 资源配额 | ❌(仅超时) | ❌ | ❌ | 容器有 | ❌ | ❌ |
| 审批关系 | 免问 + 显式升级 | 无逐条审批 | 闭表升级 | 互为替代 | 正交 | 互为替代 |

## 10. Gradus 现在在哪,以及差距

我们选的是**另一条路:副本 + 受限令牌**:

- **副本**(`workspace.py`):在 `%TEMP%` 下建 workspace 副本(git worktree 或整树复制),命令与文件工具都绑副本,关闭时按"新增/修改/删除"合回宿主;
- **受限令牌**(`restricted.py`):合成 SID + `CreateRestrictedToken` + `CreateProcessAsUserW`,只给副本与 scratch 授写 ACE;
- **读不限、网络不阻断、无资源配额**;MCP 进程在箱外;Linux 上没有原生边界。

对比上面的表,我们的位置很清楚:

| 项 | 我们 | 与最强者比 |
| --- | --- | --- |
| 写边界 | ✅ Windows | 与 codex 的受限令牌档接近 |
| 回滚能力 | ✅ 副本 + 合并 | **六家里只有我们有**(hermes 靠容器/tmpfs) |
| 读边界 | ❌ | codex/claude 有 |
| 网络 | ❌ | 只有 codex 三平台都有 |
| 资源配额 | ❌ | 六家都没有(容器路线除外) |
| Windows 覆盖 | ✅ | codex 之外最全 |
| macOS/Linux | ❌ | 五家都有 |

**我们真正独有的一件事**:把"改坏了能退回"做成主机制。别家的边界是"不让它改到外面",我们的边界是"让它改,但改在副本里,最后才合并"——代价是路径映射、合并、ACL 这一堆复杂度,收益是**回滚**,而不只是阻止。

## 11. 如果要继续补,按性价比排

| 优先级 | 补什么 | 依据 | 代价 |
| --- | --- | --- | --- |
| ~~P1~~ | ~~Windows 断网(WFP 过滤)~~ → **不做(2026-11 决定)** | 威胁模型里没有"网页注入"入口(我们没有浏览器工具);断网也挡不住"写到工作区再合回宿主"这类外泄;单机单用户 + 审批已在位。给 Windows 加 WFP 会带来规则生命周期与系统策略冲突的长期维护面,收益不抵成本 | — |
| P2 | **读边界**(ACL deny_read,把 `.ssh/.aws/.env` 排除) | codex/claude 都有;我们目前"读不限" | 中(ACL 反向授权比授权更难) |
| P3 | **资源配额**(Job Object 的内存/CPU 限额) | 六家都缺,但 Windows Job Object 现成 | 低—中 |
| P4 | Linux 后端(bwrap) | 我们只在 Windows 有边界 | 高(新平台全链路) |

**明确不做**:Docker 后端真机验证(2026-11 决定)、MCP 进程入箱(见 `sandbox.md` §4.4)、**Windows 断网**(见上表 P1 行:威胁模型里没有对应入口)。

**评测时的约定**:跑 Terminal-Bench / SWE-bench 时,Gradus 在容器里用 `sandbox: local`——容器本身就是边界,不要再叠副本。

---

## 证据索引

- Codex:`codex-rs/protocol/src/sandbox.rs`、`codex-rs/sandboxing/src/{manager,seatbelt,bwrap,denial,violation,linux_pid_namespace}.rs`、`codex-rs/sandboxing/src/*.sbpl`、`codex-rs/linux-sandbox/src/{linux_run_main,bwrap,seccomp}.rs`、`codex-rs/windows-sandbox-rs/src/{setup,process,wfp,no_reparse_dir}.rs`、`codex-rs/network-proxy/src/domain_matcher.rs`
- pi-agent:`pi/packages/coding-agent/src/core/tools/bash.ts`、`examples/extensions/sandbox/index.ts`、`docs/security.md`、`docs/containerization.md`
- DSH:`packages/sandbox/{sandbox,sandbox-local,sandbox-policy,sandbox-windows-acl}/src`、`packages/shell/bash-sandbox`、`packages/bundle/base/cordis.patch.yml`、`docs/subsystems/sandbox.md`
- hermes-agent:`tools/terminal_tool.py`、`tools/environments/docker.py`、`tools/approval.py`、`SECURITY.md`、`plugin-catalog/bubblewrap.yaml`
- deepseek-reasonix:`internal/safety/sandbox/{seatbelt_darwin,seatbelt_other,sandbox}.go`、`internal/safety/permission/permission.go`、`egress/{policy,proxy,address}.go`、`docs/GUIDE.md`、`docs/SPEC.md`
- Claude Code:闭源,证据为 npm 打包产物 `cli.js` 字符串与 `repo/CHANGELOG.md`、`README-hipaa.md`
- Gradus:`gradus/coding/{sandbox,restricted,appcontainer,workspace}.py`、`docs/sandbox.md`

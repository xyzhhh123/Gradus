# Gradus `coding` 层

[English](README.md)

Gradus 的产品层：把 `agent` 的循环装配成能直接用的助手。

## 依赖方向

```
coding/   产品层：配置、会话、工具、权限、入口
   |
agent/    循环、工具运行时、事件
   |
ai/       Provider、内部协议
```

规则：

- `coding` 可以 import `gradus.agent.*`、`gradus.ai.*` 与第三方库。
- `coding` 是唯一读环境变量、持有 API key、管理连接池生命周期的层。
- `coding` 不修改 `agent` / `ai` 的协议；需要新能力时先在下层加口子，不在本层绕过去。

## 职责

负责：

- 读配置与 API key，构造 `ModelSpec` 与 Provider；
- 管理进程级 `httpx.AsyncClient` 与 `Agent` 的生命周期；
- 实现具体工具（文件、搜索、命令），声明 `execution_mode` 与超时；
- 用 `before_tool_call` 做权限拦截，用事件流做日志与终端展示；
- 会话历史的内存保存与落盘；
- 上下文预算、压缩与模型路由（后续）；
- 命令行入口。

不负责：

- 循环、轮次、工具调度、事件形状；
- Provider 协议与流式解析；
- 首字节前的网络重试（`ai` 负责）。

## 目录

```
gradus/coding/
├── README.md
├── README.zh.md
├── coding_plan.md      # 里程碑与偏差记录
├── config.py           # 配置与模型选择：环境变量 -> ModelSpec
├── session.py          # 会话装配：连接池、Provider、Agent 生命周期
├── hooks.py            # 事件订阅与 hook 装配：展示、日志
├── permissions.py      # 工具权限策略
├── cli.py              # 命令行入口
├── tools/
│   ├── files.py        # read_file / write_file / list_dir
│   ├── search.py       # grep / glob
│   └── shell.py        # bash
├── docs/
│   ├── design.md       # 设计说明
│   └── problem.md      # 已解决问题归档
└── tests/
```

没有 `__init__.py`，和 `gradus/ai`、`gradus/agent` 一样使用 namespace package。

## 现状

骨架阶段：只有目录与职责说明，没有实现。里程碑见 [coding_plan.md](coding_plan.md)。

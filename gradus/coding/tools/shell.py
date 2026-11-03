"""命令执行工具：`bash`。

实现 `agent.base.AgentTool`：`spec`、`execution_mode`、`async execute()`。

约束：
- `execution_mode = "serial"`，不与其它工具并发；
- 默认需要权限确认，策略见 `permissions.py`；
- 必须有超时与输出上限；
- 子进程退出码与 stderr 要一起返回，不能只看 stdout。

待实现（M4 之后）。
"""

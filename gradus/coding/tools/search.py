"""搜索工具：`grep`、`glob`。

每个工具实现 `agent.base.AgentTool`：`spec`、`execution_mode`、`async execute()`。

约束：
- 都是只读，可声明 `execution_mode = "parallel"`；
- 结果要限制条数，避免大仓库一次刷满上下文；
- 不跟随符号链接，除非显式允许。

待实现（M1 之后）。
"""

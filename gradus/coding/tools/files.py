"""文件工具：`read_file`、`write_file`、`list_dir`。

每个工具实现 `agent.base.AgentTool`：`spec`、`execution_mode`、`async execute()`。

约束：
- 只读工具（`read_file` / `list_dir`）可声明 `execution_mode = "parallel"`；
- 写操作保持 `"serial"`，避免同批并发写同一文件；
- 输出长度要有上限，超出部分截断并在结果里说明；
- 路径必须解析后再做权限判断，防止绕过。

第一版只做 `read_file` 与 `list_dir`（M1）；`write_file` 在 M4。
"""

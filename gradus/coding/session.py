"""会话装配。

负责：
- 建进程级 `httpx.AsyncClient`，所有用户共用；
- 用 `create_provider(spec, api_key, client=pool)` 建 Provider；
- 用 `Agent.connect(..., client=pool)` 建每个用户的 `Agent`；
- 决定 `aclose()` 的时机：进程退出或会话销毁，不是每次请求结束。

不负责：历史内容、工具实现、权限策略。

约束：连接池的所有权按「谁创建谁关闭」；本模块创建的就由本模块关闭。

待实现（M0）。
"""

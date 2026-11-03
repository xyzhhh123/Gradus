"""事件订阅与 hook 装配。

负责：
- 把 `AgentEvent` 渲染到终端或日志；
- 把 `permissions` 的策略接进 `before_tool_call`；
- 需要时接入 `transform_messages` / `prepare_request` 做压缩与路由。

不负责：事件形状（`agent` 定义）、权限判断本身（`permissions.py`）。

约束：只消费事件，不改事件；hook 里不抛异常，失败要转成明确结果。

待实现（M3）。
"""

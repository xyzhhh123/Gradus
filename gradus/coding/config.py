"""配置与模型选择。

负责：从环境变量或配置文件读 API key、`base_url`、模型名，产出 `ModelSpec`。
不负责：实例化 Provider（`session.py` 用 spec 建），也不碰 `agent`。

约束：本模块只做读入与校验，不做网络请求，不打印。

待实现（M0）。
"""

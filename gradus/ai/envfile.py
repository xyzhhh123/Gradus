"""`.env` 解析：`ai/scripts/record.py` 与 `coding` CLI 共用同一份规则。

规则（沿用 record.py 原来的行为）：

- `KEY=VALUE`、`export KEY=VALUE`、`#` 注释行、空行；
- **文件优先**：同名环境变量会被文件里的值覆盖，让项目自带的配置压过机器上残留的变量；
- 空值视为未配置，跳过，不会把已有环境变量顶成空串；
- 值两侧的引号去掉；文件按 `utf-8-sig` 读，兼容 BOM。

为什么放在 `ai` 层：两个调用方一个在 `ai`（`scripts/record.py`），一个在 `coding`（CLI）；
依赖方向是 `coding -> ai`，所以共用实现只能落在 `ai` 侧——放 `coding` 会让 `ai` 反向依赖。
本模块不碰网络、不碰协议，只是把文件读进 `os.environ`。

不做变量展开、不处理多行值。
"""

from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: str | Path) -> None:
    """把 `.env` 里的键写进 `os.environ`；文件不存在时什么也不做。"""
    file = Path(path).expanduser()
    if not file.is_file():
        return

    try:
        lines = file.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]

        key, separator, value = line.partition("=")
        if not separator:
            continue
        value = value.strip().strip("\"'")
        if value:
            os.environ[key.strip()] = value

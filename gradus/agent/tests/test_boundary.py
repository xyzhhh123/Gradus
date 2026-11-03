"""依赖边界检查。

扫描 `gradus/agent/*.py` 的 import，确保 agent 只看得到更下层：

- ai 路径只允许 `gradus.ai.base`、`gradus.ai.types`，以及工厂 `gradus.ai.providers`
  （不含 `providers.*` 的具体适配器）；
- 第三方只允许 `jsonschema`（`validation.py`）与 `httpx`（`agent.py` 持有连接池）；
- 禁止 `gradus.coding`，以及 `gradus.ai` 下的其它模块（transport / stream_assembler 等）。

`Agent.connect()` 要建 Provider 与连接池，因此放开 `httpx` 与工厂这一条；具体用哪家适配器
仍由工厂按 `ModelSpec.provider` 决定，agent 不 import 任何厂商模块。

测试文件本身不受此限制：`tests/test_loop.py` 需要 `gradus.ai.providers.fake`。
"""

from __future__ import annotations

import ast
import sys
import unittest
from collections.abc import Iterator
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
PACKAGE = "gradus.agent"
ALLOWED_AI_PREFIXES = ("gradus.ai.base", "gradus.ai.types")
ALLOWED_AI_MODULES = ("gradus.ai.providers",)
ALLOWED_THIRD_PARTY = ("jsonschema", "httpx")
FORBIDDEN_PREFIXES = ("gradus.coding",)


def imported_modules(path: Path) -> Iterator[str]:
    """把绝对/相对 import 都还原成绝对模块名。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0:
                yield module
                continue

            parts = PACKAGE.split(".")
            base = parts[: len(parts) - node.level + 1]
            yield ".".join(base + ([module] if module else []))


class TestImportBoundary(unittest.TestCase):
    def test_agent_only_imports_lower_layers(self):
        offenders: list[str] = []

        for path in sorted(AGENT_DIR.glob("*.py")):
            for module in imported_modules(path):
                if module.startswith(FORBIDDEN_PREFIXES):
                    offenders.append(f"{path.name}: {module}")
                    continue

                root = module.split(".", 1)[0]
                if (
                    root
                    and root != "gradus"
                    and root not in ALLOWED_THIRD_PARTY
                    and root not in sys.stdlib_module_names
                ):
                    offenders.append(f"{path.name}: {module}")
                    continue

                if module == "gradus.ai":
                    offenders.append(f"{path.name}: {module}")
                elif module in ALLOWED_AI_MODULES:
                    continue
                elif module.startswith("gradus.ai.") and not module.startswith(
                    ALLOWED_AI_PREFIXES
                ):
                    offenders.append(f"{path.name}: {module}")

        self.assertEqual(offenders, [])

    def test_types_only_depends_on_ai_types(self):
        modules = [
            module
            for module in imported_modules(AGENT_DIR / "types.py")
            if module.startswith("gradus")
        ]

        self.assertEqual(modules, ["gradus.ai.types"])


if __name__ == "__main__":
    unittest.main()

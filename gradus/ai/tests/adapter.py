"""测试总入口：把各测试文件的用例汇总成一个 TestSuite。

分工：每个 test_*.py 只负责测自己的实现；本文件不写断言，也不导入任何
gradus 模块，只收拢用例，让「全部测试」有一个可执行的单一入口。

运行（在 Gradus 目录下）：
    uv run python -m gradus.ai.tests.adapter

新增测试文件时，只需在 MODULES 里追加一行模块路径。
"""

from __future__ import annotations

import unittest

MODULES = (
    "gradus.ai.tests.test_anthropic",
    "gradus.ai.tests.test_fake",
    "gradus.ai.tests.test_gemini",
    "gradus.ai.tests.test_openai_compat",
    "gradus.ai.tests.test_openai_responses",
    "gradus.ai.tests.test_provider_factory",
    "gradus.ai.tests.test_replay",
    "gradus.ai.tests.test_transport",
)


def suite() -> unittest.TestSuite:
    """按 MODULES 的顺序装载用例。

    pytest 也能跑本文件（`uv run pytest gradus/ai/tests/adapter.py -q`），
    得到的是同一批用例。
    """
    return unittest.TestLoader().loadTestsFromNames(MODULES)


def main() -> int:
    result = unittest.TextTestRunner(verbosity=2).run(suite())
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())

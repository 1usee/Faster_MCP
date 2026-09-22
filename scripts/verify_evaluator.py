"""独立的求值器验证脚本（不依赖 pytest，不依赖 mcp 包）。

作用：在**还没有安装任何依赖**的环境里，也能立刻验证安全求值器的行为。
这是刻意设计的一层"零依赖自检"——因为 safe_eval.py 只用了标准库
（ast / math / operator），所以它可以在任何 Python 3.10+ 上跑。

运行：
    python scripts/verify_evaluator.py

与 tests/ 的分工：
  - 本脚本：零依赖快速自检，输出给人看，适合"改完表达式逻辑马上验一下"
  - tests/：全量回归，需要 pytest，适合 CI

为什么值得单独做：安全边界这种东西，最怕的就是"看代码觉得对"。
必须真的把每个逃逸尝试跑一遍，看到它确实被拒绝，才能放心。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from faster_mcp.errors import ToolInputError  # noqa: E402
from faster_mcp.features.calculator.safe_eval import evaluate  # noqa: E402

OK = "  ok  "
BAD = " FAIL "

failures: list[str] = []


def expect_value(expression: str, expected: object) -> None:
    """断言表达式求值结果等于 expected。"""
    try:
        actual = evaluate(expression)
    except Exception as exc:  # noqa: BLE001
        actual = f"<raised {type(exc).__name__}: {exc}>"

    passed = actual == expected
    if not passed:
        failures.append(f"{expression!r}: 期望 {expected!r}，实际 {actual!r}")
    print(f"{OK if passed else BAD} {expression:<46} = {actual!r}")


def expect_rejected(expression: str) -> None:
    """断言表达式被拒绝（抛 ToolInputError）。"""
    try:
        actual = evaluate(expression)
        failures.append(f"{expression!r}: 本应被拒绝，却返回了 {actual!r}")
        print(f"{BAD} {expression:<46} 未拒绝！返回 {actual!r}")
    except ToolInputError as exc:
        print(f"{OK} {expression:<46} 已拒绝 ({str(exc)[:40]}...)")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"{expression!r}: 抛出了非预期的 {type(exc).__name__}")
        print(f"{BAD} {expression:<46} 抛出意外异常 {type(exc).__name__}")


def main() -> int:
    print("=" * 78)
    print("A. 基本算术（必须算对）")
    print("=" * 78)
    expect_value("1 + 1", 2)
    expect_value("(1200 * 1.13) / 4", 339.0)
    expect_value("2 ** 10", 1024)
    expect_value("10 // 3", 3)
    expect_value("10 % 3", 1)
    expect_value("-5 + 3", -2)

    print()
    print("=" * 78)
    print("B. 大整数精度（不能被降级成浮点）")
    print("=" * 78)
    expect_value("99 * 99", 9801)
    expect_value("2 ** 100", 2**100)

    print()
    print("=" * 78)
    print("B2. 浮点精度（不能被\"好看\"吃掉有效数字）")
    print("=" * 78)
    import math

    expect_value("pi", math.pi)
    expect_value("1 / 3", 1 / 3)
    expect_value("2 / 7", 2 / 7)
    expect_value("sin(1)", math.sin(1))
    # 反向：真正的浮点噪声仍要被收敛
    expect_value("0.1 + 0.2", 0.3)
    expect_value("1.1 * 3", 3.3)

    print()
    print("=" * 78)
    print("B3. 布尔不能被当成 1/0")
    print("=" * 78)
    expect_rejected("mean(True, False)")
    expect_rejected("sum(1, True)")

    print()
    print("=" * 78)
    print("C. 函数与常量")
    print("=" * 78)
    expect_value("sqrt(144)", 12.0)
    expect_value("round(sin(radians(30)), 4)", 0.5)
    expect_value("log2(8)", 3.0)
    expect_value("comb(5, 2)", 10)
    expect_value("factorial(5)", 120)
    expect_value("mean(3, 7, 11)", 7.0)
    expect_value("median(1, 3, 2)", 2)
    expect_value("min(3, 1, 2)", 1)

    print()
    print("=" * 78)
    print("D. 变量与多步表达式")
    print("=" * 78)
    expect_value("price = 199; price * 0.85", 199 * 0.85)
    expect_value("a = 2; b = 3; a ** b", 8)

    print()
    print("=" * 78)
    print("E. 安全边界（以下每一条都必须被拒绝）")
    print("=" * 78)
    for expression in [
        "__import__('os')",
        "__import__('os').system('echo pwned')",
        "eval('1+1')",
        "exec('x = 1')",
        "open('C:/Windows/win.ini')",
        "().__class__",
        "().__class__.__bases__",
        "1 .__class__",
        "'abc'.upper()",
        "[1, 2][0]",
        "globals()",
        "locals()",
        "import os",
        "for i in range(3): pass",
        "lambda x: x",
        "[x for x in range(3)]",
        "a.b = 1",
        "a = b = 1",
        "a += 1",
        "'just a string'",
        "print('hi')",
    ]:
        expect_rejected(expression)

    print()
    print("=" * 78)
    print("F. 资源限额（防 DoS）")
    print("=" * 78)
    expect_rejected("2 ** 100000")
    expect_rejected("pow(2, 100000)")
    expect_rejected("factorial(10000)")
    expect_rejected("factorial(-1)")

    print()
    print("=" * 78)
    if failures:
        print(f"FAILED: {len(failures)} 项不符合预期")
        for item in failures:
            print(f"  - {item}")
        return 1

    print("全部通过。求值器的安全边界与计算正确性均符合预期。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

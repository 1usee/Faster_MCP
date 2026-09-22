"""safe_eval 的测试：只测求值器，纯函数、无外部依赖。

这一层的测试应当最密，因为安全边界全在这里。凡是"我们**拒绝**执行什么"
的用例，价值和"我们支持什么"的用例一样高——甚至更高。
"""

from __future__ import annotations

import math

import pytest

from faster_mcp.errors import ResourceLimitError, ToolInputError
from faster_mcp.features.calculator.safe_eval import (
    MAX_EXPRESSION_LENGTH,
    MAX_FACTORIAL_INPUT,
    MAX_POWER_EXPONENT,
    evaluate,
)


# --------------------------------------------------------------------------- #
# 基本算术
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "expression, expected",
    [
        ("1 + 1", 2),
        ("(1200 * 1.13) / 4", 339.0),
        ("2 ** 10", 1024),
        ("2 ** 10 + sqrt(144)", 1036.0),
        ("10 // 3", 3),
        ("10 % 3", 1),
        ("-5 + 3", -2),
        ("+7", 7),
        ("min(3, 1, 2)", 1),
        ("max(3, 1, 2)", 3),
        ("abs(-42)", 42),
        ("round(3.14159, 2)", 3.14),
        ("floor(3.7)", 3),
        ("ceil(3.2)", 4),
    ],
)
def test_arithmetic(expression: str, expected: float) -> None:
    assert evaluate(expression) == expected


def test_integer_precision_is_preserved() -> None:
    """整数运算必须保持精确，不能悄悄退化成浮点。

    为什么单独测：模型做"大数乘法"正是为了精确。如果 2**100 变成浮点近似，
    这个工具就失去了存在意义。所以这条断言直接对准工具的核心价值。
    """
    assert evaluate("2 ** 100") == 2**100
    assert isinstance(evaluate("2 ** 100"), int)
    assert evaluate("99 * 99") == 9801


def test_float_result_when_needed() -> None:
    assert evaluate("1 / 3") == pytest.approx(1 / 3)
    assert isinstance(evaluate("1 / 3"), float)


# --------------------------------------------------------------------------- #
# 函数与常量
# --------------------------------------------------------------------------- #
def test_constants() -> None:
    assert evaluate("pi") == pytest.approx(math.pi)
    assert evaluate("tau") == pytest.approx(math.tau)
    assert evaluate("e") == pytest.approx(math.e)


def test_trig_with_radians() -> None:
    assert evaluate("sin(radians(30))") == pytest.approx(0.5)
    assert evaluate("cos(0)") == 1.0


def test_log_family() -> None:
    assert evaluate("log(e)") == pytest.approx(1.0)
    assert evaluate("log2(8)") == pytest.approx(3.0)
    assert evaluate("log10(1000)") == pytest.approx(3.0)
    assert evaluate("log(8, 2)") == pytest.approx(3.0)


def test_combinatorics() -> None:
    assert evaluate("factorial(5)") == 120
    assert evaluate("comb(5, 2)") == 10
    assert evaluate("perm(5, 2)") == 20
    assert evaluate("gcd(12, 18)") == 6
    assert evaluate("lcm(4, 6)") == 12
    assert evaluate("isqrt(17)") == 4


def test_statistics_functions_accept_both_styles() -> None:
    """统计函数应同时接受可变参数和列表，对模型更宽容。"""
    assert evaluate("mean(1, 2, 3)") == pytest.approx(2.0)
    assert evaluate("mean([1, 2, 3])") == pytest.approx(2.0)
    assert evaluate("median(1, 3, 2)") == pytest.approx(2.0)
    assert evaluate("median(1, 2, 3, 4)") == pytest.approx(2.5)
    assert evaluate("stdev(2, 4, 4, 4, 5, 5, 7, 9)") == pytest.approx(2.138, abs=1e-3)


# --------------------------------------------------------------------------- #
# 变量与多步表达式
# --------------------------------------------------------------------------- #
def test_variable_definition_and_reuse() -> None:
    assert evaluate("price = 199; price * 0.85") == pytest.approx(169.15)


def test_multiple_statements() -> None:
    assert evaluate("a = 2; b = 3; a ** b") == 8


def test_preset_variables() -> None:
    assert evaluate("x * 2", {"x": 21}) == 42


def test_expression_variable_overrides_preset() -> None:
    assert evaluate("x = 5; x", {"x": 1}) == 5


def test_last_statement_is_result() -> None:
    assert evaluate("1 + 1; 2 + 2; 3 + 3") == 6


# --------------------------------------------------------------------------- #
# 比较与布尔
# --------------------------------------------------------------------------- #
def test_comparison() -> None:
    assert evaluate("3 > 2") is True
    assert evaluate("2 > 3") is False
    assert evaluate("1 < 2 < 3") is True
    assert evaluate("1 < 5 < 3") is False


def test_boolean_ops() -> None:
    assert evaluate("True and False") is False
    assert evaluate("True or False") is True
    assert evaluate("not False") is True


def test_conditional_expression() -> None:
    assert evaluate("10 if 3 > 2 else 20") == 10
    assert evaluate("10 if 2 > 3 else 20") == 20


# --------------------------------------------------------------------------- #
# 安全边界：以下必须全部被拒绝
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "expression",
    [
        # 导入与执行
        "__import__('os')",
        "__import__('os').system('echo hi')",
        "eval('1+1')",
        "exec('x=1')",
        "compile('1', '<s>', 'eval')",
        # 属性访问：逃逸沙箱的经典路径
        "().__class__",
        "1 .__class__",
        "'abc'.upper()",
        "(1).__class__.__bases__",
        # 下标访问
        "[1,2][0]",
        # 字符串逃逸
        "'__import__'",
        # 未定义名称
        "open('/etc/passwd')",
        "os.system('ls')",
        "globals()",
        "locals()",
        # 语句类
        "import os",
        "for i in range(3): pass",
        "while True: pass",
        "def f(): pass",
        "lambda x: x",
        "[x for x in range(3)]",
        "{k: 1 for k in range(3)}",
        # 多目标/属性赋值
        "a = b = 1",
        "a.b = 1",
        "a[0] = 1",
        "a += 1",
    ],
)
def test_dangerous_expressions_are_rejected(expression: str) -> None:
    """一切非数学构造都必须抛 ToolInputError，绝不能执行。"""
    with pytest.raises(ToolInputError):
        evaluate(expression)


def test_dunder_name_rejected_even_if_defined() -> None:
    """即使变量作用域里没有它，双下划线名称也应当给出明确提示而非静默放行。"""
    with pytest.raises(ToolInputError, match="未定义"):
        evaluate("__builtins__")


# --------------------------------------------------------------------------- #
# 资源限额
# --------------------------------------------------------------------------- #
def test_power_exponent_limit() -> None:
    """经典的 DoS 向量：9**9**9 足以吃光内存。"""
    with pytest.raises(ResourceLimitError):
        evaluate(f"2 ** {MAX_POWER_EXPONENT + 1}")


def test_power_limit_applies_to_pow_function() -> None:
    """函数形式与运算符形式必须共用同一道限额，否则会留下绕过口子。"""
    with pytest.raises(ResourceLimitError):
        evaluate(f"pow(2, {MAX_POWER_EXPONENT + 1})")


def test_factorial_limit() -> None:
    with pytest.raises(ResourceLimitError):
        evaluate(f"factorial({MAX_FACTORIAL_INPUT + 1})")


def test_factorial_negative_rejected() -> None:
    with pytest.raises(ToolInputError):
        evaluate("factorial(-1)")


def test_expression_length_limit() -> None:
    with pytest.raises(ResourceLimitError):
        evaluate("1 + " * (MAX_EXPRESSION_LENGTH) + "1")


# --------------------------------------------------------------------------- #
# 输入健壮性
# --------------------------------------------------------------------------- #
def test_empty_expression() -> None:
    with pytest.raises(ToolInputError):
        evaluate("   ")


def test_non_string_expression() -> None:
    with pytest.raises(ToolInputError):
        evaluate(123)  # type: ignore[arg-type]


def test_syntax_error_has_helpful_message() -> None:
    with pytest.raises(ToolInputError) as excinfo:
        evaluate("1 +* 2")
    # 错误信息应当包含"怎么改"的线索，而不是只丢一个 SyntaxError
    assert "表达式" in str(excinfo.value)


def test_division_by_zero_message() -> None:
    with pytest.raises(ToolInputError, match="除数为零"):
        evaluate("1 / 0")


def test_unknown_function_lists_available() -> None:
    """未知函数的报错应列出可用函数，模型据此可自行纠正。"""
    with pytest.raises(ToolInputError) as excinfo:
        evaluate("foo(1)")
    assert "sqrt" in str(excinfo.value)


def test_unknown_name_hint() -> None:
    with pytest.raises(ToolInputError) as excinfo:
        evaluate("xyz + 1")
    assert "未定义的名称" in str(excinfo.value)


def test_sqrt_of_negative_gives_clear_error() -> None:
    with pytest.raises(ToolInputError, match="负数"):
        evaluate("sqrt(-1)")


def test_reserved_variable_name_rejected() -> None:
    with pytest.raises(ToolInputError):
        evaluate("pi = 3")

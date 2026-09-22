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
    assert evaluate("1 / 3") == 1 / 3
    assert isinstance(evaluate("1 / 3"), float)


# --------------------------------------------------------------------------- #
# 浮点精度："精确计算"是本工具的核心卖点，必须用精确断言守住
# --------------------------------------------------------------------------- #
def test_constants_keep_full_precision() -> None:
    """常量必须是**精确相等**，不能只满足 1e-6 的近似。

    背景：早期实现把浮点结果统一 round(x, 12)，把 pi 从
    3.141592653589793 削成 3.14159265359。而原来的测试用
    pytest.approx（相对容差 1e-6）竟然能过——因为 12 位偏差远小于容差。
    这里改用 `==`，让任何精度损失都立即失败。
    """
    assert evaluate("pi") == math.pi
    assert evaluate("tau") == math.tau
    assert evaluate("e") == math.e


def test_repeating_decimal_keeps_full_precision() -> None:
    """1/3 是无限循环小数，不能因为"四舍五入好看"就损失有效数字。"""
    assert evaluate("1 / 3") == 1 / 3
    assert evaluate("2 / 7") == 2 / 7


def test_float_noise_is_still_cleaned() -> None:
    """保留精度的同时，常见的浮点噪声仍要被收敛（这是该机制的存在理由）。"""
    assert evaluate("0.1 + 0.2") == 0.3
    assert evaluate("0.1 + 0.7") == 0.8
    assert evaluate("1.1 * 3") == 3.3
    assert evaluate("0.3 - 0.1") == 0.2


def test_exact_float_results_are_not_rounded() -> None:
    """能精确表示的浮点结果不应该被改动。"""
    assert evaluate("2.5 + 2.5") == 5.0
    assert evaluate("1 / 4") == 0.25
    assert evaluate("1 / 8") == 0.125


def test_trig_keeps_precision() -> None:
    """三角函数的输出全部是无限小数，不能截断。"""
    assert evaluate("sin(1)") == math.sin(1)
    assert evaluate("cos(1)") == math.cos(1)


# --------------------------------------------------------------------------- #
# 能力清单与实现必须同步
# --------------------------------------------------------------------------- #
def test_describe_capabilities_lists_every_safe_function() -> None:
    """describe_capabilities() 的函数清单是手写的，可能与 SAFE_FUNCTIONS 漂移。

    漂移的后果：新加了函数但忘了写进清单，模型查询能力时看不到它，
    于是不敢用——这个"先查后用"的工具就白做了。
    这里用断言把"两份数据必须一致"变成机器可检查的约束。
    """
    from faster_mcp.features.calculator.safe_eval import (
        SAFE_FUNCTIONS,
        describe_capabilities,
    )

    caps = describe_capabilities()
    listed: set[str] = set()
    for names in caps["functions"].values():
        listed.update(names)

    missing = set(SAFE_FUNCTIONS) - listed
    assert not missing, f"以下函数在 SAFE_FUNCTIONS 里但未写进能力清单：{sorted(missing)}"

    extra = listed - set(SAFE_FUNCTIONS)
    assert not extra, f"以下函数写进了能力清单但实现里不存在：{sorted(extra)}"


def test_describe_capabilities_constants_match() -> None:
    """清单里的常量也必须与 SAFE_CONSTANTS 一致。"""
    from faster_mcp.features.calculator.safe_eval import (
        SAFE_CONSTANTS,
        describe_capabilities,
    )

    assert set(describe_capabilities()["constants"]) == set(SAFE_CONSTANTS)


# --------------------------------------------------------------------------- #
# 函数与常量
# --------------------------------------------------------------------------- #
def test_constants() -> None:
    """常量的存在性与具体值（精确断言在 test_constants_keep_full_precision）。"""
    assert evaluate("pi") == math.pi
    assert evaluate("tau") == math.tau
    assert evaluate("e") == math.e


def test_trig_with_radians() -> None:
    assert evaluate("sin(radians(30))") == pytest.approx(0.5)
    assert evaluate("cos(0)") == 1.0


def test_log_family() -> None:
    # 对数底层走 C 库 math.log，不同平台末位可能差 1 ulp，
    # 这不是本项目的精度问题，保留 approx 以免测试变成平台相关的。
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
    assert evaluate("mean(1, 2, 3)") == 2.0
    assert evaluate("mean([1, 2, 3])") == 2.0
    assert evaluate("median(1, 3, 2)") == 2.0
    assert evaluate("median(1, 2, 3, 4)") == 2.5
    assert evaluate("stdev(2, 4, 4, 4, 5, 5, 7, 9)") == pytest.approx(2.138, abs=1e-3)


def test_statistics_reject_booleans() -> None:
    """布尔不能被静默当成 1/0。

    为什么单独提：Python 里 `True` 是 `int` 的子类，`float(True) == 1.0`。
    若静默放行，模型写 `mean(True, False)` 会得到一个看似合理的 0.5，
    把"传错了参数"伪装成"算对了"。convert() 的入参校验排除 bool，
    这里必须保持一致。
    """
    with pytest.raises(ToolInputError):
        evaluate("mean(True, False)")
    with pytest.raises(ToolInputError):
        evaluate("sum(1, True)")
    with pytest.raises(ToolInputError):
        evaluate("mean([True, 1])")


# --------------------------------------------------------------------------- #
# 变量与多步表达式
# --------------------------------------------------------------------------- #
def test_variable_definition_and_reuse() -> None:
    # 精度保留后，169.15 这类有限小数可以精确相等
    assert evaluate("price = 199; price * 0.85") == 169.15


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

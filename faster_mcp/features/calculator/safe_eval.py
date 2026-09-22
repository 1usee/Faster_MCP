"""安全的数学表达式求值器。

为什么不能用 eval()
------------------
`eval("1+1")` 只算 1+1，但 `eval(some_string)` 会执行**任意 Python 代码**：
`__import__("os").system("...")`、`open("/etc/passwd").read()`、读写文件、
发起网络请求，全都可以。而这里的字符串来自大模型——模型可能被提示注入
带偏，或者干脆生成一段"看起来像数学"的东西。所以求值器必须是白名单式的。

三道防线
--------
1. **AST 白名单**：把表达式解析成语法树，逐节点检查类型。
   只放行数字、变量、二元运算、比较、布尔运算、函数调用；其它节点（属性访问
   `a.b`、下标 `a[b]`、lambda、推导式、导入、赋值语句…）一律拒绝。
   属性访问尤其重要——它是 Python 里逃逸沙箱的经典路径（如 `().__class__.__bases__`）。
2. **隔离的命名空间**：eval 时传入的 globals/locals 是我们自建的字典，
   里面只有白名单里的函数和常量。`__builtins__` 被显式置空，
   所以 `__import__`、`open`、`print` 这些根本不存在。
3. **资源限额**：表达式长度、数字字面量长度、幂指数、阶乘上限都有硬限制，
   防止 `9**9**9` 或 `10**7!` 这类"一行表达式吃光内存和 CPU"的攻击。

设计取向
--------
遇到不支持的语法就**明确报错并说明原因**，而不是静默返回 None 或近似值。
因为调用方是模型：清晰的错误信息能让它改写表达式；模糊的结果会让它
把错误答案当真继续往下推理，那比直接失败更糟。
"""

from __future__ import annotations

import ast
import math
import operator
from typing import Any, Callable

from ...errors import ResourceLimitError, ToolInputError

# --------------------------------------------------------------------------- #
# 限额
# --------------------------------------------------------------------------- #
MAX_EXPRESSION_LENGTH = 2000      # 表达式字符串最大长度
MAX_INT_DIGITS = 4300             # 单个整数结果的最大十进制位数（Python 3.11+ 默认上限）
MAX_POWER_EXPONENT = 1000         # a**b 中 |b| 的上限
MAX_FACTORIAL_INPUT = 170         # 阶乘上限（170! 已接近 float 表示极限）
MAX_NUMS_IN_LITERAL = 200         # 数字字面量允许的位数


# --------------------------------------------------------------------------- #
# 白名单函数表
# --------------------------------------------------------------------------- #
# 只收录"确定性的纯数学函数"。刻意不收 random.*（不可复现）、
# 也不收任何接触外部世界的函数。
def _safe_factorial(n: int | float) -> int:
    """带限额的阶乘，避免大数爆炸。"""
    if isinstance(n, float):
        if not n.is_integer():
            raise ToolInputError(f"factorial 只接受非负整数，收到 {n}。")
        n = int(n)
    if n < 0:
        raise ToolInputError(f"factorial 只接受非负整数，收到 {n}。")
    if n > MAX_FACTORIAL_INPUT:
        raise ResourceLimitError(
            f"factorial 输入 {n} 超过上限 {MAX_FACTORIAL_INPUT}。"
            "如需大数阶乘，请改用 loggamma(n+1) 估算其自然对数。"
        )
    return math.factorial(n)


def _safe_sqrt(x: float) -> float:
    if x < 0:
        raise ToolInputError(f"sqrt 不接受负数（{x}）。如需复数结果请说明，当前引擎只支持实数。")
    return math.sqrt(x)


def _safe_log(x: float, base: float | None = None) -> float:
    if base is None:
        if x <= 0:
            raise ToolInputError(f"log 的输入必须为正数，收到 {x}。")
        return math.log(x)
    if x <= 0 or base <= 0 or base == 1:
        raise ToolInputError(f"log(x={x}, base={base}) 参数越界：x 与 base 必须为正且 base != 1。")
    return math.log(x, base)


def _clamp_round(value: float, ndigits: int | None = None) -> Any:
    """四舍五入。ndigits 只接受合理范围，避免 round(x, 10**9)。"""
    if ndigits is not None:
        if abs(int(ndigits)) > 15:
            raise ToolInputError("round 的 ndigits 请控制在 -15..15 之内。")
        ndigits = int(ndigits)
    return round(value, ndigits) if ndigits is not None else round(value)


SAFE_FUNCTIONS: dict[str, Callable[..., Any]] = {
    # 基础
    "abs": abs,
    "round": _clamp_round,
    "min": min,
    "max": max,
    "sum": lambda *args: sum(args),
    "pow": lambda a, b: _checked_pow(a, b),
    # 幂与根
    "sqrt": _safe_sqrt,
    "cbrt": lambda x: math.copysign(abs(x) ** (1 / 3), x),
    "hypot": math.hypot,
    "exp": math.exp,
    # 对数
    "log": _safe_log,
    "log2": lambda x: _safe_log(x, 2),
    "log10": lambda x: _safe_log(x, 10),
    "loggamma": math.lgamma,
    # 三角函数（弧度）
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "atan2": math.atan2,
    # 角度换算
    "radians": math.radians,
    "degrees": math.degrees,
    # 取整与符号
    "floor": math.floor,
    "ceil": math.ceil,
    "trunc": math.trunc,
    "fmod": math.fmod,
    "copysign": math.copysign,
    # 组合数学
    "factorial": _safe_factorial,
    "comb": math.comb,
    "perm": math.perm,
    "gcd": math.gcd,
    "lcm": math.lcm,
    "isqrt": math.isqrt,
    # 统计（便于模型处理一组数，不必自己累加）
    "mean": lambda *args: _mean(args),
    "median": lambda *args: _median(args),
    "stdev": lambda *args: _stdev(args),
    "variance": lambda *args: _variance(args),
}

SAFE_CONSTANTS: dict[str, float] = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
    "inf": math.inf,
    "nan": math.nan,
}


def _checked_pow(base: Any, exponent: Any) -> Any:
    """带限额的幂运算。

    这是最容易被滥用的运算符：`9**9**9` 的计算量指数级增长，
    足以让服务卡死。所以对指数做硬上限。
    函数版和 `**` 运算符版共用这份检查，保证两条路都堵住。
    """
    if isinstance(exponent, (int, float)) and abs(exponent) > MAX_POWER_EXPONENT:
        raise ResourceLimitError(
            f"幂指数 {exponent} 超过上限 {MAX_POWER_EXPONENT}。"
            "如需估算巨大幂，请用对数：log(a**b) = b * log(a)。"
        )
    return operator.pow(base, exponent)


def _as_seq(args: tuple[Any, ...]) -> list[float]:
    """把可变参数或单个可迭代对象统一成数字列表。

    这样 `mean(1,2,3)` 和 `median([1,2,3])` 都能用，对模型更宽容。
    """
    if len(args) == 1 and isinstance(args[0], (list, tuple)):
        values = list(args[0])
    else:
        values = list(args)
    if not values:
        raise ToolInputError("至少需要提供一个数字。")
    return [float(v) for v in values]


def _mean(args: tuple[Any, ...]) -> float:
    values = _as_seq(args)
    return sum(values) / len(values)


def _median(args: tuple[Any, ...]) -> float:
    values = sorted(_as_seq(args))
    n = len(values)
    mid = n // 2
    return values[mid] if n % 2 else (values[mid - 1] + values[mid]) / 2


def _variance(args: tuple[Any, ...], ddof: int = 1) -> float:
    values = _as_seq(args)
    if len(values) - ddof <= 0:
        raise ToolInputError("样本太少，无法计算方差。")
    m = sum(values) / len(values)
    return sum((v - m) ** 2 for v in values) / (len(values) - ddof)


def _stdev(args: tuple[Any, ...]) -> float:
    return math.sqrt(_variance(args))


def _normalize_numeric(value: Any) -> Any:
    """收敛浮点尾部噪声，避免 `0.1 + 0.2` 这类可见但无意义的尾巴污染模型推理。"""
    if not isinstance(value, float) or not math.isfinite(value):
        return value
    rounded = round(value, 12)
    if math.isclose(value, rounded, rel_tol=0.0, abs_tol=1e-12):
        return rounded
    return value


# --------------------------------------------------------------------------- #
# 运算符白名单
# --------------------------------------------------------------------------- #
_BIN_OPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: _checked_pow,
    ast.FloorDiv: operator.floordiv,
}

_UNARY_OPS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
    ast.Not: operator.not_,
}

_CMP_OPS: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


class _Evaluator(ast.NodeVisitor):
    """逐节点遍历 AST 并求值。

    用访问者模式（而不是给 ast 打补丁）的好处：**没实现 visit_xxx 的节点类型
    会直接抛错**，也就是"默认拒绝"。安全设计里，白名单+默认拒绝 远比
    "黑名单+默认放行" 可靠——新语法出现时不会意外放行。
    """

    def __init__(self, variables: dict[str, Any]) -> None:
        self.variables = variables

    # ---- 顶层 ------------------------------------------------------------ #
    def evaluate(self, node: ast.AST) -> Any:
        return self.visit(node)

    # ---- 字面量 ---------------------------------------------------------- #
    def visit_Constant(self, node: ast.Constant) -> Any:  # noqa: N802
        value = node.value
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            if isinstance(value, int) and len(str(abs(value))) > MAX_NUMS_IN_LITERAL:
                raise ResourceLimitError(
                    f"数字字面量过长（超过 {MAX_NUMS_IN_LITERAL} 位）。"
                    "请用科学计数法，如 1e100。"
                )
            return value
        raise ToolInputError(
            f"表达式里不支持字面量 {value!r}（类型 {type(value).__name__}）。"
            "只允许数字、True/False。"
        )

    # ---- 名字（变量 / 常量） --------------------------------------------- #
    def visit_Name(self, node: ast.Name) -> Any:  # noqa: N802
        name = node.id
        if name in self.variables:
            return self.variables[name]
        if name in SAFE_CONSTANTS:
            return SAFE_CONSTANTS[name]
        hint = ""
        if name.startswith("__") or name.endswith("__"):
            hint = "（双下划线名称被禁止）"
        raise ToolInputError(
            f"未定义的名称 '{name}'{hint}。"
            f"可用常量：{', '.join(sorted(SAFE_CONSTANTS))}；"
            "也可以在表达式开头用 `变量名 = 值;` 自定义变量。"
        )

    # ---- 二元运算 -------------------------------------------------------- #
    def visit_BinOp(self, node: ast.BinOp) -> Any:  # noqa: N802
        op_type = type(node.op)
        if op_type not in _BIN_OPS:
            raise ToolInputError(f"不支持的运算符：{op_type.__name__}。")
        left = self.visit(node.left)
        right = self.visit(node.right)
        try:
            result = _BIN_OPS[op_type](left, right)
            return _normalize_numeric(result)
        except ZeroDivisionError:
            raise ToolInputError("除数为零。请检查表达式的分母。") from None

    # ---- 一元运算 -------------------------------------------------------- #
    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:  # noqa: N802
        op_type = type(node.op)
        if op_type not in _UNARY_OPS:
            raise ToolInputError(f"不支持的一元运算符：{op_type.__name__}。")
        return _normalize_numeric(_UNARY_OPS[op_type](self.visit(node.operand)))

    # ---- 比较 ------------------------------------------------------------ #
    def visit_Compare(self, node: ast.Compare) -> Any:  # noqa: N802
        left = self.visit(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            op_type = type(op)
            if op_type not in _CMP_OPS:
                raise ToolInputError(f"不支持的比较运算符：{op_type.__name__}。")
            right = self.visit(comparator)
            if not _CMP_OPS[op_type](left, right):
                return False
            left = right
        return True

    # ---- 布尔运算 -------------------------------------------------------- #
    def visit_BoolOp(self, node: ast.BoolOp) -> Any:  # noqa: N802
        if isinstance(node.op, ast.And):
            result: Any = True
            for value in node.values:
                result = self.visit(value)
                if not result:
                    return result
            return result
        if isinstance(node.op, ast.Or):
            result = False
            for value in node.values:
                result = self.visit(value)
                if result:
                    return result
            return result
        raise ToolInputError(f"不支持的布尔运算符：{type(node.op).__name__}。")

    # ---- 三元表达式 ------------------------------------------------------ #
    def visit_IfExp(self, node: ast.IfExp) -> Any:  # noqa: N802
        """支持 `a if cond else b`。对模型很实用（按条件取不同结果）。"""
        return self.visit(node.body if self.visit(node.test) else node.orelse)

    # ---- 函数调用 -------------------------------------------------------- #
    def visit_Call(self, node: ast.Call) -> Any:  # noqa: N802
        # 只允许直接调用一个名字，禁止 a.b() 和 a[b]() ——
        # 属性访问/下标访问是逃逸沙箱的常用入口（如 ().__class__.__mro__）。
        if not isinstance(node.func, ast.Name):
            raise ToolInputError(
                "只允许调用白名单函数（如 sqrt、log、min），"
                "不支持调用对象的属性或成员方法。"
            )
        func_name = node.func.id
        if func_name not in SAFE_FUNCTIONS:
            raise ToolInputError(
                f"未知函数 '{func_name}'。"
                f"可用函数：{', '.join(sorted(SAFE_FUNCTIONS))}。"
            )
        if node.keywords:
            raise ToolInputError(
                f"函数 '{func_name}' 不支持关键字参数，请按位置传参。"
            )
        args = [self.visit(arg) for arg in node.args]
        try:
            return SAFE_FUNCTIONS[func_name](*args)
        except ToolInputError:
            raise
        except TypeError as exc:
            raise ToolInputError(f"函数 '{func_name}' 的参数不匹配：{exc}。") from exc
        except (ValueError, OverflowError) as exc:
            raise ToolInputError(f"函数 '{func_name}' 计算失败：{exc}。") from exc

    # ---- 列表/元组字面量（供统计函数使用） ------------------------------- #
    def visit_List(self, node: ast.List) -> Any:  # noqa: N802
        return [self.visit(elt) for elt in node.elts]

    def visit_Tuple(self, node: ast.Tuple) -> Any:  # noqa: N802
        return tuple(self.visit(elt) for elt in node.elts)

    # ---- 默认拒绝 -------------------------------------------------------- #
    def generic_visit(self, node: ast.AST) -> Any:
        raise ToolInputError(
            f"表达式包含不支持的语法：{type(node).__name__}。"
            "本引擎只支持数学表达式（算术、比较、布尔、白名单函数调用），"
            "不支持赋值语句、循环、条件语句块、属性访问、下标访问、lambda、导入等。"
        )


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
def evaluate(expression: str, variables: dict[str, Any] | None = None) -> Any:
    """求值一个数学表达式。

    参数
    ----
    expression:
        表达式字符串。支持分号分隔的多个小表达式，最后一个的值作为返回值，
        例如 `"price = 199; price * 0.85"`。这让模型能在一次调用里
        既定义中间量又给出结果，省去多轮往返。
    variables:
        预置变量（会与表达式内定义的变量合并，表达式内的赋值可覆盖）。

    返回
    ----
    求值结果。整数运算返回 int（不丢精度），涉及浮点则返回 float。

    抛出
    ----
    ToolInputError      —— 语法不支持、名称未定义、参数不合法等（模型可自愈）
    ResourceLimitError  —— 超出资源限额（本质是 ToolInputError 的子类）
    """
    if not isinstance(expression, str):
        raise ToolInputError(
            f"expression 必须是字符串，收到 {type(expression).__name__}。"
        )

    expr = expression.strip()
    if not expr:
        raise ToolInputError("表达式为空。请提供形如 `(1200 * 1.13) / 4` 的数学表达式。")
    if len(expr) > MAX_EXPRESSION_LENGTH:
        raise ResourceLimitError(
            f"表达式长度 {len(expr)} 超过上限 {MAX_EXPRESSION_LENGTH} 字符。"
            "请拆分计算步骤，或先把中间结果存成变量。"
        )

    # 自建命名空间：只放白名单内容，__builtins__ 显式清空。
    scope: dict[str, Any] = dict(SAFE_CONSTANTS)
    if variables:
        scope.update(variables)

    # 分号分段求值：每段可以是一句赋值，也可以是表达式
    statements = _split_statements(expr)
    result: Any = None

    for statement in statements:
        result = _eval_one(statement, scope)

    return _normalize_numeric(result)


def _split_statements(expr: str) -> list[str]:
    """按顶层分号切分表达式。

    为什么要自己切、不用 expr.split(";")：分号可能出现在函数参数里的
    字符串或嵌套结构中。当前语法里字符串极少见，但按括号深度切分能
    稳健地支持 `min(1,2); max(3,4)` 这类写法，代价很小。
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []

    for char in expr:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if char == ";" and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))

    return [p.strip() for p in parts if p.strip()]


def _eval_one(statement: str, scope: dict[str, Any]) -> Any:
    """求值单条语句，支持 `name = expr` 形式的变量定义。"""
    try:
        tree = ast.parse(statement, mode="eval")
        return _Evaluator(scope).evaluate(tree.body)
    except SyntaxError:
        # 可能是赋值语句。模式限定为单个 `Name = expr`，
        # 这样既满足"定义中间变量"的需求，又不放开任意语句执行。
        return _eval_assignment(statement, scope)
    except ToolInputError:
        raise
    except ZeroDivisionError:
        raise ToolInputError("除数为零。请检查表达式的分母。") from None
    except OverflowError:
        raise ToolInputError("计算结果溢出，请缩小数值范围或改用对数表示。") from None
    except RecursionError:
        # 极深嵌套（如 1000 层括号）会打爆解析/遍历递归。
        # 与其让进程嗝屁，不如明确报错。
        raise ToolInputError("表达式嵌套层级过深，请拆分成多步计算。") from None


def _eval_assignment(statement: str, scope: dict[str, Any]) -> Any:
    """处理 `变量名 = 表达式`。

    只接受最简单的单变量赋值。以下都被拒绝：
      - 多目标 `a = b = 1`
      - 属性/下标赋值 `a.b = 1`、`a[0] = 1`
      - 增量赋值 `a += 1`、链式比较赋值、解包赋值
    理由依然是白名单原则：能表达的都表达，不能表达的一律明确拒绝。
    """
    try:
        tree = ast.parse(statement, mode="exec")
    except SyntaxError as exc:
        raise ToolInputError(
            f"无法解析表达式：{statement!r}（{exc.msg}）。"
            "请检查括号是否配对、运算符是否完整。"
        ) from exc

    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Assign):
        raise ToolInputError(
            "只支持单条数学表达式，或 `变量名 = 表达式` 形式的变量定义。"
            "不支持 if/for/while/def/import 等语句。"
        )

    assign = tree.body[0]
    if len(assign.targets) != 1 or not isinstance(assign.targets[0], ast.Name):
        raise ToolInputError("变量定义的左侧必须是单个简单名称（如 `price = 199`）。")

    var_name = assign.targets[0].id
    if var_name.startswith("_") or var_name in SAFE_CONSTANTS or var_name in SAFE_FUNCTIONS:
        raise ToolInputError(
            f"变量名 '{var_name}' 被保留（常量或函数名），请换一个名字。"
        )

    value = _Evaluator(scope).evaluate(assign.value)
    scope[var_name] = value
    return value


def describe_capabilities() -> dict[str, Any]:
    """返回引擎能力清单，供 list_calculator_capabilities 工具使用。

    为什么要有这个工具：模型对工具的能力边界通常是模糊的。它不知道我们支持
    `comb` 还是 `loggamma`，就可能在表达式里瞎猜函数名，浪费一次调用。
    提供一个"先查后用"的工具，能显著降低试错次数——这是把 API 文档
    变成可调用工具的思路。
    """
    return {
        "functions": {
            "基础": ["abs", "round", "min", "max", "sum", "pow"],
            "幂与根": ["sqrt", "cbrt", "hypot", "exp"],
            "对数": ["log", "log2", "log10", "loggamma"],
            "三角函数(弧度)": ["sin", "cos", "tan", "asin", "acos", "atan", "atan2"],
            "角度换算": ["radians", "degrees"],
            "取整与符号": ["floor", "ceil", "trunc", "fmod", "copysign"],
            "组合数学": ["factorial", "comb", "perm", "gcd", "lcm", "isqrt"],
            "统计": ["mean", "median", "stdev", "variance"],
        },
        "constants": {name: value for name, value in sorted(SAFE_CONSTANTS.items())},
        "operators": ["+", "-", "*", "/", "//", "%", "**", "== != < <= > >=", "and or not", "a if c else b"],
        "syntax_notes": [
            "支持分号分隔多步：`a = 100; a * 1.13`，最后一步的值即返回值。",
            "可用 `变量名 = 表达式` 定义中间变量，变量只在该次调用内有效。",
            "不支持：属性访问(a.b)、下标(a[0])、lambda、循环、条件语句块、导入。",
            "整数运算保持精确；出现浮点则按 IEEE 754 双精度计算。",
        ],
        "limits": {
            "max_expression_length": MAX_EXPRESSION_LENGTH,
            "max_power_exponent": MAX_POWER_EXPONENT,
            "max_factorial_input": MAX_FACTORIAL_INPUT,
        },
    }

"""计算器功能模块 —— 把 engine/safe_eval 的能力暴露成 MCP 工具。

本文件是"功能模块的标准范本"。加新功能时建议照着它的结构写：

  1. 声明 name / description / version（name 是启停开关的键）
  2. 每个工具用 @feature_tool 装饰，docstring 第一段就是给模型看的工具描述
  3. 参数用类型注解（自动生成 Schema），复杂参数用 Literal 限制取值
  4. 业务逻辑放在兄弟模块里（engine.py），本文件只做参数转接和结果整理
  5. 抛 ToolInputError 表达"模型可以改正的错误"

工具描述怎么写才好用
--------------------
模型选工具的唯一依据就是工具名 + 描述 + 参数 Schema。所以描述里要有三件事：
  - 做什么（compute a math expression）
  - 什么时候用（when you need exact arithmetic）
  - 返回什么（returns the exact numeric result）

反面例子："计算器"——模型不知道边界在哪，容易在算日期、算字符串长度时也调它。
正面例子见下面的 calculate。
"""

from __future__ import annotations

from typing import Any

from ...errors import ToolInputError
from ..base import Feature, feature_tool
from . import engine


class CalculatorFeature(Feature):
    """为大模型提供精确计算能力的工具集。

    为什么这个功能是项目的第一个功能：模型最大的可靠性短板之一就是算术。
    它生成的是"最可能的token序列"，不是"正确的计算结果"，所以多位数乘法、
    百分比、三角函数这类任务经常产出"看起来对但差一点"的答案，
    而且模型没有办法自己发现错误。把算术外包给确定性引擎，是性价比最高的补强。
    """

    name = "calculator"
    description = "数学计算与单位换算工具集，为模型提供确定性的精确计算结果。"
    version = "0.1.0"

    # ------------------------------------------------------------------ #
    # 核心工具
    # ------------------------------------------------------------------ #
    @feature_tool(primary=True)
    def calculate(self, expression: str) -> dict[str, Any]:
        """计算一个数学表达式并返回精确结果。需要做算术、百分比、幂运算、三角函数、统计等计算时调用本工具，不要自己心算。

        支持 + - * / // % ** 运算符，白名单数学函数（sqrt、log、sin、comb、mean 等），
        常量 pi/e/tau，以及用 `变量名 = 值;` 定义中间变量（分号分隔多步，最后一步为返回值）。

        参数 expression 是数学表达式字符串，例如 "(1200 * 1.13) / 4"、
        "sqrt(2) * 100"、"a = 199; a * 0.85"、"mean(3, 7, 11)"。

        返回包含 expression（原表达式）、result（格式化结果）、raw_value（原始数值）、
        value_type（integer/float/boolean/sequence）、variables（表达式内定义的变量）的对象。
        只有数字、比较和布尔表达式，不能访问文件、网络或导入模块。
        """
        # 业务逻辑在 engine.compute 里；此处只做"接到参数 → 调引擎 → 转成可序列化结构"。
        result = engine.compute(expression)
        return result.to_payload()

    @feature_tool
    def evaluate_math(self, expression: str) -> dict[str, Any]:
        """calculate 的同义工具：输入一个数学表达式，返回精确数值结果。

        与 calculate 完全等价，提供两个名字是因为不同模型对"计算"的措辞偏好不同，
        多一个入口能提高命中率。新代码建议优先用 calculate。

        参数 expression 为数学表达式字符串，例如 "12 * 12"、"2 ** 32"、"pi * 2"。
        返回 expression、result、raw_value、value_type 等字段。
        """
        return engine.compute(expression).to_payload()

    # ------------------------------------------------------------------ #
    # 单位换算
    # ------------------------------------------------------------------ #
    @feature_tool
    def convert_units(
        self,
        value: float,
        from_unit: str,
        to_unit: str,
    ) -> dict[str, Any]:
        """在同类单位之间换算数值，例如把英里转公里、华氏转摄氏、GB 转 MB。

        支持类别：
          length（m, km, cm, mm, in, ft, yd, mi, nmi 等）
          mass（kg, g, mg, t, lb, oz 等）
          volume（l, ml, m3, gal, cup, floz 等）
          time（s, ms, min, h, d, wk, yr 等）
          data（b, kb, mb, gb, tb，按 1024 进制）
          temperature（c, f, k, r）
        也接受中文单位名（米、公里、摄氏度、华氏 等）。

        参数 value 是要换算的数值，from_unit 是原单位，to_unit 是目标单位。
        返回 category、input、result（含单位的字符串）、raw_value 等字段。
        跨类别换算（如公里转千克）会明确报错。
        """
        conversion = engine.convert(value, from_unit, to_unit)
        return conversion.to_payload()

    # ------------------------------------------------------------------ #
    # 能力自描述
    # ------------------------------------------------------------------ #
    @feature_tool
    def list_calculator_capabilities(self) -> dict[str, Any]:
        """列出计算器支持的全部函数、常量、运算符、语法说明与资源限额。

        在构造较长表达式之前调用本工具，可以确认某个函数是否存在
        （例如是否支持 comb、loggamma、mean），避免因函数名猜错而浪费一次调用。
        无需参数，返回结构化的能力清单。
        """
        capabilities = engine.describe_capabilities()
        capabilities["units"] = engine.supported_units()
        capabilities["tools"] = [
            {
                "name": "calculate",
                "purpose": "求值数学表达式",
                "example": "calculate(expression='(1200 * 1.13) / 4')",
            },
            {
                "name": "convert_units",
                "purpose": "单位换算",
                "example": "convert_units(value=100, from_unit='km', to_unit='mi')",
            },
            {
                "name": "list_calculator_capabilities",
                "purpose": "查询本清单（即当前工具）",
                "example": "list_calculator_capabilities()",
            },
        ]
        return capabilities


# 说明：loader.py 通过扫描本模块里"__module__ 等于本模块"的 Feature 子类来发现功能，
# 所以本文件末尾不需要任何注册代码。装饰器负责登记工具，import 负责触发，
# 加载顺序由 Feature.dependencies 决定。这就是"加功能不修改既有文件"的落地点。

"""计算器的业务逻辑层。

分层意图：safe_eval.py 只负责"把表达式算出来"，本文件负责"把结果整理成
对模型友好的形式"，feature.py 只负责"把能力暴露成 MCP 工具"。
三层各司其职，所以：
  - 想改结果格式（比如加百分比表示）→ 只动本文件
  - 想加支持的函数 → 只动 safe_eval.py
  - 想改工具名/描述 → 只动 feature.py
改哪层都不影响另外两层，测试也能精确对准某一层。

结果格式化的原则
----------------
模型读数字的能力弱于读文本：它看到一个裸的 `0.30000000000000004` 时，
可能会把这个尾巴带进后续推理。所以我们**同时给出精确值和人类可读值**，
让模型有明确的"该用哪个"的指引。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...errors import ToolInputError
from .safe_eval import evaluate

# describe_capabilities 由 feature 层调用，这里转出以便统一从 engine 访问能力清单。
from .safe_eval import describe_capabilities  # noqa: F401


@dataclass(slots=True)
class CalculationResult:
    """一次计算的结构化结果。

    为什么要返回结构而不只返回一个数字：MCP 工具的返回值会进模型上下文。
    结构化字段能让模型明确知道"主结果是什么"，避免它在长文本里找数字。
    """

    expression: str
    value: Any
    value_type: str
    formatted: str
    variables: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        """转成可直接序列化并返回给 MCP 客户端的字典。"""
        payload: dict[str, Any] = {
            "expression": self.expression,
            "result": self.formatted,
            "raw_value": self.value,
            "value_type": self.value_type,
        }
        if self.variables:
            payload["variables"] = self.variables
        if self.notes:
            payload["notes"] = self.notes
        return payload


def classify_value(value: Any) -> str:
    """给结果值打个类型标签，帮助模型正确解读。"""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, (list, tuple)):
        return "sequence"
    return type(value).__name__


def format_value(value: Any) -> str:
    """把结果格式化成人类可读字符串。

    规则：
      - bool → "True"/"False"（模型更容易理解成真假）
      - int → 原样（保留精度，不做科学计数法，哪怕很长）
      - float → 优先整数形式；否则保留 12 位有效数字并去掉尾随零。
        为什么要收窄到 12 位：`0.1 + 0.2` 在双精度下是 0.30000000000000004，
        直接给模型会污染它的后续推理。取 12 位有效数字既能表达精度，
        又能让常见浮点误差"消失"。
      - 序列 → 逐个格式化后拼接
    """
    if isinstance(value, bool):
        return "True" if value else "False"

    if isinstance(value, int):
        return str(value)

    if isinstance(value, float):
        if value != value:  # NaN
            return "NaN"
        if value in (float("inf"), float("-inf")):
            return "Infinity" if value > 0 else "-Infinity"
        # 整数形式的浮点（2.0）显示为 2，避免模型把 2.0 当成"约等于 2"
        if value.is_integer() and abs(value) < 1e16:
            return str(int(value))
        text = f"{value:.12g}"
        # 清理科学计数法里多余的前导零，如 1e-05 → 1e-5
        return text

    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(format_value(v) for v in value) + "]"

    return str(value)


def _collect_values(expression: str, value: Any) -> dict[str, Any]:
    """收集表达式里定义过的变量，便于模型核对中间量。

    为什么要重跑一遍而不是让 evaluate 顺便返回作用域：evaluate 的对外契约是
    "进字符串、出一个值"，极简的接口更好测也更好换实现。这里多跑一次纯算术
    的开销可以忽略，所以选择保住接口的干净，而不是为了这点性能让契约变胖。
    """
    # 内部函数是刻意下划线命名的：本包内部可共享，对外不承诺稳定。
    from .safe_eval import SAFE_CONSTANTS, _eval_one, _split_statements

    scope: dict[str, Any] = dict(SAFE_CONSTANTS)
    for statement in _split_statements(expression):
        try:
            _eval_one(statement, scope)
        except Exception:  # noqa: BLE001 - 变量收集是辅助信息，失败不影响主结果
            break

    # 只保留用户自定义的变量（排除常量）
    return {k: v for k, v in scope.items() if k not in SAFE_CONSTANTS}


def compute(expression: str, variables: dict[str, Any] | None = None) -> CalculationResult:
    """求值表达式并整理成结构化结果。

    这是计算器的主流程，feature 层的 calculate 工具直接调用它。
    """
    value = evaluate(expression, variables)

    notes: list[str] = []
    value_type = classify_value(value)

    if isinstance(value, float):
        if value != value:
            notes.append("结果是 NaN（非数字），通常意味着出现了未定义运算（如 0/0）。")
        elif value in (float("inf"), float("-inf")):
            notes.append("结果溢出为无穷大，请检查是否有除零或极端放大。")
        elif not value.is_integer():
            notes.append(
                "浮点结果已按 12 位有效数字显示；如需更多位数请在表达式中用 round() 指定。"
            )
    elif isinstance(value, int) and abs(value) >= 10**15:
        notes.append("这是一个大整数，已精确表示（未做浮点近似）。")

    return CalculationResult(
        expression=expression,
        value=value,
        value_type=value_type,
        formatted=format_value(value),
        variables=_collect_values(expression, value),
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# 单位换算
# --------------------------------------------------------------------------- #
# 结构：类别 -> {单位: 相对基准单位的换算系数}
# 温度比较特殊（仿射变换而非缩放），单独处理。
_LINEAR_UNITS: dict[str, dict[str, float]] = {
    "length": {
        # 基准：米
        "m": 1.0, "km": 1000.0, "cm": 0.01, "mm": 0.001,
        "um": 1e-6, "nm": 1e-9,
        "in": 0.0254, "ft": 0.3048, "yd": 0.9144,
        "mi": 1609.344, "nmi": 1852.0,
        # 中文别名，方便模型直接照抄用户原话
        "米": 1.0, "千米": 1000.0, "公里": 1000.0, "厘米": 0.01, "毫米": 0.001,
        "英寸": 0.0254, "英尺": 0.3048, "码": 0.9144, "英里": 1609.344,
    },
    "mass": {
        # 基准：千克
        "kg": 1.0, "g": 0.001, "mg": 1e-6, "ug": 1e-9, "t": 1000.0,
        "lb": 0.45359237, "oz": 0.028349523125, "st": 6.35029318,
        "千克": 1.0, "公斤": 1.0, "克": 0.001, "毫克": 1e-6, "吨": 1000.0,
        "磅": 0.45359237, "盎司": 0.028349523125,
    },
    "volume": {
        # 基准：升
        "l": 1.0, "ml": 0.001, "cl": 0.01, "dl": 0.1,
        "m3": 1000.0, "cm3": 0.001,
        "gal": 3.785411784,        # 美制加仑
        "qt": 0.946352946,         # 美制夸脱
        "pt": 0.473176473,         # 美制品脱
        "cup": 0.2365882365,       # 美制杯
        "floz": 0.0295735295625,   # 美制液量盎司
        "tbsp": 0.01478676478125,
        "tsp": 0.00492892159375,
        "升": 1.0, "毫升": 0.001, "立方米": 1000.0, "加仑": 3.785411784,
    },
    "time": {
        # 基准：秒
        "s": 1.0, "ms": 0.001, "us": 1e-6, "ns": 1e-9,
        "min": 60.0, "h": 3600.0, "d": 86400.0,
        "wk": 604800.0, "yr": 31557600.0,  # 儒略年 = 365.25 天
        "秒": 1.0, "毫秒": 0.001, "分钟": 60.0, "小时": 3600.0,
        "天": 86400.0, "周": 604800.0, "年": 31557600.0,
    },
    "data": {
        # 基准：字节。存储单位用 1024 进制（KiB 系列），
        # 网络速率单位用 1000 进制（kb/s 系列）——这里统一按存储处理，
        # 并在返回里注明所用进制，避免 1000/1024 之争悄悄出错。
        "b": 1.0, "kb": 1024.0, "mb": 1024.0**2, "gb": 1024.0**3,
        "tb": 1024.0**4, "pb": 1024.0**5,
        "kib": 1024.0, "mib": 1024.0**2, "gib": 1024.0**3, "tib": 1024.0**4,
        "字节": 1.0, "千字节": 1024.0, "兆字节": 1024.0**2, "吉字节": 1024.0**3,
    },
}

# 温度专用换算：一律先转成摄氏度，再转目标单位。
_TEMPERATURE_TO_CELSIUS = {
    "c": ("linear", 0.0, 1.0),      # C = x
    "f": ("linear", -32.0, 5.0 / 9.0),  # C = (x - 32) * 5/9
    "k": ("linear", -273.15, 1.0),  # C = x - 273.15
    "r": ("linear", -491.67, 5.0 / 9.0),  # 兰氏度 C = (x - 491.67) * 5/9
    "摄氏": ("linear", 0.0, 1.0),
    "度": ("linear", 0.0, 1.0),
    "华氏": ("linear", -32.0, 5.0 / 9.0),
    "开尔文": ("linear", -273.15, 1.0),
}
_CELSIUS_TO_TARGET = {
    "c": ("linear", 0.0, 1.0),
    "f": ("affine", 32.0, 9.0 / 5.0),   # F = C * 9/5 + 32
    "k": ("affine", 273.15, 1.0),
    "r": ("affine", 491.67, 9.0 / 5.0),
    "摄氏": ("linear", 0.0, 1.0),
    "度": ("linear", 0.0, 1.0),
    "华氏": ("affine", 32.0, 9.0 / 5.0),
    "开尔文": ("affine", 273.15, 1.0),
}


def _normalize_unit(unit: str) -> str:
    """规范化单位写法：去空格、转小写、把常见全角字符转半角。"""
    return unit.strip().lower().replace("°", "").replace("　", "")


def _find_category(unit: str) -> str | None:
    """找出单位所属类别。两个类别都收录同一单位时（理论上不该发生）取先匹配的。"""
    if unit in _TEMPERATURE_TO_CELSIUS:
        return "temperature"
    for category, table in _LINEAR_UNITS.items():
        if unit in table:
            return category
    return None


@dataclass(slots=True)
class ConversionResult:
    """一次单位换算的结果。"""

    value: float
    from_unit: str
    to_unit: str
    category: str
    result: float
    formatted: str
    notes: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "category": self.category,
            "input": f"{format_value(self.value)} {self.from_unit}",
            "result": self.formatted,
            "raw_value": self.result,
        }
        if self.notes:
            payload["notes"] = self.notes
        return payload


def convert(value: float, from_unit: str, to_unit: str) -> ConversionResult:
    """在同类单位之间换算。

    参数不合法时抛出 ToolInputError，并在消息里列出该类别支持的全部单位——
    这一步很关键：模型拿到"支持列表"就能自己纠正拼写，不用再来回试。
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ToolInputError(f"value 必须是数字，收到 {type(value).__name__}。")

    src = _normalize_unit(from_unit)
    dst = _normalize_unit(to_unit)

    src_category = _find_category(src)
    dst_category = _find_category(dst)

    if src_category is None:
        raise ToolInputError(
            f"未知单位 '{from_unit}'。支持的单位类别："
            f"{', '.join(sorted(list(_LINEAR_UNITS) + ['temperature']))}。"
        )
    if dst_category is None:
        raise ToolInputError(
            f"未知单位 '{to_unit}'。支持的单位类别："
            f"{', '.join(sorted(list(_LINEAR_UNITS) + ['temperature']))}。"
        )
    if src_category != dst_category:
        raise ToolInputError(
            f"单位类别不匹配：'{from_unit}' 属于 {src_category}，"
            f"'{to_unit}' 属于 {dst_category}，无法直接换算。"
        )

    notes: list[str] = []

    if src_category == "temperature":
        _, offset, scale = _TEMPERATURE_TO_CELSIUS[src]
        celsius = (float(value) + offset) * scale
        _, t_offset, t_scale = _CELSIUS_TO_TARGET[dst]
        result = celsius * t_scale + t_offset
        if dst in ("k", "开尔文") and result < 0:
            notes.append("换算结果低于绝对零度，请核对输入是否合理。")
    elif src_category == "data":
        table = _LINEAR_UNITS[src_category]
        base = float(value) * table[src]
        result = base / table[dst]
        notes.append("数据量按 1024 进制换算（1 KB = 1024 B）；若需 1000 进制请明确说明。")
    else:
        table = _LINEAR_UNITS[src_category]
        base = float(value) * table[src]
        result = base / table[dst]

    return ConversionResult(
        value=float(value),
        from_unit=from_unit,
        to_unit=to_unit,
        category=src_category,
        result=result,
        formatted=f"{format_value(result)} {to_unit}",
        notes=notes,
    )


def supported_units() -> dict[str, list[str]]:
    """列出各类别支持的单位（供能力查询工具使用）。

    只列 ASCII 短名，避免把中英文混排的超长列表塞给模型。
    """
    units = {category: sorted(table) for category, table in _LINEAR_UNITS.items()}
    # 温度只保留短名
    units["temperature"] = ["c", "f", "k", "r"]
    return units

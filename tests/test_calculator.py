"""calculator 功能层测试：注册、工具调用、单位换算、结果格式。

与 test_safe_eval.py 的分工：
  - test_safe_eval.py   测"算得对不对、安全不安全"（引擎层）
  - 本文件              测"能不能被模型正常调用、返回的东西模型能不能用"（工具层）
两者互不重复，这样任何一层出问题都能立刻定位到层。
"""

from __future__ import annotations

import pytest

from faster_mcp.config import Settings
from faster_mcp.errors import ToolInputError
from faster_mcp.features.calculator import CalculatorFeature
from faster_mcp.features.calculator.engine import convert, supported_units
from faster_mcp.registry import FeatureRegistry


@pytest.fixture
def feature() -> CalculatorFeature:
    """一个已 setup 的功能实例。"""
    instance = CalculatorFeature()
    instance.setup()
    return instance


# --------------------------------------------------------------------------- #
# 注册契约
# --------------------------------------------------------------------------- #
def test_feature_registers_cleanly() -> None:
    """注册不冲突，且四个工具都在。

    这条测试的价值最高：它一次性验证了装饰器生效、工具名没撞车、
    Schema 生成没崩。新增功能时照抄这个形态。
    """
    reg = FeatureRegistry()
    reg.register(CalculatorFeature())
    reg.finalize()

    expected = {"calculate", "evaluate_math", "convert_units", "list_calculator_capabilities"}
    assert expected <= set(reg.tools)


def test_feature_name_is_stable() -> None:
    """功能名是启停开关的键，属于对外契约，改动会破坏别人的配置。

    所以用测试把它钉住：改名必须是一次"有意为之"的改动。
    """
    assert CalculatorFeature.name == "calculator"


def test_tools_have_required_schema_fields() -> None:
    """每个工具都必须有名字、描述、inputSchema —— 三者缺一，模型的调用质量就会掉。"""
    reg = FeatureRegistry()
    reg.register(CalculatorFeature())
    reg.finalize()

    for spec in reg.iter_tools():
        assert spec.name
        assert spec.description, f"{spec.name} 缺少描述"
        assert spec.parameters["type"] == "object"
        assert "properties" in spec.parameters


def test_calculate_schema_exposes_expression() -> None:
    reg = FeatureRegistry()
    reg.register(CalculatorFeature())
    reg.finalize()

    spec = reg.tools["calculate"]
    assert "expression" in spec.parameters["properties"]
    assert "expression" in spec.parameters["required"]


def test_convert_units_schema_has_three_required_params() -> None:
    reg = FeatureRegistry()
    reg.register(CalculatorFeature())
    reg.finalize()

    spec = reg.tools["convert_units"]
    assert set(spec.parameters["required"]) == {"value", "from_unit", "to_unit"}


def test_self_is_not_exposed_as_parameter() -> None:
    """`self` 必须被剔除，否则模型会看到并尝试填写一个不该存在的参数。"""
    reg = FeatureRegistry()
    reg.register(CalculatorFeature())
    reg.finalize()

    for spec in reg.iter_tools():
        assert "self" not in spec.parameters["properties"]


# --------------------------------------------------------------------------- #
# calculate 工具
# --------------------------------------------------------------------------- #
def test_calculate_returns_structured_payload(feature: CalculatorFeature) -> None:
    payload = feature.calculate("(1200 * 1.13) / 4")

    assert payload["expression"] == "(1200 * 1.13) / 4"
    assert payload["result"] == "339"
    assert payload["raw_value"] == pytest.approx(339.0)
    assert payload["value_type"] == "float"


def test_calculate_exposes_intermediate_variables(feature: CalculatorFeature) -> None:
    """中间变量回传，模型才能核对"我给的数对不对"。"""
    payload = feature.calculate("price = 199; price * 0.85")
    assert payload["variables"]["price"] == 199


def test_evaluate_math_is_equivalent(feature: CalculatorFeature) -> None:
    assert feature.evaluate_math("6 * 7") == feature.calculate("6 * 7")


def test_calculate_propagates_input_error(feature: CalculatorFeature) -> None:
    with pytest.raises(ToolInputError):
        feature.calculate("1 +")


# --------------------------------------------------------------------------- #
# 结果格式化
# --------------------------------------------------------------------------- #
def test_float_noise_is_hidden(feature: CalculatorFeature) -> None:
    """0.1 + 0.2 的双精度尾巴不应原样丢给模型，否则会污染后续推理。"""
    payload = feature.calculate("0.1 + 0.2")
    assert payload["result"] == "0.3"


def test_integer_float_displays_without_decimal(feature: CalculatorFeature) -> None:
    assert feature.calculate("10 / 2")["result"] == "5"


def test_large_integer_keeps_full_precision(feature: CalculatorFeature) -> None:
    payload = feature.calculate("2 ** 100")
    assert payload["result"] == str(2**100)
    assert payload["value_type"] == "integer"


def test_boolean_result(feature: CalculatorFeature) -> None:
    payload = feature.calculate("3 > 2")
    assert payload["result"] == "True"
    assert payload["value_type"] == "boolean"


def test_infinity_is_reported_with_note(feature: CalculatorFeature) -> None:
    payload = feature.calculate("1e308 * 10")
    assert payload["result"] == "Infinity"
    # 溢出属于"结果看着像数字但已经不可用"的情况，必须给出提示
    assert any("溢出" in note for note in payload["notes"])


# --------------------------------------------------------------------------- #
# convert_units 工具
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value, src, dst, expected",
    [
        (1, "km", "m", 1000),
        (1, "mi", "km", 1.609344),
        (100, "cm", "in", 39.37007874),
        (1, "kg", "lb", 2.20462262),
        (1, "gb", "mb", 1024),
        (60, "min", "h", 1),
        (1, "d", "s", 86400),
        (1, "l", "ml", 1000),
    ],
)
def test_linear_conversions(value, src, dst, expected) -> None:
    assert convert(value, src, dst).result == pytest.approx(expected, rel=1e-6)


def test_temperature_celsius_to_fahrenheit() -> None:
    assert convert(100, "c", "f").result == pytest.approx(212.0)
    assert convert(0, "c", "f").result == pytest.approx(32.0)
    assert convert(37, "c", "f").result == pytest.approx(98.6)


def test_temperature_fahrenheit_to_celsius() -> None:
    assert convert(212, "f", "c").result == pytest.approx(100.0)
    assert convert(32, "f", "c").result == pytest.approx(0.0)


def test_temperature_kelvin() -> None:
    assert convert(0, "c", "k").result == pytest.approx(273.15)
    assert convert(300, "k", "c").result == pytest.approx(26.85)
    assert convert(32, "f", "k").result == pytest.approx(273.15)


def test_temperature_roundtrip() -> None:
    """往返换算必须回到原值。这条测试能抓住仿射变换写反的隐蔽错误。"""
    original = 25.0
    fahrenheit = convert(original, "c", "f").result
    assert convert(fahrenheit, "f", "c").result == pytest.approx(original)


def test_chinese_unit_names() -> None:
    assert convert(1, "公里", "米").result == pytest.approx(1000)
    assert convert(1, "千克", "克").result == pytest.approx(1000)


def test_degree_symbol_is_tolerated() -> None:
    """用户会说"度"，不该因为多了个 ° 就失败。"""
    assert convert(100, "°c", "°f").result == pytest.approx(212.0)


def test_convert_wrong_category_is_rejected() -> None:
    with pytest.raises(ToolInputError, match="类别不匹配"):
        convert(1, "km", "kg")


def test_convert_unknown_unit_lists_alternatives() -> None:
    """报错信息里要给出线索，模型才能自己改对。"""
    with pytest.raises(ToolInputError) as excinfo:
        convert(1, "furlong", "m")
    assert "未知单位" in str(excinfo.value)


def test_convert_non_numeric_value_rejected() -> None:
    with pytest.raises(ToolInputError):
        convert("abc", "m", "km")  # type: ignore[arg-type]


def test_convert_through_tool_returns_payload(feature: CalculatorFeature) -> None:
    payload = feature.convert_units(1, "km", "mi")
    assert payload["category"] == "length"
    assert payload["input"] == "1 km"
    assert "mi" in payload["result"]


def test_data_conversion_notes_the_base() -> None:
    """1024 vs 1000 是个经典歧义，必须显式声明用了哪种。"""
    result = convert(1, "gb", "mb")
    assert any("1024" in note for note in result.notes)


def test_supported_units_covers_all_categories() -> None:
    units = supported_units()
    assert set(units) == {"length", "mass", "volume", "time", "data", "temperature"}
    assert "km" in units["length"]
    assert "k" in units["temperature"]


# --------------------------------------------------------------------------- #
# 能力查询工具
# --------------------------------------------------------------------------- #
def test_capabilities_lists_functions_and_units(feature: CalculatorFeature) -> None:
    payload = feature.list_calculator_capabilities()

    assert "sqrt" in payload["functions"]["幂与根"]
    assert "pi" in payload["constants"]
    assert "length" in payload["units"]
    assert any("calculate" in str(t) for t in payload["tools"])


# --------------------------------------------------------------------------- #
# 与配置系统的联动
# --------------------------------------------------------------------------- #
def test_feature_can_be_disabled_by_config() -> None:
    """黑名单应能关掉计算器；这条测试钉住了"功能可启停"这个架构承诺。"""
    settings = Settings.from_env({"FASTER_MCP_DISABLED": "calculator"})
    assert settings.is_feature_enabled("calculator") is False


def test_feature_enabled_by_default() -> None:
    assert Settings.from_env({}).is_feature_enabled("calculator") is True

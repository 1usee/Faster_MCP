"""不依赖 MCP 客户端的自检脚本。

用途：加完新功能后，用一条命令确认"服务能不能正常组装、工具长什么样、
计算结果对不对"。不需要装客户端，也不需要起协议循环。

运行：
    python scripts\\smoke_test.py

退出码：0 表示全部通过，1 表示有失败项（便于接进 CI）。

为什么要有这个脚本，而不是让人直接 pytest：
  1) pytest 需要额外安装 dev 依赖；本脚本只用标准库 + 本项目
  2) 它会**打印**出工具清单和 Schema，让人直观看到"模型将看到什么"
     —— 这是调工具描述时最需要的信息
"""

from __future__ import annotations

import sys
from pathlib import Path

# 允许直接以脚本方式运行（把项目根加进 sys.path）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from faster_mcp.config import Settings  # noqa: E402
from faster_mcp.errors import FasterMcpError  # noqa: E402
from faster_mcp.loader import load_features  # noqa: E402
from faster_mcp.registry import FeatureRegistry  # noqa: E402

PASS = "[OK]  "
FAIL = "[FAIL]"


def section(title: str) -> None:
    """打印带分隔线的标题，让输出可读。"""
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def check(condition: bool, description: str) -> bool:
    """打印一条检查结果，返回是否通过。"""
    print(f"{PASS if condition else FAIL} {description}")
    return condition


def main() -> int:
    failures = 0

    # ------------------------------------------------------------------ #
    section("1. 加载功能模块")
    # ------------------------------------------------------------------ #
    try:
        registry = load_features(Settings.from_env({}), target=FeatureRegistry())
        registry.finalize()
    except FasterMcpError as exc:
        print(f"{FAIL} 功能加载失败：{exc}")
        return 1

    print(registry.summary())
    failures += not check(len(registry.features) > 0, "至少加载了一个功能")
    failures += not check(len(registry.tools) > 0, "至少注册了一个工具")

    # ------------------------------------------------------------------ #
    section("2. 工具 Schema（模型将看到的内容）")
    # ------------------------------------------------------------------ #
    for spec in registry.iter_tools():
        print(f"\n--- {spec.name}  [功能: {spec.feature_name}] ---")
        print(f"描述: {spec.description}")
        props = spec.parameters.get("properties", {})
        required = set(spec.parameters.get("required", []))
        if props:
            print("参数:")
            for param_name, schema in props.items():
                flag = "(必填)" if param_name in required else "(选填)"
                print(f"  - {param_name} {flag}: {schema.get('type', '?')}")
        else:
            print("参数: 无")

    # ------------------------------------------------------------------ #
    section("3. 工具可调用性")
    # ------------------------------------------------------------------ #
    calc = registry.tools.get("calculate")
    if calc is None:
        print(f"{FAIL} 找不到 calculate 工具")
        return 1

    # 每个用例：(描述, 表达式, 期望值字符串)
    cases = [
        ("基础四则", "(1200 * 1.13) / 4", "339"),
        ("大整数精度", "2 ** 100", str(2**100)),
        ("三角函数", "round(sin(radians(30)), 4)", "0.5"),
        ("多步变量", "price = 199; round(price * 0.85, 2)", "169.15"),
        ("统计", "mean(3, 7, 11)", "7"),
        ("布尔表达式", "3 > 2", "True"),
        ("浮点误差收敛", "0.1 + 0.2", "0.3"),
    ]

    for label, expression, expected in cases:
        try:
            payload = calc.func(expression=expression)
            actual = str(payload.get("result"))
            ok = actual == expected
        except Exception as exc:  # noqa: BLE001
            actual, ok = f"异常: {exc}", False

        print(f"{PASS if ok else FAIL} {label:<14} {expression:<32} -> {actual}")
        failures += not ok

    # ------------------------------------------------------------------ #
    section("4. 单位换算")
    # ------------------------------------------------------------------ #
    convert = registry.tools.get("convert_units")
    if convert is None:
        print(f"{FAIL} 找不到 convert_units 工具")
        return 1

    conv_cases = [
        ("长度", 1, "km", "mi", 0.621371),
        ("温度", 100, "c", "f", 212.0),
        ("数据量", 1, "gb", "mb", 1024.0),
        ("质量", 1, "kg", "lb", 2.204622),
        ("时间", 1, "d", "h", 24.0),
    ]

    for label, value, src, dst, expected in conv_cases:
        try:
            payload = convert.func(value=value, from_unit=src, to_unit=dst)
            raw = float(payload["raw_value"])
            ok = abs(raw - expected) < max(1e-4, abs(expected) * 1e-6)
        except Exception as exc:  # noqa: BLE001
            payload, ok = {"result": f"异常: {exc}"}, False

        print(f"{PASS if ok else FAIL} {label:<8} {value} {src} -> {payload['result']}")
        failures += not ok

    # ------------------------------------------------------------------ #
    section("5. 安全边界（以下必须被拒绝）")
    # ------------------------------------------------------------------ #
    dangerous = [
        "__import__('os').system('echo pwned')",
        "open('C:/Windows/win.ini').read()",
        "().__class__.__bases__",
        "'abc'.upper()",
        "globals()",
        "lambda x: x",
        "[x for x in range(3)]",
    ]

    for expression in dangerous:
        try:
            payload = calc.func(expression=expression)
            rejected = payload.get("ok") is False
            detail = payload.get("error_type", "?")
        except Exception as exc:  # noqa: BLE001 - 抛异常也算拒绝成功
            rejected, detail = True, type(exc).__name__

        print(f"{PASS if rejected else FAIL} 已拒绝: {expression[:48]:<48} ({detail})")
        failures += not rejected

    # ------------------------------------------------------------------ #
    section("6. 配置开关")
    # ------------------------------------------------------------------ #
    only_calc = Settings.from_env({"FASTER_MCP_FEATURES": "calculator"})
    failures += not check(
        only_calc.is_feature_enabled("calculator"), "白名单里包含 calculator 时被启用"
    )

    blocked = Settings.from_env({"FASTER_MCP_DISABLED": "calculator"})
    failures += not check(
        not blocked.is_feature_enabled("calculator"), "黑名单里包含 calculator 时被禁用"
    )

    # ------------------------------------------------------------------ #
    section("结果汇总")
    # ------------------------------------------------------------------ #
    if failures:
        print(f"{FAIL} 共有 {failures} 项未通过，请检查上面的输出。")
        return 1

    print(f"{PASS} 全部检查通过。服务可以正常启动：python -m faster_mcp")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""架构层测试：验证"插件式可扩展架构"这个承诺真的成立。

为什么单独写这份：前两份测试测的是"计算器算得对不对"。但本项目的核心卖点
是**架构**——"以后加新功能不用改核心代码"。这个承诺如果没有测试守着，
很可能在某人图省事往 server.py 里硬编码一个 import 时就悄悄失效了。

本文件的测试用"路径无关"的方式表达约束：
  - 功能能被自动发现，不需要任何登记
  - 新增功能不要求修改既有文件
  - 依赖排序与循环检测生效
"""

from __future__ import annotations

import importlib
import shutil
import sys
import textwrap
from pathlib import Path

import pytest

from faster_mcp.config import Settings
from faster_mcp.errors import FeatureLoadError
from faster_mcp.features.base import Feature, feature_tool
from faster_mcp.loader import _discover_feature_modules, _sort_by_dependencies, load_features
from faster_mcp.registry import FeatureRegistry, ToolSpec


# --------------------------------------------------------------------------- #
# 自动发现
# --------------------------------------------------------------------------- #
def test_calculator_is_discovered_without_registration() -> None:
    """features/ 下的子包应当被自动发现，不需要任何清单登记。"""
    assert "calculator" in _discover_feature_modules()


def test_load_features_registers_calculator() -> None:
    reg = load_features(Settings.from_env({}), target=FeatureRegistry())
    reg.finalize()
    assert "calculator" in reg.features
    assert "calculate" in reg.tools


def test_load_features_respects_blacklist() -> None:
    settings = Settings.from_env({"FASTER_MCP_DISABLED": "calculator,datetime"})
    with pytest.raises(FeatureLoadError):
        load_features(settings, target=FeatureRegistry())


# --------------------------------------------------------------------------- #
# 注册表契约
# --------------------------------------------------------------------------- #
class _DummyFeature(Feature):
    name = "dummy_test_feature"
    description = "测试用的假功能"

    @feature_tool
    def dummy_tool(self, x: int) -> int:
        """把输入加一。"""
        return x + 1


def test_dummy_feature_registers_and_invokes() -> None:
    """证明"只写一个类"就能产出可调用工具——这是加功能的完整最小成本。"""
    reg = FeatureRegistry()
    reg.register(_DummyFeature())
    reg.finalize()

    spec = reg.tools["dummy_tool"]
    assert spec.func(x=1) == 2
    assert spec.feature_name == "dummy_test_feature"


def test_duplicate_feature_name_rejected() -> None:
    reg = FeatureRegistry()
    reg.register(_DummyFeature())
    with pytest.raises(FeatureLoadError, match="功能名冲突"):
        reg.register(_DummyFeature())


def test_duplicate_tool_name_rejected() -> None:
    """工具名撞车必须启动期就报错，而不是等模型调用时才发现。

    因为工具名是模型看到的名字，静默覆盖会导致"模型以为在调 A，实际调了 B"。
    """
    class _OtherFeature(Feature):
        name = "other_test_feature"

        @feature_tool(name="dummy_tool")  # 故意撞名
        def some_tool(self) -> int:
            """撞名测试。"""
            return 0

    reg = FeatureRegistry()
    reg.register(_DummyFeature())
    with pytest.raises(FeatureLoadError, match="工具名冲突"):
        reg.register(_OtherFeature())


def test_feature_without_tools_rejected() -> None:
    class _EmptyFeature(Feature):
        name = "empty_test_feature"

    reg = FeatureRegistry()
    with pytest.raises(FeatureLoadError, match="没有提供任何工具"):
        reg.register(_EmptyFeature())


def test_feature_without_name_rejected() -> None:
    class _NamelessFeature(Feature):
        @feature_tool
        def t(self) -> int:
            """无名字功能。"""
            return 1

    reg = FeatureRegistry()
    with pytest.raises(FeatureLoadError, match="缺少 `name`"):
        reg.register(_NamelessFeature())


def test_explicit_toolspec_style_works() -> None:
    """显式声明风格也应可用，供需要动态生成工具的场合使用。"""

    class _ExplicitFeature(Feature):
        name = "explicit_test_feature"

        def tools(self) -> list[ToolSpec]:
            return [
                ToolSpec(
                    name="explicit_tool",
                    description="显式声明的工具。",
                    parameters={"type": "object", "properties": {}, "required": []},
                    func=lambda: 42,
                    feature_name=self.name,
                )
            ]

    reg = FeatureRegistry()
    reg.register(_ExplicitFeature())
    reg.finalize()
    assert reg.tools["explicit_tool"].func() == 42


# --------------------------------------------------------------------------- #
# 依赖排序
# --------------------------------------------------------------------------- #
class _FeatureA(Feature):
    name = "test_a"


class _FeatureB(Feature):
    name = "test_b"
    dependencies = ("test_a",)


class _FeatureC(Feature):
    name = "test_c"
    dependencies = ("test_b",)


def test_dependency_order() -> None:
    """依赖方必须排在依赖之后。"""
    ordered = _sort_by_dependencies(
        {"test_c": _FeatureC, "test_b": _FeatureB, "test_a": _FeatureA}
    )
    names = [cls.name for cls in ordered]
    assert names.index("test_a") < names.index("test_b") < names.index("test_c")


def test_circular_dependency_detected() -> None:
    class _Loop1(Feature):
        name = "test_loop1"
        dependencies = ("test_loop2",)

    class _Loop2(Feature):
        name = "test_loop2"
        dependencies = ("test_loop1",)

    with pytest.raises(FeatureLoadError, match="循环"):
        _sort_by_dependencies({"test_loop1": _Loop1, "test_loop2": _Loop2})


def test_missing_dependency_detected() -> None:
    class _Orphan(Feature):
        name = "test_orphan"
        dependencies = ("test_nonexistent",)

    with pytest.raises(FeatureLoadError, match="依赖缺失"):
        _sort_by_dependencies({"test_orphan": _Orphan})


# --------------------------------------------------------------------------- #
# 最终承诺：新增功能必须"只增不改"
# --------------------------------------------------------------------------- #
def test_new_feature_directory_requires_no_core_change(tmp_path: Path) -> None:
    """在 features/ 下凭空放一个新目录，它应当不需要改任何既有文件就被加载。

    实现方式：
      1. 在真实的 features/ 目录里临时创建一个新功能包
      2. 清掉模块缓存后重新导入，验证它被自动发现
      3. 无论成败都删掉临时目录，保证不污染仓库

    这条测试就是"可扩展架构"这个卖点的回归测试。
    如果哪天有人把功能改成手写 import 清单，这条会立刻失败。
    """
    import faster_mcp.features as features_pkg

    features_dir = Path(features_pkg.__file__).parent
    new_pkg = features_dir / "_tmp_regression_feature"
    created = False

    try:
        if new_pkg.exists():
            shutil.rmtree(new_pkg, ignore_errors=True)
        new_pkg.mkdir()
        (new_pkg / "__init__.py").write_text(
            textwrap.dedent(
                '''
                """临时回归测试功能。"""

                from .feature import TmpRegressionFeature

                __all__ = ["TmpRegressionFeature"]
                '''
            ).lstrip(),
            encoding="utf-8",
        )
        (new_pkg / "feature.py").write_text(
            textwrap.dedent(
                '''
                """临时功能：验证"只增不改"的加载契约。"""

                from ..base import Feature, feature_tool


                class TmpRegressionFeature(Feature):
                    name = "_tmp_regression"
                    description = "临时回归测试功能"

                    @feature_tool
                    def tmp_ping(self) -> str:
                        """回归测试用。"""
                        return "pong"
                '''
            ).lstrip(),
            encoding="utf-8",
        )
        created = True

        # 清缓存让 pkgutil 能看到新目录
        importlib.invalidate_caches()
        for mod_name in list(sys.modules):
            if mod_name.startswith("faster_mcp.features._tmp_regression_feature"):
                del sys.modules[mod_name]

        # 不修改任何既有文件，直接加载
        modules = _discover_feature_modules()
        assert "_tmp_regression_feature" in modules

        reg = load_features(Settings.from_env({}), target=FeatureRegistry())
        reg.finalize()
        assert "_tmp_regression" in reg.features
        assert "tmp_ping" in reg.tools
        assert reg.tools["tmp_ping"].func() == "pong"

    finally:
        # 清理：即使断言失败也要删掉临时目录，避免污染仓库。
        # 这里必须递归删除，因为 Python 会在 __pycache__ 中生成 .pyc 文件。
        if created:
            shutil.rmtree(new_pkg, ignore_errors=True)
            importlib.invalidate_caches()
        for mod_name in list(sys.modules):
            if mod_name.startswith("faster_mcp.features._tmp_regression_feature"):
                del sys.modules[mod_name]

"""功能模块自动发现与加载。

核心机制（"import 即注册"）
-------------------------
registry.py 里讲过，注册发生在 import 阶段。本模块的工作就是：
**把 features/ 目录下每个模块 import 一遍**，让它们有机会自我注册。
import 之后，再从 `faster_mcp.features.base` 里取出本次新出现的 Feature 子类，
按依赖关系排好序，注册进 registry。

为什么不让模块自己往 registry 里塞、而是要收集 Feature 子类？
因为依赖排序需要先拿到"所有功能及其依赖声明"，再统一决策加载顺序。
先收集、后注册，才能做拓扑排序和循环检测。

自动发现怎么实现
----------------
用 `pkgutil.iter_modules` 遍历 `features/` 的直接子包，用 `importlib.import_module`
导入 `faster_mcp.features.<子包名>`。新增功能时不需要在任何地方登记——
这就是 README 里"加功能只增加文件、不修改文件"的技术根据。

注意：`features/` 下的**每个子目录必须有 __init__.py 并导出 Feature 子类**；
只有平级的 .py 文件（如 base.py）不会被当作功能模块扫描，避免把工具函数误当功能。
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from types import ModuleType

from .config import Settings
from .errors import FeatureLoadError
from .registry import Feature, FeatureRegistry

logger = logging.getLogger(__name__)

# 功能模块所在的包路径
FEATURES_PACKAGE = "faster_mcp.features"


def _discover_feature_modules() -> list[str]:
    """列出 features 包下的所有**子包**名（不含 base 这类平级模块）。

    返回的是包名列表，如 ["calculator"]，而不是完整路径。
    """
    package = importlib.import_module(FEATURES_PACKAGE)
    module_names: list[str] = []

    for info in pkgutil.iter_modules(package.__path__):
        # ispkg=True 才是功能模块。平级 .py 文件是公共代码，跳过。
        if info.ispkg:
            module_names.append(info.name)

    return sorted(module_names)


def _import_module(module_name: str) -> ModuleType:
    """导入一个功能包，并把它抛出的 ImportError 包装成可定位的启动期错误。

    为什么要包装：功能模块里常见的手误是漏写依赖包或写错相对导入，
    原始的 ImportError 信息不告诉你"是哪个功能坏了"。这里补上上下文。
    """
    full_name = f"{FEATURES_PACKAGE}.{module_name}"
    try:
        return importlib.import_module(full_name)
    except Exception as exc:  # noqa: BLE001 - 任何导入失败都不该让服务半死不活
        raise FeatureLoadError(
            f"导入功能模块 '{full_name}' 失败：{type(exc).__name__}: {exc}\n"
            "请检查该功能目录下的 __init__.py 是否正确导出了 Feature 子类，"
            "以及模块内是否存在语法错误或缺失的第三方依赖。"
        ) from exc


def _collect_feature_subclasses(module: ModuleType) -> list[type[Feature]]:
    """取出模块中及其子模块导出的 Feature 子类。

    package 模块（如 `faster_mcp.features.calculator`）通常只会在 `__init__.py`
    里做 `from .feature import CalculatorFeature` 这样的导出；
    这时类的 `__module__` 是 `faster_mcp.features.calculator.feature`，
    不会等于 package 的 `__name__`。因此这里要允许同一功能包命名空间下的
    Feature 子类被发现，并且仍排除基类本身和抽象类。
    """
    found: list[type[Feature]] = []
    seen: set[type[Feature]] = set()
    namespace = module.__name__
    for attr in vars(module).values():
        if not isinstance(attr, type):
            continue
        if not issubclass(attr, Feature) or attr is Feature:
            continue
        if getattr(attr, "__abstract__", False):  # 预留：标记为抽象基类则跳过
            continue
        mod_name = getattr(attr, "__module__", "")
        if mod_name != namespace and not mod_name.startswith(f"{namespace}."):
            continue
        if attr in seen:
            continue
        seen.add(attr)
        found.append(attr)
    return found


def _sort_by_dependencies(
    feature_classes: dict[str, type[Feature]],
) -> list[type[Feature]]:
    """按 Feature.dependencies 做拓扑排序，并检测循环依赖与缺失依赖。

    为什么需要：功能之间可能复用（比如 `finance` 依赖 `calculator` 的求值器）。
    先加载被依赖方，能让依赖方在 setup() 里直接取用已经准备好的能力。

    返回顺序：依赖在前，依赖方在后。
    """
    ordered: list[type[Feature]] = []
    # 0=未访问 1=访问中（栈上） 2=已完成
    state: dict[str, int] = {name: 0 for name in feature_classes}

    def visit(name: str, path: list[str]) -> None:
        if state.get(name) == 2:
            return
        if state.get(name) == 1:
            cycle = " -> ".join(path + [name])
            raise FeatureLoadError(
                f"功能依赖出现循环：{cycle}\n"
                "请打破依赖环（通常做法是把公共逻辑抽到一个被双方依赖的第三个功能里）。"
            )

        if name not in feature_classes:
            raise FeatureLoadError(
                f"依赖缺失：{path[-1] if path else '?'} 声明依赖功能 '{name}'，"
                "但 features/ 下没有找到同名功能。"
                "请检查依赖名拼写，或确认该功能没有被删除。"
            )

        state[name] = 1
        for dep in feature_classes[name].dependencies:
            visit(dep, path + [name])
        state[name] = 2
        ordered.append(feature_classes[name])

    for name in sorted(feature_classes):
        if state[name] == 0:
            visit(name, [])

    return ordered


def load_features(
    settings: Settings | None = None,
    *,
    target: FeatureRegistry | None = None,
) -> FeatureRegistry:
    """发现并注册全部启用的功能，返回填好的注册表。

    参数
    ----
    settings:
        配置。为 None 时从环境变量读取。
    target:
        目标注册表。为 None 时使用全局单例（服务启动用）；
        测试里可传入一个干净实例，避免全局状态串味。

    流程
    ----
    1. 扫描 features/ 下的子包
    2. 逐个 import（模块在 import 时可能自行注册，但我们以第 3 步为准）
    3. 收集模块内定义的 Feature 子类
    4. 按配置过滤（白名单/黑名单）
    5. 拓扑排序
    6. 实例化、调用 setup()、注册进 registry
    """
    settings = settings or Settings.from_env()
    reg = target

    module_names = _discover_feature_modules()
    if not module_names:
        raise FeatureLoadError(
            f"在 {FEATURES_PACKAGE} 下没有发现任何功能模块（子包）。"
            "请至少保留一个功能目录，或参考 docs/EXTENDING.md 新建一个。"
        )

    # 收集所有功能的类，做**全局**依赖校验：即便某功能被禁用，
    # 依赖它的功能也应给出清晰提示，而不是抛"依赖缺失"让人困惑。
    all_classes: dict[str, type[Feature]] = {}
    for module_name in module_names:
        module = _import_module(module_name)
        for cls in _collect_feature_subclasses(module):
            if not cls.name:
                raise FeatureLoadError(
                    f"{module_name}.{cls.__name__} 缺少 `name` 属性，无法作为功能加载。"
                )
            if cls.name in all_classes:
                other = all_classes[cls.name].__qualname__
                raise FeatureLoadError(
                    f"功能名冲突：'{cls.name}' 同时出现在 {other} 和 {cls.__qualname__}。"
                    "功能名必须全局唯一。"
                )
            all_classes[cls.name] = cls

    # 按配置过滤
    enabled = {
        name: cls
        for name, cls in all_classes.items()
        if settings.is_feature_enabled(name)
    }
    skipped = sorted(set(all_classes) - set(enabled))
    if skipped:
        logger.info("按配置跳过的功能：%s", ", ".join(skipped))
    if not enabled:
        raise FeatureLoadError(
            "没有任何功能被加载。请检查 faster_mcp/features/ 下是否有模块，"
            "以及 FASTER_MCP_FEATURES / FASTER_MCP_DISABLED 是否把功能全屏蔽了。"
        )

    # 被禁用的功能如果被启用功能依赖，给出明确错误（而不是隐晦的 KeyError）
    for name, cls in enabled.items():
        for dep in cls.dependencies:
            if dep not in enabled:
                reason = (
                    "该依赖功能被禁用" if dep in all_classes else "该依赖功能不存在"
                )
                raise FeatureLoadError(
                    f"功能 '{name}' 依赖 '{dep}'，但{reason}。"
                    f"请通过环境变量 FASTER_MCP_FEATURES / FASTER_MCP_DISABLED 一并启用它。"
                )

    ordered = _sort_by_dependencies(enabled)

    for cls in ordered:
        feature = cls()
        # setup 在注册前调用：让它有机会准备资源，且失败时能早暴露
        try:
            feature.setup()
        except Exception as exc:  # noqa: BLE001
            raise FeatureLoadError(
                f"功能 '{cls.name}' 的 setup() 执行失败：{type(exc).__name__}: {exc}"
            ) from exc

        (reg or _global_registry()).register(feature)
        logger.debug("已加载功能：%s (%s)", cls.name, cls.__name__)

    return reg or _global_registry()


def _global_registry() -> FeatureRegistry:
    """延迟导入全局单例，避免模块级循环导入。"""
    from .registry import registry as _registry

    return _registry

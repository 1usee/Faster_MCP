"""功能注册表 —— 整个项目可扩展性的支点。

一句话原理
----------
功能模块在**被 import 的时候**，通过 `@feature_tool` 装饰器把函数登记进
一个进程内的全局注册表。启动时只需要遍历注册表并把工具挂到 MCP 实例上，
不需要任何"手写 import 清单"。

为什么要这么绕？
因为手写清单是扩展性的头号敌人：加功能要改两处（新文件 + 清单），
漏改就静默失效，还容易冲突。改成"import 即注册"后，新增功能只增加文件、
不修改文件，冲突面自然小，也更符合插件化直觉。

三类对象
--------
ToolSpec      一个可被模型调用的工具（名字、描述、参数 Schema、函数本体）。
Feature       一个功能模块（一组相关工具的集合，如"计算器"）。
FeatureRegistry  登记册，记录全部 Feature 与 Tool。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable

from .errors import FeatureLoadError


# --------------------------------------------------------------------------- #
# ToolSpec：一个工具的描述
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ToolSpec:
    """一个暴露给模型的工具。

    name / description / parameters 这三项会被序列化成 MCP 的 tool schema
    发给客户端，模型据此决定"什么时候调用、参数怎么填"。
    因此 description 的写法直接决定模型的调用准确率，务必写清
    "做什么 + 什么时候用 + 返回什么"。
    """

    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]
    feature_name: str
    # 是否作为"主工具"高亮（用于文档与自检输出，不影响协议）
    primary: bool = False

    def invoke(self, **kwargs: Any) -> Any:
        """调用工具本体。

        这里刻意不做参数校验——校验交给 MCP SDK 生成的入口函数，
        因为那里的校验基于类型注解，能给出模型看得懂的错误信息。
        registry 层只负责"存"和"取"，职责单一，便于测试替换。
        """
        return self.func(**kwargs)


# --------------------------------------------------------------------------- #
# Feature：一个功能模块
# --------------------------------------------------------------------------- #
class Feature:
    """功能模块基类。

    子类需要声明 `name`（唯一标识，也是白/黑名单里用的键）、
    `description`，并至少提供一个工具。

    工具的声明方式有两种，**推荐第一种**：

    1) 装饰器（推荐，零样板代码）

        class MyFeature(Feature):
            name = "my_feature"

            @feature_tool
            def my_tool(self, x: int) -> int:
                '''把 x 加一。'''
                return x + 1

       装饰器会自动：
         - 从类型注解生成 JSON Schema（模型据此填参数）
         - 从 docstring 取工具描述
         - 剔除 `self`，让签名符合 MCP 要求

    2) 显式声明（需要动态生成工具名、或复用同一函数时用）

        class MyFeature(Feature):
            name = "my_feature"

            def _impl(self, x: int) -> int:
                return x + 1

            def tools(self):
                return [ToolSpec(
                    name="my_tool",
                    description="把 x 加一。",
                    parameters={"type": "object",
                                "properties": {"x": {"type": "integer"}},
                                "required": ["x"]},
                    func=self._impl,
                    feature_name=self.name,
                )]
    """

    # ---- 子类必须覆盖 ---------------------------------------------------- #
    name: str = ""
    description: str = ""
    # 需要先加载的其它功能名。加载器据此做拓扑排序，并检测循环依赖。
    dependencies: tuple[str, ...] = ()
    version: str = "0.0.0"

    # 装饰器登记下来的方法（函数对象本身），按声明顺序保存。
    # 注意：这里存的是**未绑定**的函数，绑定发生在 registry 收集时。
    # 之所以不用 ToolSpec：ToolSpec 需要绑定后的签名，而类定义阶段拿不到实例。
    _declared_tools: list[Any] = []

    # ---- 生命周期钩子（可选覆盖） ---------------------------------------- #
    def setup(self) -> None:
        """功能被加载后、工具被挂载前调用一次。

        适合放"需要预先准备但不算工具"的事：打开连接池、加载数据字典、预编译正则。
        因为调用发生在注册阶段，所以**不要在这里做慢启动工作**（如网络请求），
        否则会拖慢客户端启动本服务的时间。
        """
        return None

    def teardown(self) -> None:
        """功能卸载时调用，用于释放 setup 申请的资源。默认什么都不做。"""
        return None

    # ---- 工具收集 -------------------------------------------------------- #
    def tools(self) -> list[ToolSpec]:
        """返回本功能提供的全部工具。

        默认返回空列表——装饰器风格的工具**不经过这里**，而是由 registry 扫
        `__mcp_tool__` 元数据自动收集（因为那需要实例，类定义阶段拿不到）。

        本方法专供**显式声明风格**使用：需要动态生成工具名、或把同一个函数
        以多个名字暴露时，覆盖本方法返回 ToolSpec 列表即可。
        显式声明的工具与装饰器风格可以混用；同名时显式声明优先。
        """
        return []

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """为每个子类准备**独立**的 _declared_tools 列表。

        这一行不做的话，所有子类会共享基类的那一个列表，
        导致 A 功能的工具出现在 B 功能里——是个隐蔽且难查的 bug。
        """
        super().__init_subclass__(**kwargs)
        cls._declared_tools = []


# --------------------------------------------------------------------------- #
# 装饰器：把方法变成工具
# --------------------------------------------------------------------------- #
# 类型注解 -> JSON Schema 的映射表。
# 只覆盖模型真正常用、且 MCP 客户端能稳定解析的类型；
# 复杂结构（dict/list）用 generic 映射，并在 docstring 里说明格式。
_TYPE_MAP: dict[Any, dict[str, Any]] = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
    list: {"type": "array"},
    dict: {"type": "object"},
}


def _schema_for_annotation(annotation: Any) -> dict[str, Any]:
    """把单个类型注解翻译成 JSON Schema 片段。

    默认值/可选/复杂类型都向后兼容：识别不了就退化成 string，
    宁可让模型多填一次，也不要因为 Schema 生成失败而丢掉整个工具。
    """
    if annotation is inspect.Parameter.empty or annotation is Any:
        return {"type": "string"}

    # 处理 str | None / Optional[str] / Union[...]
    import typing

    origin = typing.get_origin(annotation)
    if origin is typing.Union:  # Optional[X] 的底层就是 Union
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if args:
            schema = _schema_for_annotation(args[0])
            schema["nullable"] = True
            return schema

    if annotation in _TYPE_MAP:
        return dict(_TYPE_MAP[annotation])

    # Literal["a", "b"] -> enum，非常适合让模型"二选一"，能显著减少瞎填
    if origin is typing.Literal:
        values = list(typing.get_args(annotation))
        return {"type": "string", "enum": values}

    if origin in (list, set, tuple):
        return {"type": "array"}

    if origin is dict:
        return {"type": "object"}

    return {"type": "string"}


def _build_parameters(func: Callable[..., Any]) -> dict[str, Any]:
    """从函数签名生成 MCP 工具的 inputSchema。

    规则：
      - 跳过 `self`（功能方法绑定时会被剔除）
      - 有默认值的参数不进 required
      - docstring 里没提到参数含义时，用 "参数名: 类型" 兜底描述
    """
    signature = inspect.signature(func)
    properties: dict[str, Any] = {}
    required: list[str] = []

    for param_name, param in signature.parameters.items():
        if param_name in ("self", "cls"):
            continue

        # *args / **kwargs 无法表达为固定 Schema，直接不暴露（避免模型乱填）
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue

        schema = _schema_for_annotation(param.annotation)
        if param.default is inspect.Parameter.empty:
            required.append(param_name)
        elif param.default is not None:
            schema["default"] = param.default

        schema.setdefault("description", f"参数 {param_name}")
        properties[param_name] = schema

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


def feature_tool(func: Callable[..., Any] | None = None, *,
                 name: str | None = None,
                 description: str | None = None,
                 primary: bool = False) -> Callable[..., Any]:
    """把 Feature 的一个方法声明为 MCP 工具。

    用法：::

        class Calc(Feature):
            name = "calculator"

            @feature_tool
            def add(self, a: float, b: float) -> float:
                '''把两个数相加。'''
                return a + b

    参数
    ----
    name:
        覆盖工具名。默认用方法名。工具名是模型调用的键，建议用
        动词开头的 snake_case（如 `convert_units`）。
    description:
        覆盖工具描述。默认取 docstring 第一段。
        描述是模型选择工具的唯一依据，请写"做什么 + 何时用 + 返回什么"。
    primary:
        标记为功能的主工具，仅用于文档和自检输出的高亮。

    原理
    ----
    在**类定义阶段**记录一条 ToolSpec；真正绑定实例发生在 registry
    收集工具时。也就是说装饰器只做登记，不做绑定，因此同一个 Feature
    类可以被实例化多次而不互相污染。
    """

    def decorator(method: Callable[..., Any]) -> Callable[..., Any]:
        tool_name = name or method.__name__
        raw_doc = inspect.getdoc(method) or ""
        # 取 docstring 的第一段作为工具描述（后面的内容可以写实现细节给开发者看）
        tool_description = description or raw_doc.split("\n\n")[0].strip()

        # 关键：不要用 callable.__name__ 之外的方式去猜名字，
        # 这里把元数据挂在函数上，等 registry 收集时再组装 ToolSpec。
        # 附加 reason: 保持装饰器纯粹，才能在 tools() 覆盖时灵活替换。
        method.__mcp_tool__ = {  # type: ignore[attr-defined]
            "name": tool_name,
            "description": tool_description,
            "primary": primary,
        }
        return method

    # 支持 @feature_tool 和 @feature_tool(name="x") 两种写法
    if func is not None:
        return decorator(func)
    return decorator


# --------------------------------------------------------------------------- #
# FeatureRegistry：登记册
# --------------------------------------------------------------------------- #
class FeatureRegistry:
    """记录全部功能与工具，并在挂载前做一致性检查。

    使用顺序（由 server.py 驱动）：

        reg = FeatureRegistry()
        reg.register(CalculatorFeature())      # 加载阶段
        reg.finalize()                          # 校验阶段
        for spec in reg.iter_tools(): ...       # 挂载阶段
    """

    def __init__(self) -> None:
        self._features: dict[str, Feature] = {}
        self._tools: dict[str, ToolSpec] = {}
        self._finalized = False

    # ---- 注册 ------------------------------------------------------------ #
    def register(self, feature: Feature) -> None:
        """注册一个功能实例并收集它的工具。"""
        if self._finalized:
            raise FeatureLoadError(
                "注册表已冻结，不能再注册新功能。"
                "请确认所有功能都在 build_server() 之前完成注册。"
            )

        name = feature.name
        if not name:
            raise FeatureLoadError(
                f"{type(feature).__name__} 缺少 `name` 属性。"
                "每个 Feature 子类必须声明唯一的 name（会作为启停开关的键）。"
            )
        if name in self._features:
            existing = type(self._features[name]).__name__
            raise FeatureLoadError(
                f"功能名冲突：'{name}' 已被 {existing} 注册，"
                f"现在 {type(feature).__name__} 又想用同名。"
                "功能名必须全局唯一，请修改其中一个的 name。"
            )

        collected = self._collect_tools(feature)
        if not collected:
            raise FeatureLoadError(
                f"功能 '{name}' 没有提供任何工具。"
                "请在方法上加 @feature_tool 装饰器，或覆盖 tools() 方法。"
            )

        # 先校验工具名冲突，再落库，避免出现"注册了一半"的脏状态
        for spec in collected:
            if spec.name in self._tools:
                owner = self._tools[spec.name].feature_name
                raise FeatureLoadError(
                    f"工具名冲突：'{spec.name}' 已被功能 '{owner}' 占用，"
                    f"功能 '{name}' 又想用同名。"
                    "工具名会直接暴露给模型，必须全局唯一，请改名或加前缀。"
                )

        self._features[name] = feature
        for spec in collected:
            self._tools[spec.name] = spec

    def _collect_tools(self, feature: Feature) -> list[ToolSpec]:
        """把 Feature 上登记的方法组装成 ToolSpec 列表。

        分两条路：
          - 装饰器风格：扫类属性，找带 __mcp_tool__ 元数据的方法
          - 显式风格：调用 feature.tools() 拿现成的 ToolSpec
        两种方式可以混用；同名时**显式声明覆盖装饰器**。
        """
        specs: list[ToolSpec] = []

        # 先调用 tools() 拿到显式声明的工具，再扫装饰器登记的方法。
        # 顺序很重要：**显式声明优先**。如果某个工具名同时出现在 tools() 返回值和
        # 装饰器登记结果里，就采用 tools() 的那份（覆盖语义在下面用 seen 实现）。
        try:
            explicit = feature.tools()
        except Exception as exc:  # noqa: BLE001 - 转换成启动期错误，便于定位
            raise FeatureLoadError(
                f"功能 '{feature.name}' 的 tools() 执行失败：{exc}"
            ) from exc

        seen: set[str] = set()
        for spec in explicit:
            if not spec.name:
                raise FeatureLoadError(
                    f"功能 '{feature.name}' 声明了一个没有 name 的工具。"
                    "每个 ToolSpec 都必须有唯一的 name。"
                )
            if spec.name in seen:
                raise FeatureLoadError(
                    f"功能 '{feature.name}' 重复声明了工具 '{spec.name}'。"
                    "请检查 tools() 的返回值里是否有重名。"
                )
            seen.add(spec.name)
            # 用 replace 派生副本，而不是就地改 spec.feature_name。
            # 原因：tools() 允许返回模块级常量列表或缓存对象，就地改会污染
            # 别处的同一对象；派生副本则让每个注册表各自独立，天然隔离。
            specs.append(
                replace(spec, feature_name=spec.feature_name or feature.name)
            )

        for attr_name in dir(feature):
            if attr_name.startswith("__"):
                continue
            method = getattr(feature, attr_name, None)
            meta = getattr(method, "__mcp_tool__", None)
            if meta is None:
                continue
            if not callable(method):
                continue
            if meta["name"] in seen:
                # 已被显式声明覆盖，跳过
                continue

            specs.append(
                ToolSpec(
                    name=meta["name"],
                    description=meta["description"],
                    # 用绑定后的方法生成签名，这样 self 已被自动剔除
                    parameters=_build_parameters(method),
                    func=method,
                    feature_name=feature.name,
                    primary=bool(meta.get("primary", False)),
                )
            )
            seen.add(meta["name"])

        return specs

    # ---- 校验 ------------------------------------------------------------ #
    def finalize(self) -> None:
        """冻结注册表并做最终一致性检查（工具名唯一性已在注册时保证）。"""
        if not self._features:
            raise FeatureLoadError(
                "没有任何功能被加载。请检查 faster_mcp/features/ 下是否有模块，"
                "以及 FASTER_MCP_FEATURES / FASTER_MCP_DISABLED 是否把功能全屏蔽了。"
            )
        self._finalized = True

    # ---- 读取 ------------------------------------------------------------ #
    @property
    def features(self) -> dict[str, Feature]:
        return dict(self._features)

    @property
    def tools(self) -> dict[str, ToolSpec]:
        return dict(self._tools)

    def iter_tools(self) -> Iterable[ToolSpec]:
        """按功能名排序后产出工具，保证每次启动的注册顺序稳定可复现。"""
        for feature_name in sorted(self._features):
            for spec in self._tools.values():
                if spec.feature_name == feature_name:
                    yield spec

    def tools_of(self, feature_name: str) -> list[ToolSpec]:
        """取某个功能下的全部工具。供自检脚本与将来的文档生成器使用。"""
        return [s for s in self._tools.values() if s.feature_name == feature_name]

    def find_feature_of_tool(self, tool_name: str) -> str | None:
        """工具 -> 所属功能名，便于日志与错误信息定位。"""
        spec = self._tools.get(tool_name)
        return spec.feature_name if spec else None

    def summary(self) -> str:
        """人类可读的概览，供自检脚本和日志使用。"""
        lines = [f"已加载 {len(self._features)} 个功能，共 {len(self._tools)} 个工具："]
        for spec in self.iter_tools():
            mark = "*" if spec.primary else " "
            lines.append(f"  [{mark}] {spec.name:<28} ({spec.feature_name})")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 模块级单例
# --------------------------------------------------------------------------- #
# 为什么用单例：装饰器在 import 阶段执行，此时没有 registry 实例可传参，
# 只能写入一个全局登记册。loader 保证同一进程只会构建一次 server，
# 因此不存在多实例互相污染的问题。
registry = FeatureRegistry()


def register_feature(feature: Feature, *, into: FeatureRegistry | None = None) -> None:
    """把功能实例登记进指定（默认全局）注册表。

    除了由 loader 自动加载外，也支持手工注册——这在测试里非常有用：
    可以只装一个假功能，不依赖磁盘上的真实模块。
    """
    (into or registry).register(feature)

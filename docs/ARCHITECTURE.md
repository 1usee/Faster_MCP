"""架构图解 —— 一页看懂功能是怎么从目录变成模型可调用的工具的。

为什么把这段放在 docs/ 而不是 README：
README 讲"怎么用"，这里讲"为什么这样设计"。需要改架构时看这里。
"""

# 一、启动时发生了什么
#
#   python -m faster_mcp
#         │
#         ├─ config.Settings.from_env()        读环境变量（服务名 / 功能启停 / 日志级别）
#         │
#         ├─ loader.load_features(settings)
#         │     │  ★ 返回值是填好的 FeatureRegistry，不是 None：
#         │     │     不传 target 时写入模块级全局单例（服务启动用）；
#         │     │     传 target=自建实例可写入它（写测试时用，避免全局状态串味）。
#         │     │
#         │     ├─ pkgutil.iter_modules(features/)     扫描 features/ 下所有子包
#         │     │        ↓ 找到 ["calculator", ...]
#         │     │
#         │     ├─ importlib.import_module(...)         逐个导入
#         │     │        ↓ import 时装饰器执行
#         │     │        └─ @feature_tool 把函数元数据挂到方法上（!! 只挂元数据，不注册 !!）
#         │     │
#         │     ├─ _collect_feature_subclasses(module)  取出模块内定义的 Feature 子类
#         │     ├─ 按配置过滤（白名单 / 黑名单）
#         │     ├─ _sort_by_dependencies(...)            拓扑排序 + 环检测
#         │     └─ registry.register(feature)            实例化 → setup() → 收集 ToolSpec → 登记
#         │
#         ├─ registry.finalize()               冻结并校验（至少一个功能）
#         │
#         ├─ server.mount_tools(mcp, registry)
#         │     └─ 对每个 ToolSpec：
#         │          包一层错误处理壳（保留原签名，这样 SDK 能生成正确的 inputSchema）
#         │          → mcp.add_tool(wrapper, name=..., description=...)
#         │
#         └─ mcp.run()                         进入 MCP 协议循环（stdio 或 streamable-http）
#
#   关键观察：上面这条链路里**没有任何一处提到具体功能名**。
#   唯一的"具体"是 features/ 目录里的子包，而它们是被扫描发现的，不是被写死的。
#   ── 这就是"加功能只增加文件、不修改文件"能够成立的完整理由。

# 二、一次工具调用发生了什么
#
#   模型决定调用 calculate(expression="(1200*1.13)/4")
#         │
#         └─ MCP 客户端 → JSON-RPC → 本服务
#               │
#               └─ mount_tools 时包好的 wrapper(*args, **kwargs)
#                     │
#                     ├─ logging.debug 记录入参（排查"模型到底传了什么"）
#                     │
#                     └─ spec.func(**kwargs)   ← 真正的 Feature 方法
#                           │
#                           ├─ engine.compute(expression)
#                           │     ├─ safe_eval.evaluate  → AST 白名单校验 → 隔离命名空间求值
#                           │     ├─ format_value        → 人类可读化（顺便收敛浮点误差）
#                           │     └─ _collect_values     → 收集中间变量
#                           │
#                           └─ 返回 dict（JSON 可序列化）
#                                 │
#         ┌───────────────────────┴──────────────────────────────┐
#         │ 正常返回 → 原样回给模型                                │
#         │ 抛 ToolInputError → 转成 {ok:false, error_type:        │
#         │                     "invalid_input", message, hint}   │
#         │                    —— 不抛协议错误，让模型能读到原因   │
#         │                    并自行改正参数                      │
#         │ 抛其它异常 → 记 traceback（只进日志），回给模型一个    │
#         │             简洁的 execution_error，不泄内部细节       │
#         └───────────────────────────────────────────────────────┘

# 三、新增一个功能要动的文件（对比）
#
#   ┌───────────────────────────────┬───────────────┬───────────────┐
#   │ 文件                           │ 手写清单式     │ 本项目式       │
#   ├───────────────────────────────┼───────────────┼───────────────┤
#   │ features/new/__init__.py       │ 新建          │ 新建 ✅        │
#   │ features/new/feature.py        │ 新建          │ 新建 ✅        │
#   │ server.py                      │ **必须修改**  │ 不动 ✅        │
#   │ 某个注册清单                   │ **必须修改**  │ 不存在 ✅      │
#   │ 测试                           │ 新建          │ 新建 ✅        │
#   └───────────────────────────────┴───────────────┴───────────────┘
#
#   "不改既有文件"不只是省事：它意味着加功能**不会与别人的改动冲突**，
#   也不会因为漏改一处清单而静默失效。这在多人协作或长期演进时价值最大。

# 四、为什么选 FastMCP 而不是裸 SDK
#
#   裸 SDK：手写 JSON Schema、手写请求分发、手写参数解析。加一个工具约 30~50 行样板。
#   FastMCP：@mcp.tool 装饰器从类型注解生成 Schema，从 docstring 取描述。
#   本项目在 FastMCP 之上又加了一层 registry，是为了拿到
#   **运行期的工具清单**（FastMCP 自己只管注册，不提供"遍历所有工具"的稳定接口）。
#   有了清单，才能做：启停开关、能力自检、文档生成、未来的动态工具集。
#
#   一句话：FastMCP 负责"挂载"，registry 负责"账本"。账本是可扩展性的前提。

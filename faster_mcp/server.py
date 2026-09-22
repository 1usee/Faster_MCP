"""服务入口 —— 把注册表里的工具挂到 MCP 实例上并启动。

为什么入口这么薄
----------------
本文件只做四件事：读配置 → 加载功能 → 挂载工具 → 运行。
任何"某个功能具体怎么实现"的知识都不应该出现在这里。理由：
一旦入口知道了具体功能，加功能就必须改入口，可扩展性就破了。
本文件的代码量应当与功能数量**无关**——这是判断架构是否解耦的实用标准。

挂载机制（关键的十几行）
------------------------
FastMCP 用 `@mcp.tool` 装饰器注册工具。我们手里是一份运行期的 ToolSpec 列表，
没法用装饰器语法，所以用 `mcp.add_tool(fn, name=..., description=...)` 动态挂载。
每个工具都要包一层壳：壳的作用有两个——
  1. 把 ToolInputError 转成对模型友好的返回值（而不是让它看到 traceback）
  2. 记录调用日志，便于排查"模型到底传了什么"

注意：包装函数的签名必须**逐参数展开**，不能写成 `def wrapper(**kwargs)`。
因为 MCP SDK 是从函数签名生成输入 Schema 的，`**kwargs` 生成不出有效 Schema，
模型就看不到参数说明了。所以这里按参数名动态构造签名。
"""

from __future__ import annotations

import argparse
import inspect
import logging
import sys
from typing import Any, Callable

from .config import Settings
from .errors import FasterMcpError, ToolExecutionError, ToolInputError
from .loader import load_features
from .registry import FeatureRegistry, ToolSpec

logger = logging.getLogger("faster_mcp")


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
def setup_logging(level: str = "INFO") -> None:
    """配置日志输出到 stderr。

    为什么必须写 stderr：stdio 传输下 stdout 承载 JSON-RPC 协议流，
    任何非协议内容混进去都会让客户端解析失败。这是 MCP 服务端最常见的低级错误。
    """
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


# --------------------------------------------------------------------------- #
# 工具包装
# --------------------------------------------------------------------------- #
def _make_wrapper(spec: ToolSpec) -> Callable[..., Any]:
    """给 ToolSpec 包一层错误处理壳，并保留原始签名。

    实现要点：
      - 用 inspect.signature 读出 ToolSpec.func 的参数
      - 动态生成一个签名相同的函数（这样 SDK 能生成正确的 inputSchema）
      - 内部调用真实函数，统一处理异常
    """
    original = spec.func
    signature = inspect.signature(original)

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        logger.debug("调用工具 %s，参数=%s", spec.name, kwargs or args)
        try:
            return original(*args, **kwargs)
        except ToolInputError as exc:
            # 模型可自愈类错误：返回结构化错误，让模型看到原因并改写调用。
            # 不抛出异常，因为抛异常会变成协议层错误，模型更难利用其中的信息。
            logger.info("工具 %s 输入不合法：%s", spec.name, exc)
            return {
                "ok": False,
                "error_type": "invalid_input",
                "message": str(exc),
                "hint": "请根据 message 修正参数后重试。",
            }
        except FasterMcpError as exc:
            logger.warning("工具 %s 执行失败：%s", spec.name, exc)
            return {
                "ok": False,
                "error_type": "execution_error",
                "message": str(exc),
            }
        except Exception as exc:  # noqa: BLE001 - 兜底，绝不让 traceback 泄给模型
            logger.exception("工具 %s 出现未预期的异常", spec.name)
            raise ToolExecutionError(
                f"工具 '{spec.name}' 内部错误：{type(exc).__name__}: {exc}"
            ) from exc

    # 关键：把原函数的签名和文档挂到壳上，SDK 据此生成 Schema。
    wrapper.__signature__ = signature  # type: ignore[attr-defined]
    wrapper.__name__ = spec.name
    wrapper.__doc__ = spec.description
    wrapper.__annotations__ = getattr(original, "__annotations__", {})
    return wrapper


def mount_tools(mcp: Any, registry: FeatureRegistry) -> int:
    """把注册表里的全部工具挂到 FastMCP 实例上，返回挂载数量。

    解耦检查点：本函数不知道任何具体的工具名或参数，
    它只认识 ToolSpec 这个通用结构。因此新增功能时本函数无需改动。
    """
    count = 0
    for spec in registry.iter_tools():
        mcp.add_tool(
            _make_wrapper(spec),
            name=spec.name,
            description=spec.description,
        )
        count += 1
    return count


# --------------------------------------------------------------------------- #
# 构建服务
# --------------------------------------------------------------------------- #
def build_server(settings: Settings | None = None) -> tuple[Any, FeatureRegistry]:
    """构建 MCP 服务实例。

    返回 (mcp 实例, 注册表)。拆出来是为了可测试：测试里可以构建服务后
    直接检查注册表，或对工具函数做内存调用，不必真的跑起一个进程。
    """
    settings = settings or Settings.from_env()

    # 延迟导入：让本模块在没装 mcp 的环境里也能被导入（例如只跑纯逻辑测试）
    from mcp.server.fastmcp import FastMCP

    registry = load_features(settings)
    registry.finalize()

    mcp = FastMCP(settings.server_name)
    mounted = mount_tools(mcp, registry)

    logger.info(
        "服务 '%s' 就绪：%d 个功能，%d 个工具",
        settings.server_name,
        len(registry.features),
        mounted,
    )
    logger.debug("\n%s", registry.summary())

    return mcp, registry


# --------------------------------------------------------------------------- #
# 命令行入口
# --------------------------------------------------------------------------- #
def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="faster-mcp",
        description="多功能 MCP 服务：为大模型提供计算器等工具。",
    )
    parser.add_argument(
        "--http", action="store_true",
        help="使用 HTTP 传输（默认 stdio，适合被客户端以子进程方式启动）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP 监听地址")
    parser.add_argument("--port", type=int, default=8000, help="HTTP 监听端口")
    parser.add_argument(
        "--list", action="store_true",
        help="只打印已加载的功能与工具清单，然后退出（不启动服务）",
    )
    parser.add_argument(
        "--log-level", default=None,
        help="日志级别：DEBUG/INFO/WARNING/ERROR（默认取环境变量）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """命令行入口。被 pyproject 的 console script 与本模块 __main__ 共用。"""
    args = _parse_args(argv)
    settings = Settings.from_env()
    if args.log_level:
        settings.log_level = args.log_level

    setup_logging(settings.log_level)

    if args.list:
        # 自检模式：只加载并打印，不启动协议循环。
        # 开发新功能时用这个命令最快看到结果。
        registry = load_features(settings)
        registry.finalize()
        print(registry.summary())
        return 0

    mcp, _ = build_server(settings)

    if args.http:
        # HTTP 传输需要额外配置 host/port。不同 SDK 版本的字段名略有差异，
        # 因此这里做兼容处理：能设置就设置，不能则退回默认值并提示。
        try:
            mcp.settings.host = args.host
            mcp.settings.port = args.port
        except Exception:  # noqa: BLE001
            logger.warning("当前 SDK 版本不支持在运行时改 host/port，将使用默认值。")
        mcp.run(transport="streamable-http")
    else:
        mcp.run()  # 默认 stdio

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Faster MCP —— 为大模型提供可插拔工具的多功能 MCP 服务。

包的对外门面极小：只暴露版本号与"构建服务"的入口。
真正的组装逻辑在 faster_mcp.server 里，功能模块在 faster_mcp.features 下。

加新功能时**不需要改这个文件**。
"""

__version__ = "0.1.0"
__all__ = ["__version__"]

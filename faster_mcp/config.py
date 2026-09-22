"""运行配置。

设计原则：**一切可配置项集中在这里，其它模块不直接读环境变量。**
好处是加新配置时只改一处，并且可以在测试里构造一个显式的 Config 对象，
不必污染进程环境。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# 环境变量前缀，避免与宿主环境里的同名变量冲突
ENV_PREFIX = "FASTER_MCP_"

ENV_NAME = ENV_PREFIX + "NAME"
ENV_FEATURES = ENV_PREFIX + "FEATURES"
ENV_DISABLED = ENV_PREFIX + "DISABLED"
ENV_LOG_LEVEL = ENV_PREFIX + "LOG_LEVEL"


def _split_csv(raw: str | None) -> list[str]:
    """把 'a, b,,c' 解析成 ['a', 'b', 'c']。空字符串视为空列表。"""
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(slots=True)
class Settings:
    """服务配置。

    属性
    ----
    server_name:
        暴露给 MCP 客户端的服务名。会显示在客户端的服务列表里。
    enabled_features:
        功能白名单。包含 "*" 或为空表示"全部启用"。
        用于按需瘦身：只给模型暴露它当前需要的工具，能显著提升模型的工具选择准确率。
    disabled_features:
        功能黑名单，优先级高于白名单。用于临时关掉某个出问题的功能而不用改代码。
    log_level:
        日志级别（DEBUG/INFO/WARNING/ERROR），写到 stderr。
        注意：stdio 传输下 stdout 被协议占用，**任何日志都不能打到 stdout**，
        否则会污染 JSON-RPC 消息流。日志统一走 stderr。
    """

    server_name: str = "Faster MCP"
    enabled_features: list[str] = field(default_factory=lambda: ["*"])
    disabled_features: list[str] = field(default_factory=list)
    log_level: str = "INFO"

    def is_feature_enabled(self, feature_name: str) -> bool:
        """判断某个功能是否应当被启用（黑名单优先）。"""
        if feature_name in self.disabled_features:
            return False
        if not self.enabled_features or "*" in self.enabled_features:
            return True
        return feature_name in self.enabled_features

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        """从环境变量构造配置。传入 env 便于测试注入。"""
        source = os.environ if env is None else env

        enabled = _split_csv(source.get(ENV_FEATURES))
        return cls(
            server_name=source.get(ENV_NAME, "Faster MCP"),
            enabled_features=enabled or ["*"],
            disabled_features=_split_csv(source.get(ENV_DISABLED)),
            log_level=source.get(ENV_LOG_LEVEL, "INFO").upper(),
        )

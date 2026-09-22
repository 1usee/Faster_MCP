"""供功能模块直接导入的公共出口。

功能模块里统一写 `from ..base import Feature, feature_tool, ToolSpec`，
好处是将来这些类挪位置时，只需要改这一个转发文件，所有功能不用动。
这是"稳定接口层"的常见做法。
"""

from ..registry import Feature, ToolSpec, feature_tool, register_feature

__all__ = ["Feature", "ToolSpec", "feature_tool", "register_feature"]

"""时间与日期功能模块。"""

from __future__ import annotations

from datetime import datetime, timezone

from ...errors import ToolInputError
from ..base import Feature, feature_tool


class DatetimeFeature(Feature):
    """为模型提供时间与日期计算能力。"""

    name = "datetime"
    description = "时间获取与日期计算工具集，用于查询当前时间和计算日期差值。"
    version = "0.1.0"

    @feature_tool(primary=True)
    def now_utc(self) -> dict[str, str]:
        """返回当前 UTC 时间，适合在需要知道“现在是什么时刻”时调用。

        返回 timestamp（ISO 8601）和 timezone（UTC）两项字段。
        """
        now = datetime.now(timezone.utc)
        return {
            "timestamp": now.isoformat(),
            "timezone": "UTC",
        }

    @feature_tool
    def days_between(self, start_date: str, end_date: str) -> dict[str, int]:
        """计算两个日期之间的天数差值。

        参数 start_date 与 end_date 必须为 YYYY-MM-DD。返回 days（end - start）和 abs_days（绝对值）。
        """
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ToolInputError(
                f"日期格式应为 YYYY-MM-DD，收到 start_date={start_date!r}, end_date={end_date!r}。"
            ) from exc

        delta = (end - start).days
        return {"days": delta, "abs_days": abs(delta)}

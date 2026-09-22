from __future__ import annotations

import pytest

from faster_mcp.errors import ToolInputError
from faster_mcp.features.datetime import DatetimeFeature
from faster_mcp.registry import FeatureRegistry


@pytest.fixture
def feature() -> DatetimeFeature:
    instance = DatetimeFeature()
    instance.setup()
    return instance


def test_feature_registers_cleanly() -> None:
    reg = FeatureRegistry()
    reg.register(DatetimeFeature())
    reg.finalize()

    expected = {"now_utc", "days_between"}
    assert expected <= set(reg.tools)


def test_now_utc_returns_iso8601(feature: DatetimeFeature) -> None:
    payload = feature.now_utc()
    assert payload["timezone"] == "UTC"
    assert "T" in payload["timestamp"]
    assert payload["timestamp"].endswith("+00:00")


def test_days_between(feature: DatetimeFeature) -> None:
    result = feature.days_between("2024-01-01", "2024-03-01")
    assert result["days"] == 60
    assert result["abs_days"] == 60


def test_days_between_rejects_bad_format(feature: DatetimeFeature) -> None:
    with pytest.raises(ToolInputError):
        feature.days_between("2024/01/01", "2024-03-01")

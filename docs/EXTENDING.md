# 新功能开发指南

这份文档的目标只有一个：**让你（或未来的你、或另一个模型）在半小时内加好一个新功能，
并且不用读核心代码、不用改核心代码。**

---

## 1. 先理解三件事

### 1.1 一个功能 = 一个目录

```
faster_mcp/features/<功能名>/
```

目录名就是功能模块名。`loader.py` 用 `pkgutil.iter_modules` 扫描 `features/` 下**所有子包**，
所以新建目录 = 新增功能。**没有任何清单文件需要修改**，这是本项目最重要的设计约束。

### 1.2 注册发生在 import 阶段

功能模块被 import 时，`@feature_tool` 装饰器把函数登记到类上；loader 收集类、实例化、
注册进 `FeatureRegistry`；`server.py` 遍历注册表，动态挂载到 MCP 实例。

```
新目录 → loader 自动 import → 装饰器登记 → 依赖排序 → 注册 → 挂载 → 模型可见
        ↑ 你完全不用管这一段 ↑
```

### 1.3 三层分工（照着分，将来不会乱）

计算器功能就是按这个分法的现成例子：

| 层 | 计算器里的文件 | 职责 | 变更频率 |
|---|---|---|---|
| 工具层 | `feature.py` | 工具名、描述、参数校验、把结果包成 payload | 中 |
| 业务层 | `engine.py` | 结果格式化、单位换算表、把引擎结果整理成结构体 | 低 |
| 引擎层 | `safe_eval.py` | 通用算法/基础设施（AST 白名单求值器） | 低 |

不是每个功能都需要三层。小功能可以只有 `feature.py` 一层；
但只要出现"纯计算逻辑"，就该抽到单独的模块（如 `engine.py`），
而不是塞进工具函数里。

好处很实际：**下面两层的测试不需要启动 MCP 服务**，跑得快、好调试。
`tests/test_safe_eval.py` 和 `tests/test_calculator.py` 就分别对准了引擎层和工具层。

---

## 2. 三步加一个功能

### 第 1 步：建目录

```
faster_mcp/features/datetime/
├─ __init__.py     # 导出 Feature 子类（loader 靠它发现功能）
└─ feature.py      # 定义 Feature 子类
```

### 第 2 步：写 `__init__.py`

```python
"""时间与日期工具。"""

from .feature import DatetimeFeature

__all__ = ["DatetimeFeature"]
```

### 第 3 步：写 `feature.py`

```python
"""时间与日期功能模块。"""

from __future__ import annotations

from datetime import datetime, timezone

from ..base import Feature, feature_tool


class DatetimeFeature(Feature):
    """为模型提供时间与日期计算能力。"""

    name = "datetime"                 # 全局唯一，也是启停开关的键
    description = "时间获取与日期计算工具集。"
    version = "0.1.0"
    # dependencies = ("calculator",)  # 需要复用别的功能时写在这里

    @feature_tool(primary=True)
    def now_utc(self) -> str:
        """返回当前 UTC 时间（ISO 8601 格式）。需要知道"现在"时调用。"""
        return datetime.now(timezone.utc).isoformat()

    @feature_tool
    def days_between(self, start_date: str, end_date: str) -> dict:
        """计算两个日期之间的天数差。

        参数 start_date 与 end_date 格式为 YYYY-MM-DD。
        返回 days（天数，end - start）与 abs_days（绝对值）。
        """
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError as exc:
            from ...errors import ToolInputError
            raise ToolInputError(
                f"日期格式应为 YYYY-MM-DD，收到 start_date={start_date!r}, "
                f"end_date={end_date!r}（{exc}）。"
            ) from exc
        delta = (end - start).days
        return {"days": delta, "abs_days": abs(delta)}
```

### 第 4 步：验证

```powershell
python -m faster_mcp --list      # 看看工具是否出现
python scripts\smoke_test.py     # 完整自检
```

**完成。** 你没有修改除了新增文件之外的任何东西。

---

## 3. 工具写作规范

### 3.1 工具描述是给模型看的，不是给同事看的

模型**只**依据 `工具名 + description + 参数 Schema` 决定是否调用。所以描述必须回答三个问题：

| 问题 | 反面例子 | 正面例子 |
|---|---|---|
| 做什么 | `"算东西"` | `"计算一个数学表达式并返回精确结果。"` |
| 何时用 | （缺失） | `"需要做算术、百分比、幂运算时调用，不要自己心算。"` |
| 返回什么 | （缺失） | `"返回 result（格式化结果）、raw_value（原始数值）等字段。"` |

**"不要自己心算"这类引导语很有用**——模型的默认倾向是自己算，明确告诉它外包给工具能显著提高调用率。

### 3.2 参数用类型注解，别用 `**kwargs`

Schema 从签名生成。`**kwargs` 生成不出有效 Schema，模型会觉得这个工具没参数可填。

```python
# 好：Schema 自动生成，模型看得到参数名和类型
@feature_tool
def convert_units(self, value: float, from_unit: str, to_unit: str) -> dict: ...

# 差：SDK 无法生成参数说明
@feature_tool
def convert_units(self, **kwargs) -> dict: ...
```

### 3.3 用 `Literal` 让模型做选择题

取值有限的参数用 `Literal`，会自动变成 JSON Schema 的 `enum`，能大幅减少瞎填：

```python
from typing import Literal

@feature_tool
def format_number(self, value: float, style: Literal["plain", "scientific", "percent"]) -> str:
    """按指定风格格式化数字。style 可选 plain / scientific / percent。"""
```

### 3.4 错误信息要写成"修复指南"

抛 `ToolInputError` 时，错误文本会直接进模型上下文，是它自我纠正的唯一线索。

```python
# 差：模型不知道怎么办
raise ToolInputError("参数错误")

# 好：模型能直接照做
raise ToolInputError(
    f"未知单位 '{from_unit}'。支持 length / mass / volume / time / data / temperature "
    "六类，例如 km、lb、gal、min、gb、c。"
)
```

**诀窍：在错误信息里列出所有合法取值。** 这一步能省掉一整轮"模型试探 → 服务再报错"的往返。

### 3.5 返回值优先用 dict

模型从结构化字段里取数字，比从长字符串里"找"数字可靠得多。

```python
# 好
return {"result": 339.0, "currency": "CNY", "rounded": 339}

# 可用但不理想
return "结果是 339 元"
```

---

## 4. 可用的基类 API

继承 `Feature`（从 `..base` 导入）后可用：

| 成员 | 说明 |
|---|---|
| `name` | **必填**。功能唯一标识，也是 `FASTER_MCP_FEATURES` / `FASTER_MCP_DISABLED` 里用的键 |
| `description` | 功能说明，出现在自检输出与文档 |
| `version` | 语义化版本，便于排查"线上跑的是哪版" |
| `dependencies` | 依赖的其它功能名元组，加载器自动拓扑排序并检测环 |
| `setup()` | 加载后调用一次。放轻量的预准备；**别做网络请求或慢 IO** |
| `teardown()` | 卸载时调用，释放 `setup` 申请的资源 |
| `tools()` | 高级用法：覆盖它可用代码动态生成工具列表（见 registry.py 文档） |

> **`setup()` 的注意事项**：它在客户端启动服务时同步执行。
> 如果里面卡了 5 秒，用户就能感觉到客户端卡了 5 秒。重活请放到第一次工具调用时做惰性初始化。

---

## 5. 依赖其它功能

```python
class FinanceFeature(Feature):
    name = "finance"
    dependencies = ("calculator",)   # 先加载 calculator
```

加载器会保证 `calculator` 先被实例化。它同时会：
- 检测**循环依赖**并给出完整调用链，例如 `a -> b -> c -> a`
- 检测**缺失依赖**并提示是"被禁用"还是"不存在"，附带修复建议

若要调用另一个功能的能力，推荐**直接 import 它的底层模块**（如 `from ..calculator import engine`），
而不是去注册表里捞实例——后者会引入运行期耦合，前者只是普通的代码复用。

---

## 6. 测试新功能

在 `tests/` 下加一个文件，照抄这个两层结构：

```python
"""datetime 功能的测试。"""
import pytest

from faster_mcp.errors import ToolInputError
from faster_mcp.registry import FeatureRegistry
from faster_mcp.features.datetime import DatetimeFeature


@pytest.fixture
def feature():
    f = DatetimeFeature()
    f.setup()
    return f


def test_feature_registers_without_conflict():
    """工具能被注册，且不污染其它功能的工具名空间。"""
    reg = FeatureRegistry()
    reg.register(feature=DatetimeFeature())
    reg.finalize()
    assert "days_between" in reg.tools


def test_days_between(feature):
    result = feature.days_between("2024-01-01", "2024-03-01")
    assert result["days"] == 60


def test_days_between_rejects_bad_format(feature):
    with pytest.raises(ToolInputError):
        feature.days_between("2024/01/01", "2024-03-01")
```

**为什么要写"注册不冲突"这条测试**：它一次性验证了三件事——装饰器生效、名字没撞车、
Schema 生成没崩。这是新功能最容易踩的三个坑。

因为 `FeatureRegistry` 可以独立实例化，测试之间天然隔离，不会互相干扰。

---

## 7. 常见坑（按踩到的频率排序）

**① 忘了在 `__init__.py` 里导出**

现象：`--list` 看不到你的工具。
原因：loader 只认"定义了 Feature 子类且 `__module__` 等于该模块"的类，
而 `__init__.py` 没导入 `feature.py` 时，那个类根本不存在。

**② 工具名撞车**

现象：启动时报 `工具名冲突`。
原因：工具名是全局命名空间，暴露给模型。加个功能前缀最省事：
`report_avg` 而不是 `avg`。

**③ 日志打到了 stdout**

现象：客户端连上就断，或者报 JSON 解析错误。
原因：stdio 传输下 **stdout 是协议通道**。任何 `print()` 都会污染它。
请用 `logging`（本项目的 `setup_logging` 已把日志定向到 stderr），
或者 `print(..., file=sys.stderr)`。

**④ 在工具里抛了非 `FasterMcpError` 异常**

现象：客户端收到 `execution_error`，模型的调用体验变差。
原因：只有 `ToolInputError` 会被转成"模型可自愈"的结构化响应。
参数问题请主动抛 `ToolInputError`，别让 `KeyError` 裸奔出去。

**⑤ 返回了不可序列化的对象**

现象：调用成功但客户端报序列化错误。
原因：MCP 要求返回 JSON 可序列化内容。返回 `dict` / `list` / `str` / 数字 / `bool` /
`None`；要返回自定义对象就先转成 dict（参考 `CalculationResult.to_payload()`）。

**⑥ 在 `setup()` 里做重活**

现象：客户端启动变慢。
原因：`setup()` 是同步的、在启动路径上。重活请惰性化。

**⑦ 依赖成环**

现象：启动报 `功能依赖出现循环`，并打印完整链条。
修法：把双方都需要的逻辑抽到第三个功能里，让它们都依赖那个功能，而不是互相依赖。

---

## 8. 检查清单

加完功能，逐条打勾：

- [ ] 新建了 `features/<名字>/` 目录，含 `__init__.py` 和 `feature.py`
- [ ] `__init__.py` 里导出了 Feature 子类
- [ ] `Feature.name` 已设置且全局唯一
- [ ] 每个工具都有 docstring，且回答了"做什么/何时用/返回什么"
- [ ] 参数都有类型注解，没有 `**kwargs`
- [ ] 参数校验失败的路径抛 `ToolInputError`，错误信息里列出了合法取值
- [ ] 返回值是 JSON 可序列化的基本类型
- [ ] 没有向 stdout 打印任何东西
- [ ] `python -m faster_mcp --list` 能看到新工具
- [ ] `python scripts\smoke_test.py` 通过
- [ ] `pytest` 通过（含"注册不冲突"那条测试）
- [ ] 若依赖其它功能，`dependencies` 已声明且无环

---

## 9. 可选：让功能默认处于关闭状态

有时你想先把功能加进来、但默认不暴露给模型。做法：把 `name` 写进环境变量
`FASTER_MCP_DISABLED`（黑名单）即可，**不需要改代码**：

```powershell
$env:FASTER_MCP_DISABLED = "datetime"
python -m faster_mcp --list
```

启用时反过来用白名单：

```powershell
$env:FASTER_MCP_FEATURES = "calculator,datetime"
```

**为什么这个机制重要**：功能越多的 MCP 服务，模型的"选错工具"概率越高。
按场景给模型装配不同的工具组合（比如数据任务只开 `calculator,statistics`，
日程任务只开 `datetime`），通常比一次性塞进全部工具效果更好。
这也是本架构把功能做成可启停单元、而非写死在一个大模块里的实际原因。

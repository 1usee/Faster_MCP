# 多功能 MCP 服务（Faster MCP）

为大模型提供一个可随时调用的工具服务。**第一个功能是计算器**（让模型在需要精确计算时直接调用，而不是自己"心算"出错），
但整个项目是按**插件式可扩展架构**搭的：以后每加一个能力，只需新增一个模块文件，不用碰核心代码。

---

## 目录结构

```
Faster_MCP/
├─ faster_mcp/                     # 包本体
│  ├─ __init__.py
│  ├─ __main__.py                  # 支持 python -m faster_mcp 启动
│  ├─ server.py                    # ★ 唯一入口：组装 + 启动
│  ├─ config.py                    # 运行配置（环境变量 / .env 风格）
│  ├─ registry.py                  # ★ 插件注册表（核心机制）
│  ├─ loader.py                    # ★ 自动发现并加载 features/ 下的模块
│  ├─ errors.py                    # 统一异常类型
│  └─ features/                    # ★ 功能模块目录（新功能加在这里）
│     ├─ __init__.py
│     ├─ base.py                   # Feature 基类 / 注册装饰器
│     └─ calculator/               # 计算器功能（第一个功能）
│        ├─ __init__.py
│        ├─ feature.py             # 工具注册
│        ├─ engine.py              # 计算引擎（纯函数，可独立测试）
│        └─ safe_eval.py           # 安全表达式求值器（AST 白名单）
├─ tests/                          # 测试
│  ├─ test_safe_eval.py
│  └─ test_calculator.py
├─ docs/
│  └─ EXTENDING.md                 # ★ 新功能开发指南（加功能前必看）
├─ examples/
│  └─ mcp_config_example.json      # 各类 MCP 客户端的接入配置示例
├─ scripts/
│  └─ smoke_test.py                # 不装客户端的自检脚本
├─ pyproject.toml
├─ requirements.txt
├─ .gitignore
└─ README.md                       # 本文件
```

标 ★ 的是理解本项目的关键文件。

---

## 快速开始

### 1. 安装依赖

```powershell
# 建议虚拟环境
python -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
# 或者把本包以开发模式装上（会获得 faster_mcp 包和 faster-mcp 命令）
pip install -e .
```

### 2. 自检（不需要 MCP 客户端）

```powershell
python scripts\smoke_test.py
```

它会加载全部功能模块，打印出注册了哪些工具、每个工具的参数结构，并实测几个计算用例。

### 3. 启动服务

```powershell
python -m faster_mcp
# 或（pip install -e . 之后）
faster-mcp
```

默认使用 **stdio** 传输（MCP 客户端以子进程方式启动本服务时的标准做法）。
需要跨进程/远程访问时，用 `--http` 启动 HTTP 传输：

```powershell
python -m faster_mcp --http --host 127.0.0.1 --port 8000
```

### 4. 接入到 MCP 客户端

把 `examples/mcp_config_example.json` 里的配置片段贴到你客户端的 MCP 配置中即可。
核心就是告诉客户端：**用什么命令启动这个服务**。详细说明见下一节。

---

## 它提供什么工具（当前）

### 计算器功能（feature: `calculator`）

| 工具名 | 说明 |
|---|---|
| `calculate` | 求值一个数学表达式，支持变量、函数、常量。核心工具。 |
| `evaluate_math` | `calculate` 的别名，语义更直白，方便模型命中。 |
| `convert_units` | 单位换算（长度 / 质量 / 体积 / 时间 / 数据量 / 温度）。 |
| `list_calculator_capabilities` | 返回计算器支持的全部函数、常量、运算符说明，供模型先查后用。 |

**为什么给模型配计算器？** 语言模型生成的是"最可能的文本"，不是精确算术。多位数乘法、百分比、单位换算这类任务，
模型"猜"出来的答案经常差一点，而且它自己无法验证。让模型把表达式交给本服务的
`calculate` 执行，等于把算术外包给一个确定性的解释器，结果可复现、可审计。这就是这个项目存在的第一个理由。

`calculate` 表达式示例：

```
(1200 * 1.13) / 4                 -> 339
2 ** 10 + sqrt(144)               -> 1036
hypot(3, 4) * pi                  -> 15.707963...
price = 199; price * 0.85         -> 支持自定义变量
round(sin(radians(30)), 4)        -> 0.5
```

---

## 核心设计：为什么加功能不用改核心

一句话概括：**启动 → 发现 → 注册 → 挂载**，四步全自动。

```
启动
 │
 ├─ config.py      读取配置（服务名、要启用/禁用哪些功能、传输方式…）
 │
 ├─ loader.py      扫描 faster_mcp/features/ 下的每个子包
 │                  ↓ 导入模块（import 本身就会触发注册）
 ├─ registry.py    功能模块调用 @mcp_feature / @feature_tool 装饰器
 │                  → 把自己登记进全局注册表 FeatureRegistry
 │
 └─ server.py      遍历注册表 → 把每个工具挂到 FastMCP 实例上 → 运行
```

关键点：**注册发生在 import 阶段，而不是 server.py 里手写一堆 import。**
`loader.py` 用 `pkgutil` + `importlib` 遍历目录，所以新增一个功能模块时，
没有任何一个"列表文件"需要你手动去追加。这就是可扩展性的来源：

> 加功能 = 在 `features/` 下新建一个文件夹 + 写一个继承 `Feature` 的类。
> 不需要修改 `server.py`、不需要修改任何已有文件。

每个功能模块还能自己声明依赖（`Feature.dependencies`），
加载器会自动按依赖顺序构建并做**循环依赖检测**，让功能之间可以安全复用。

---

## 三步加一个新功能

完整示例、模板、约定和常见坑都在 **[docs/EXTENDING.md](docs/EXTENDING.md)**，这里给最短版本：

**第 1 步：建目录**

```
faster_mcp/features/<你的功能名>/
├─ __init__.py     # 内容：from .feature import <YourFeature>  (导出 Feature 子类)
├─ feature.py      # 定义 <YourFeature>(Feature)
└─ ...             # 你自己的实现文件
```

**第 2 步：写 Feature 子类**（用装饰器声明工具，Schema 从类型注解和 docstring 自动生成）

```python
from ...registry import Feature, feature_tool

class DatetimeFeature(Feature):
    name = "datetime"
    description = "时间与日期工具"
    version = "0.1.0"

    @feature_tool
    def now_utc(self) -> str:
        """返回当前 UTC 时间（ISO 8601）。"""
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()
```

**第 3 步：自检 → 完成**

```powershell
python scripts\smoke_test.py
```

工具立刻出现，**无需改动任何既有文件**。

---

## 环境变量配置

| 变量 | 默认值 | 说明 |
|---|---|---|
| `FASTER_MCP_NAME` | `Faster MCP` | 服务显示名，客户端里看到的名称 |
| `FASTER_MCP_FEATURES` | `*` | 逗号分隔的功能白名单，`*` 表示全部启用 |
| `FASTER_MCP_DISABLED` | 空 | 逗号分隔的功能黑名单，优先级高于白名单 |
| `FASTER_MCP_LOG_LEVEL` | `INFO` | 日志级别 |

例：只想暴露计算器 → `FASTER_MCP_FEATURES=calculator`

---

## 测试

```powershell
pip install -e ".[dev]"
pytest
```

测试分两层，这是刻意的设计：

- `test_safe_eval.py` —— 只测求值器，纯函数、无依赖。
- `test_calculator.py` —— 测 Feature 层：注册是否生效、工具是否可被调用、参数校验是否正常。

给新功能写测试时照抄这个分层：**引擎逻辑一份测试，工具暴露一份测试。**

---

## 安全说明（重要）

计算器本质上是"执行模型给出的字符串"，所以求值器**不是** `eval()`：

- 使用 `ast` 解析后做**节点白名单**，只允许算术/比较/布尔表达式与显式允许的函数。
- 函数与常量来自自己的白名单表，**不是内置命名空间**，拿不到 `__import__`、`open`、`exec` 之类。
- 表达式长度、幂指数、阶乘上限都有硬限制，防止 `9**9**9`、`10**7!` 这类资源耗尽攻击。
- 求值过程在受控的 `eval` 中进行，`globals`/`locals` 均为空字典，变量只存在于自建的作用域里。

结论：模型无法通过表达式读写文件、联网或导入模块。加新功能时如果涉及执行类操作，请照抄这个
"白名单 + 限额" 的思路，而不要退回裸 `eval`。

---

## 下一步可以加什么

架构已经为这些留好位置，任选其一都能在半小时内起步：

- `datetime` —— 当前时间、日期差、时区换算（模型极容易算错闰年和时区）
- `text` —— 字符串统计、正则替换、编码转换、哈希
- `json_yaml` —— 结构化数据解析与转换
- `statistics` —— 描述统计、分位数、相关分析
- `finance` —— 复利、贷款月供、IRR
- `web` —— 联网检索（需注意该功能的权限与超时约束）

---

## 许可

MIT
